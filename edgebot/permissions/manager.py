"""
edgebot/permissions/manager.py - Lightweight permission approval layer.

Structurally inspired by claude-code's permission pipeline:
 - central decision point before tool execution
 - persisted/session allow rules
 - interactive approval when a sensitive action needs confirmation

v2 schema adds:
 - bash_programs: program-name allowlist (first shlex token, basename-stripped)
 - bash_deny_patterns: regex blacklist; matches return a hard policy error
 - workspace_write_auto_allow: auto-allow write/edit inside WORKDIR
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from edgebot.permissions.defaults import (
    DEFAULT_BASH_DENY_PATTERNS,
    DEFAULT_BASH_PROGRAMS,
    INTERPRETER_PROGRAMS,
)


PromptHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]

_RULES_VERSION = 2


@dataclass(slots=True)
class PermissionDecision:
    behavior: str
    message: str = ""
    updated_params: dict[str, Any] | None = None


class PermissionManager:
    """Runtime permission checker with persisted allow rules."""

    _TOOL_ALWAYS_ASK = {
        "bash",
        "write_file",
        "edit_file",
        "background_run",
    }

    def __init__(self, rules_path: Path):
        self.rules_path = Path(rules_path)
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        self._prompt_handler: PromptHandler | None = None
        self._session_rules: dict[str, Any] = {
            "allow_tools": [],
            "bash_prefixes": [],
            "bash_programs": [],
        }
        self._rules = self._load_or_seed_rules()
        self._deny_regexes = self._compile_deny_patterns()

    # ----- handler wiring -----

    def set_prompt_handler(self, handler: PromptHandler | None) -> None:
        self._prompt_handler = handler

    # ----- rules I/O -----

    def _default_rules(self) -> dict[str, Any]:
        return {
            "version": _RULES_VERSION,
            "allow_tools": [],
            "bash_programs": list(DEFAULT_BASH_PROGRAMS),
            "bash_prefixes": [],
            "bash_deny_patterns": list(DEFAULT_BASH_DENY_PATTERNS),
            "workspace_write_auto_allow": True,
        }

    def _load_or_seed_rules(self) -> dict[str, Any]:
        if not self.rules_path.exists():
            rules = self._default_rules()
            self._write_rules(rules)
            return rules

        try:
            data = json.loads(self.rules_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._default_rules()

        if not isinstance(data, dict):
            return self._default_rules()

        rules = self._default_rules()
        for key in ("allow_tools", "bash_programs", "bash_prefixes", "bash_deny_patterns"):
            value = data.get(key)
            if isinstance(value, list):
                rules[key] = [item for item in value if isinstance(item, str)]
        if isinstance(data.get("workspace_write_auto_allow"), bool):
            rules["workspace_write_auto_allow"] = data["workspace_write_auto_allow"]

        if data.get("version") != _RULES_VERSION:
            self._write_rules(rules)
        return rules

    def _write_rules(self, rules: dict[str, Any]) -> None:
        self.rules_path.write_text(
            json.dumps(rules, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _save_rules(self) -> None:
        self._write_rules(self._rules)

    def _compile_deny_patterns(self) -> list[re.Pattern[str]]:
        compiled: list[re.Pattern[str]] = []
        for pat in self._rules.get("bash_deny_patterns", []):
            try:
                compiled.append(re.compile(pat))
            except re.error:
                continue
        return compiled

    # ----- decision helpers -----

    def _tool_is_sensitive(self, tool_name: str, params: dict[str, Any], tool: Any) -> bool:
        if getattr(tool, "is_read_only", lambda _: False)(params):
            return False
        return tool_name in self._TOOL_ALWAYS_ASK

    @staticmethod
    def _normalize_program(token: str) -> str:
        if not token:
            return ""
        if "/" in token or "\\" in token:
            token = Path(token).name
        if token.lower().endswith(".exe"):
            token = token[:-4]
        return token.lower()

    @classmethod
    def _bash_program(cls, command: str) -> str:
        """First program name in the command (legacy single-token view)."""
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            tokens = command.strip().split()
        if not tokens:
            return ""
        return cls._normalize_program(tokens[0])

    # Shell operators that chain or pipe distinct commands; each side must
    # independently pass the program allowlist.
    _CHAIN_OPERATORS = ("&&", "||", ";", "|")
    # `cd` is intentionally not in the default allowlist (a bare allowance
    # would let `cd X && <anything>` slip through). Instead we transparently
    # skip leading `cd ...` segments and validate the *following* command(s).
    _TRANSPARENT_LEADERS = {"cd", "pushd", "popd"}

    @classmethod
    def _bash_programs_in_chain(cls, command: str) -> list[str]:
        """Split on shell chain operators and return each segment's program.

        - `cd X && rg foo`         -> ["rg"]            (cd is transparent)
        - `cd X && rm -rf .`       -> ["rm"]            (still gated)
        - `git status | grep foo`  -> ["git", "grep"]
        - `for %f in (*.py) do find ...` (cmd.exe) -> ["for", "find"]
        Returns [] if the command cannot be parsed safely.
        """
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return []
        if not tokens:
            return []

        segments: list[list[str]] = [[]]
        for tok in tokens:
            if tok in cls._CHAIN_OPERATORS:
                segments.append([])
            else:
                segments[-1].append(tok)

        programs: list[str] = []
        for seg in segments:
            if not seg:
                continue
            head = cls._normalize_program(seg[0])
            if head.startswith("@"):
                head = head[1:]
            # `cd` / `pushd` / `popd` as a whole segment are transparent —
            # the safety check is whatever segment runs *next* in the chain.
            if head in cls._TRANSPARENT_LEADERS:
                continue
            # Windows cmd `for %f in (...) do <cmd> ...` — also include `do <cmd>`.
            if head == "for" and "do" in seg:
                try:
                    do_idx = seg.index("do")
                    if do_idx + 1 < len(seg):
                        inner = cls._normalize_program(seg[do_idx + 1])
                        if inner.startswith("@"):
                            inner = inner[1:]
                        if inner:
                            programs.append(inner)
                except ValueError:
                    pass
                programs.append("for")
                continue
            if head:
                programs.append(head)
        return programs

    def _bash_denied(self, command: str) -> bool:
        return any(rx.search(command) for rx in self._deny_regexes)

    @staticmethod
    def _chain_contains_interpreter(programs: list[str]) -> bool:
        """True if any segment of the command chain runs an interpreter / pkg manager / build driver.

        Bare program-name allow-rules ("allow program: python") are unsafe for these
        commands because their argument surface is Turing-complete (`python -c "..."`,
        `npm run any-script`, `make any-target`). When this returns True, the command
        cannot be auto-approved by a program-name rule and must either match an exact
        prefix rule or be approved interactively.
        """
        return any(p in INTERPRETER_PROGRAMS for p in programs)

    def _matches_allow_rule(self, tool_name: str, params: dict[str, Any]) -> bool:
        allow_tools = set(self._rules.get("allow_tools", [])) | set(self._session_rules.get("allow_tools", []))
        if tool_name in allow_tools:
            return True

        if tool_name == "bash":
            command = str(params.get("command", "")).strip()
            if not command:
                return False
            if self._bash_denied(command):
                return False
            chain = self._bash_programs_in_chain(command)
            # Legacy / explicit-prefix rules are checked first because users
            # who intentionally granted `python -V` or `npm test` should keep
            # working even though `python`/`npm` are interpreters.
            prefixes = list(self._rules.get("bash_prefixes", [])) + list(self._session_rules.get("bash_prefixes", []))
            if any(command.startswith(prefix) for prefix in prefixes if prefix):
                return True
            # Program-name allowlist only applies when no segment of the chain
            # is an interpreter / package manager / build driver. This stops
            # `python` in the seed list from acting as a wildcard.
            if chain and self._chain_contains_interpreter(chain):
                return False
            programs_allowed = {
                p.lower() for p in (
                    list(self._rules.get("bash_programs", []))
                    + list(self._session_rules.get("bash_programs", []))
                )
            }
            if chain and all(p in programs_allowed for p in chain):
                return True
            return False

        if tool_name in {"write_file", "edit_file"} and self._rules.get("workspace_write_auto_allow", True):
            if self._path_inside_workdir(params.get("path", "")):
                return True

        return False

    @staticmethod
    def _path_inside_workdir(raw_path: Any) -> bool:
        from edgebot.config import WORKDIR

        try:
            path_str = str(raw_path or "").strip()
            if not path_str:
                return False
            p = Path(path_str)
            if not p.is_absolute():
                p = (Path(WORKDIR) / p)
            resolved = p.resolve()
            workdir = Path(WORKDIR).resolve()
            return resolved == workdir or workdir in resolved.parents
        except (OSError, ValueError):
            return False

    def _build_request(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "bash":
            command = str(params.get("command", "")).strip()
            program = self._bash_program(command)
            chain = self._bash_programs_in_chain(command)
            # Interpreters / build drivers / package managers must be granted
            # by exact command (`allow_prefix`), never by program name —
            # otherwise one approval lets the model run any script.
            uses_interpreter = bool(chain) and self._chain_contains_interpreter(chain)
            scope_hint = "allow_prefix" if (uses_interpreter or not program) else "allow_program"
            scope_value = command if uses_interpreter else (program or command)
            return {
                "tool": tool_name,
                "message": f"Edgebot requests permission to run shell command:\n{command}",
                "scope_hint": scope_hint,
                "scope_value": scope_value,
                "raw_command": command,
                "uses_interpreter": uses_interpreter,
            }
        if tool_name in {"write_file", "edit_file"}:
            path = str(params.get("path", "")).strip()
            return {
                "tool": tool_name,
                "message": f"Edgebot requests permission to modify file:\n{path}",
                "scope_hint": "allow_tool",
                "scope_value": tool_name,
            }
        if tool_name == "background_run":
            command = str(params.get("command", "")).strip()
            return {
                "tool": tool_name,
                "message": f"Edgebot requests permission to start background task:\n{command}",
                "scope_hint": "allow_tool",
                "scope_value": tool_name,
            }
        return {
            "tool": tool_name,
            "message": f"Edgebot requests permission to use tool '{tool_name}'.",
            "scope_hint": "allow_tool",
            "scope_value": tool_name,
        }

    def _apply_allow(self, request: dict[str, Any], persist: bool, scope: str) -> None:
        target = self._rules if persist else self._session_rules
        if request["tool"] == "bash" and scope == "allow_program":
            value = str(request.get("scope_value", "")).strip().lower()
            if value:
                target.setdefault("bash_programs", [])
                if value not in target["bash_programs"]:
                    target["bash_programs"].append(value)
        elif request["tool"] == "bash" and scope == "allow_prefix":
            value = str(request.get("raw_command") or request.get("scope_value", "")).strip()
            if value:
                target.setdefault("bash_prefixes", [])
                if value not in target["bash_prefixes"]:
                    target["bash_prefixes"].append(value)
        elif scope == "allow_tool":
            value = str(request.get("scope_value", "")).strip()
            if value:
                target.setdefault("allow_tools", [])
                if value not in target["allow_tools"]:
                    target["allow_tools"].append(value)
        if persist:
            self._save_rules()

    # ----- public API -----

    async def authorize(self, tool_name: str, params: dict[str, Any], tool: Any) -> PermissionDecision:
        if getattr(tool, "is_read_only", lambda _: False)(params):
            return PermissionDecision("allow", updated_params=params)

        if tool_name == "bash":
            command = str(params.get("command", "")).strip()
            if command and self._bash_denied(command):
                return PermissionDecision(
                    "deny",
                    "Policy denied: command matches deny pattern. "
                    "Do NOT retry with shell tricks; ask the user instead.",
                )

        if self._matches_allow_rule(tool_name, params):
            return PermissionDecision("allow", updated_params=params)

        if not self._tool_is_sensitive(tool_name, params, tool):
            return PermissionDecision("allow", updated_params=params)

        request = self._build_request(tool_name, params)
        if self._prompt_handler is None:
            return PermissionDecision(
                "deny",
                f"Permission denied for tool '{tool_name}': interactive approval is unavailable.",
            )

        response = await self._prompt_handler(request)
        if not isinstance(response, dict):
            return PermissionDecision("deny", f"Permission denied for tool '{tool_name}'.")

        action = str(response.get("action", "deny"))
        if action != "allow":
            feedback = str(response.get("feedback", "")).strip()
            suffix = f" Feedback: {feedback}" if feedback else ""
            return PermissionDecision("deny", f"Permission denied for tool '{tool_name}'.{suffix}")

        scope = str(response.get("scope", "") or "")
        persist = bool(response.get("persist", False))
        if scope:
            self._apply_allow(request, persist, scope)
        updated_params = response.get("updated_params")
        if not isinstance(updated_params, dict):
            updated_params = params
        return PermissionDecision("allow", updated_params=updated_params)

    def list_rules(self) -> dict[str, Any]:
        return {
            "persisted": self._rules,
            "session": self._session_rules,
        }

    async def clear_session_rules(self) -> None:
        self._session_rules = {
            "allow_tools": [],
            "bash_prefixes": [],
            "bash_programs": [],
        }
