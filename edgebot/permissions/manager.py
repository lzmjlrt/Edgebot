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
import asyncio
import fnmatch
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
from edgebot.tools.shell import is_read_only_command


PromptHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
BatchPromptHandler = Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any] | None]]

_RULES_VERSION = 2
_DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class PermissionRule:
    action: str
    tool: str
    pattern: str
    raw: str
    source: str
    prefix: bool = False


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
        "task",
    }
    _READ_ONLY_BUT_APPROVAL_REQUIRED = {
        "web_fetch",
        "web_search",
    }
    _DEFAULT_MODE_VALUES = {"ask", "acceptEdits", "bypassPermissions"}
    _TOOL_ALIASES = {
        "bash": "bash",
        "write": "write_file",
        "writefile": "write_file",
        "write_file": "write_file",
        "edit": "edit_file",
        "editfile": "edit_file",
        "edit_file": "edit_file",
        "read": "read_file",
        "readfile": "read_file",
        "read_file": "read_file",
        "glob": "glob",
        "grep": "grep",
        "webfetch": "web_fetch",
        "web_fetch": "web_fetch",
        "websearch": "web_search",
        "web_search": "web_search",
        "task": "task",
        "backgroundrun": "background_run",
        "background_run": "background_run",
    }
    _DISPLAY_TOOL_NAMES = {
        "bash": "Bash",
        "write_file": "Write",
        "edit_file": "Edit",
        "read_file": "Read",
        "glob": "Glob",
        "grep": "Grep",
        "web_fetch": "WebFetch",
        "web_search": "WebSearch",
        "task": "Task",
        "background_run": "BackgroundRun",
    }

    def __init__(
        self,
        rules_path: Path,
        *,
        user_settings_path: Path | None = None,
        project_settings_path: Path | None = None,
        local_settings_path: Path | None = None,
        approval_timeout_seconds: float | None = _DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ):
        self.rules_path = Path(rules_path)
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        if project_settings_path is None or local_settings_path is None:
            from edgebot.config import WORKDIR

            project_root = Path(WORKDIR)
        else:
            project_root = Path(project_settings_path).parent.parent
        self.user_settings_path = Path(user_settings_path) if user_settings_path else (
            Path.home() / ".claude" / "settings.json"
        )
        self.project_settings_path = Path(project_settings_path) if project_settings_path else (
            project_root / ".claude" / "settings.json"
        )
        self.local_settings_path = Path(local_settings_path) if local_settings_path else (
            project_root / ".claude" / "settings.local.json"
        )
        self.approval_timeout_seconds = approval_timeout_seconds
        self._prompt_handler: PromptHandler | None = None
        self._batch_prompt_handler: BatchPromptHandler | None = None
        self._session_rules: dict[str, Any] = {
            "allow_tools": [],
            "bash_prefixes": [],
            "bash_programs": [],
        }
        self._rules = self._load_or_seed_rules()
        self._deny_regexes = self._compile_deny_patterns()
        self._settings_rules: dict[str, list[PermissionRule]] = {
            "allow": [],
            "deny": [],
            "ask": [],
        }
        self._default_mode = "ask"
        self._load_settings_rules()

    # ----- handler wiring -----

    def set_prompt_handler(self, handler: PromptHandler | None) -> None:
        self._prompt_handler = handler

    def set_batch_prompt_handler(self, handler: BatchPromptHandler | None) -> None:
        self._batch_prompt_handler = handler

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

    def _read_json_file(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _write_json_file(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _load_settings_rules(self) -> None:
        """Load Claude-style permissions from user/project/local settings.

        Arrays merge across all levels. Deny rules are intentionally evaluated
        before ask/allow regardless of source, so a deny cannot be loosened by
        an allow in another file.
        """
        loaded: dict[str, list[PermissionRule]] = {
            "allow": [],
            "deny": [],
            "ask": [],
        }
        default_mode = "ask"
        for source, path in (
            ("user", self.user_settings_path),
            ("project", self.project_settings_path),
            ("local", self.local_settings_path),
        ):
            data = self._read_json_file(path)
            permissions = data.get("permissions")
            if not isinstance(permissions, dict):
                continue
            mode = permissions.get("defaultMode")
            if isinstance(mode, str) and mode in self._DEFAULT_MODE_VALUES:
                default_mode = mode
            for action in ("allow", "deny", "ask"):
                value = permissions.get(action)
                if not isinstance(value, list):
                    continue
                for item in value:
                    if not isinstance(item, str):
                        continue
                    rule = self._parse_unified_rule(item, action=action, source=source)
                    if rule is not None:
                        loaded[action].append(rule)
        self._settings_rules = loaded
        self._default_mode = default_mode

    @classmethod
    def _canonical_tool_name(cls, name: str) -> str:
        collapsed = name.strip().replace("-", "_")
        key = collapsed.replace("_", "").lower()
        if collapsed.lower() in cls._TOOL_ALIASES:
            return cls._TOOL_ALIASES[collapsed.lower()]
        return cls._TOOL_ALIASES.get(key, collapsed.lower())

    @classmethod
    def _display_tool_name(cls, tool_name: str) -> str:
        return cls._DISPLAY_TOOL_NAMES.get(tool_name, tool_name)

    @classmethod
    def _parse_unified_rule(
        cls,
        raw: str,
        *,
        action: str,
        source: str,
    ) -> PermissionRule | None:
        text = raw.strip()
        if not text:
            return None
        match = re.fullmatch(r"([A-Za-z_][\w-]*)\((.*)\)", text)
        if not match:
            return None
        tool = cls._canonical_tool_name(match.group(1))
        body = match.group(2).strip()
        prefix = False
        if tool == "bash" and body.endswith(":*"):
            body = body[:-2]
            prefix = not any(ch in body for ch in "*?[")
        return PermissionRule(
            action=action,
            tool=tool,
            pattern=body,
            raw=text,
            source=source,
            prefix=prefix,
        )

    @classmethod
    def _format_unified_rule(cls, tool_name: str, pattern: str) -> str:
        return f"{cls._display_tool_name(tool_name)}({pattern})"

    @staticmethod
    def _glob_match(value: str, pattern: str) -> bool:
        return fnmatch.fnmatchcase(value, pattern)

    @staticmethod
    def _path_match(value: str, pattern: str) -> bool:
        value_norm = value.replace("\\", "/")
        pattern_norm = pattern.replace("\\", "/")
        return fnmatch.fnmatchcase(value_norm, pattern_norm)

    def _match_rule(self, rule: PermissionRule, tool_name: str, params: dict[str, Any]) -> bool:
        tool_name = self._canonical_tool_name(tool_name)
        if rule.tool != tool_name:
            return False
        if tool_name == "bash":
            command = str(params.get("command", "")).strip()
            if rule.prefix:
                return command == rule.pattern or command.startswith(rule.pattern + " ")
            return self._glob_match(command, rule.pattern)
        if tool_name in {"write_file", "edit_file", "read_file"}:
            path = str(params.get("path", "")).strip()
            return self._path_match(path, rule.pattern)
        if tool_name == "web_fetch":
            value = str(params.get("url", "")).strip()
            return self._glob_match(value, rule.pattern)
        if tool_name == "web_search":
            value = str(params.get("query", "")).strip()
            return self._glob_match(value, rule.pattern)
        if tool_name == "task":
            value = str(params.get("agent_type") or params.get("name") or params.get("description") or params.get("prompt") or "").strip()
            return self._glob_match(value, rule.pattern)
        if tool_name == "background_run":
            command = str(params.get("command", "")).strip()
            return self._glob_match(command, rule.pattern)
        return self._glob_match(tool_name, rule.pattern) or self._glob_match("*", rule.pattern)

    def _matched_rules(
        self,
        action: str,
        tool_name: str,
        params: dict[str, Any],
    ) -> list[PermissionRule]:
        return [
            rule
            for rule in self._settings_rules.get(action, [])
            if self._match_rule(rule, tool_name, params)
        ]

    def _settings_decision(self, tool_name: str, params: dict[str, Any]) -> str | None:
        if self._matched_rules("deny", tool_name, params):
            return "deny"
        if self._matched_rules("ask", tool_name, params):
            return "ask"
        if self._matched_rules("allow", tool_name, params):
            return "allow"
        return None

    def _pattern_for_allow_scope(self, request: dict[str, Any], scope: str) -> str:
        tool_name = self._canonical_tool_name(str(request.get("tool", "")))
        if tool_name == "bash":
            command = str(request.get("raw_command") or request.get("scope_value") or "").strip()
            if scope == "allow_program":
                program = str(request.get("scope_value") or self._bash_program(command)).strip()
                return f"{program}:*" if program else command
            return self._bash_permission_pattern(command)
        if tool_name in {"write_file", "edit_file", "read_file"}:
            return str(request.get("params", {}).get("path") or request.get("scope_value") or "*")
        if tool_name == "web_fetch":
            return str(request.get("params", {}).get("url") or "*")
        if tool_name == "web_search":
            return str(request.get("params", {}).get("query") or "*")
        if tool_name == "background_run":
            command = str(request.get("raw_command") or request.get("scope_value") or "").strip()
            return self._bash_permission_pattern(command) if command else "*"
        return "*"

    @staticmethod
    def _bash_permission_pattern(command: str) -> str:
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            tokens = command.split()
        if not tokens:
            return command
        if tokens[0].lower() == "git" and len(tokens) >= 2:
            return f"git {tokens[1]}:*"
        return f"{tokens[0]}:*"

    def _rule_preview(self, request: dict[str, Any], scope: str) -> str:
        tool_name = self._canonical_tool_name(str(request.get("tool", "")))
        return self._format_unified_rule(tool_name, self._pattern_for_allow_scope(request, scope))

    def _save_unified_allow_rule(self, request: dict[str, Any], target_name: str, scope: str) -> None:
        path = self.project_settings_path if target_name == "project" else self.user_settings_path
        data = self._read_json_file(path)
        permissions = data.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
            data["permissions"] = permissions
        allow = permissions.get("allow")
        if not isinstance(allow, list):
            allow = []
            permissions["allow"] = allow
        rule = self._rule_preview(request, scope)
        if rule not in allow:
            allow.append(rule)
        if "defaultMode" not in permissions:
            permissions["defaultMode"] = "ask"
        self._write_json_file(path, data)
        self._load_settings_rules()

    # ----- decision helpers -----

    def _tool_is_sensitive(self, tool_name: str, params: dict[str, Any], tool: Any) -> bool:
        tool_name = self._canonical_tool_name(tool_name)
        if tool_name in self._READ_ONLY_BUT_APPROVAL_REQUIRED:
            return True
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
        settings_decision = self._settings_decision(tool_name, params)
        if settings_decision == "allow":
            return True
        if settings_decision in {"deny", "ask"}:
            return False

        tool_name = self._canonical_tool_name(tool_name)
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
            if not is_read_only_command(command):
                return False
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
        tool_name = self._canonical_tool_name(tool_name)
        if tool_name == "bash":
            command = str(params.get("command", "")).strip()
            program = self._bash_program(command)
            chain = self._bash_programs_in_chain(command)
            is_read_only = is_read_only_command(command)
            # Interpreters / build drivers / package managers must be granted
            # by exact command (`allow_prefix`), never by program name —
            # otherwise one approval lets the model run any script.
            uses_interpreter = bool(chain) and self._chain_contains_interpreter(chain)
            scope_hint = "allow_prefix" if (uses_interpreter or not program or not is_read_only) else "allow_program"
            scope_value = command if uses_interpreter else (program or command)
            request = {
                "tool": tool_name,
                "message": f"Edgebot requests permission to run shell command:\n{command}",
                "scope_hint": scope_hint,
                "scope_value": scope_value,
                "raw_command": command,
                "uses_interpreter": uses_interpreter,
                "requires_confirmation": self._bash_requires_confirmation(command),
                "params": dict(params),
            }
            request["rule_preview"] = self._rule_preview(request, scope_hint)
            return request
        if tool_name in {"write_file", "edit_file"}:
            path = str(params.get("path", "")).strip()
            request = {
                "tool": tool_name,
                "message": f"Edgebot requests permission to modify file:\n{path}",
                "scope_hint": "allow_tool",
                "scope_value": tool_name,
                "params": dict(params),
            }
            request["rule_preview"] = self._rule_preview(request, "allow_tool")
            return request
        if tool_name == "background_run":
            command = str(params.get("command", "")).strip()
            request = {
                "tool": tool_name,
                "message": f"Edgebot requests permission to start background task:\n{command}",
                "scope_hint": "allow_prefix",
                "scope_value": command,
                "raw_command": command,
                "params": dict(params),
            }
            request["rule_preview"] = self._rule_preview(request, "allow_prefix")
            return request
        if tool_name == "web_fetch":
            url = str(params.get("url", "")).strip()
            request = {
                "tool": tool_name,
                "message": f"Edgebot requests permission to fetch URL:\n{url}",
                "scope_hint": "allow_tool",
                "scope_value": tool_name,
                "params": dict(params),
            }
            request["rule_preview"] = self._rule_preview(request, "allow_tool")
            return request
        if tool_name == "web_search":
            query = str(params.get("query", "")).strip()
            request = {
                "tool": tool_name,
                "message": f"Edgebot requests permission to search the web:\n{query}",
                "scope_hint": "allow_tool",
                "scope_value": tool_name,
                "params": dict(params),
            }
            request["rule_preview"] = self._rule_preview(request, "allow_tool")
            return request
        if tool_name == "task":
            description = str(params.get("description") or params.get("prompt") or "").strip()
            request = {
                "tool": tool_name,
                "message": f"Edgebot requests permission to start a subagent:\n{description}",
                "scope_hint": "allow_tool",
                "scope_value": tool_name,
                "params": dict(params),
            }
            request["rule_preview"] = self._rule_preview(request, "allow_tool")
            return request
        return {
            "tool": tool_name,
            "message": f"Edgebot requests permission to use tool '{tool_name}'.",
            "scope_hint": "allow_tool",
            "scope_value": tool_name,
            "params": dict(params),
        }

    @staticmethod
    def _bash_requires_confirmation(command: str) -> bool:
        return bool(re.search(
            r"\b("
            r"git\s+reset\s+--hard|git\s+clean\s+-[^\s]*[xfd]|"
            r"rm\s+-[^\s]*[rf]|remove-item\b[^|;&<>]*\b(?:-recurse|-force)|"
            r"sudo|curl\b[^|;&<>]*\|\s*(?:bash|sh|pwsh|powershell)"
            r")",
            command,
            re.IGNORECASE,
        ))

    def _apply_allow(self, request: dict[str, Any], persist: bool, scope: str) -> None:
        save_target = str(request.get("save_target") or "").strip()
        if persist and save_target in {"project", "user"}:
            self._save_unified_allow_rule(request, save_target, scope)
            return
        target = self._rules if persist else self._session_rules
        tool_name = self._canonical_tool_name(str(request.get("tool", "")))
        if tool_name == "bash" and scope == "allow_program":
            value = str(request.get("scope_value", "")).strip().lower()
            if value:
                target.setdefault("bash_programs", [])
                if value not in target["bash_programs"]:
                    target["bash_programs"].append(value)
        elif tool_name == "bash" and scope == "allow_prefix":
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
        tool_name = self._canonical_tool_name(tool_name)

        settings_decision = self._settings_decision(tool_name, params)
        if settings_decision == "deny":
            return PermissionDecision(
                "deny",
                "denied_by_rule: operation matches a configured deny rule. "
                "Do NOT retry with alternate syntax; ask the user instead.",
            )
        if settings_decision == "allow":
            return PermissionDecision("allow", updated_params=params)

        if tool_name == "bash":
            command = str(params.get("command", "")).strip()
            if command and self._bash_denied(command):
                return PermissionDecision(
                    "deny",
                    "Policy denied: command matches deny pattern. "
                    "Do NOT retry with shell tricks; ask the user instead.",
                )

        if settings_decision != "ask" and getattr(tool, "is_read_only", lambda _: False)(params) and (
            tool_name not in self._READ_ONLY_BUT_APPROVAL_REQUIRED
        ):
            return PermissionDecision("allow", updated_params=params)

        if settings_decision != "ask" and self._matches_allow_rule(tool_name, params):
            return PermissionDecision("allow", updated_params=params)

        if settings_decision != "ask":
            if self._default_mode == "bypassPermissions":
                return PermissionDecision("allow", updated_params=params)
            if (
                self._default_mode == "acceptEdits"
                and tool_name in {"write_file", "edit_file"}
            ):
                return PermissionDecision("allow", updated_params=params)
            if not self._tool_is_sensitive(tool_name, params, tool):
                return PermissionDecision("allow", updated_params=params)

        request = self._build_request(tool_name, params)
        if self._prompt_handler is None:
            return PermissionDecision(
                "deny",
                f"Permission denied for tool '{tool_name}': interactive approval is unavailable.",
            )

        response = await self._run_prompt_with_timeout(request)
        if not isinstance(response, dict):
            return PermissionDecision("deny", f"Permission denied for tool '{tool_name}'.")

        action = str(response.get("action", "deny"))
        if action != "allow":
            feedback = str(response.get("feedback", "")).strip()
            suffix = f" Feedback: {feedback}" if feedback else ""
            return PermissionDecision("deny", f"Permission denied for tool '{tool_name}'.{suffix}")

        updated_params = response.get("updated_params")
        if not isinstance(updated_params, dict):
            updated_params = params
        scope = str(response.get("scope", "") or "")
        persist = bool(response.get("persist", False))
        if scope:
            allow_request = self._build_request(tool_name, updated_params)
            allow_request["save_target"] = str(response.get("save_target") or "")
            self._apply_allow(allow_request, persist, scope)
        return PermissionDecision("allow", updated_params=updated_params)

    async def _run_prompt_with_timeout(self, request: dict[str, Any]) -> dict[str, Any] | None:
        assert self._prompt_handler is not None
        if self.approval_timeout_seconds is None or self.approval_timeout_seconds <= 0:
            return await self._prompt_handler(request)
        try:
            return await asyncio.wait_for(
                self._prompt_handler(request),
                timeout=float(self.approval_timeout_seconds),
            )
        except asyncio.TimeoutError:
            return {
                "action": "deny",
                "feedback": (
                    f"Permission prompt timed out after "
                    f"{self.approval_timeout_seconds:g}s."
                ),
            }

    def build_request(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._build_request(tool_name, params)

    async def prompt_batch(self, requests: list[dict[str, Any]]) -> dict[str, Any] | None:
        if self._batch_prompt_handler is None:
            return None
        if self.approval_timeout_seconds is None or self.approval_timeout_seconds <= 0:
            return await self._batch_prompt_handler(requests)
        try:
            return await asyncio.wait_for(
                self._batch_prompt_handler(requests),
                timeout=float(self.approval_timeout_seconds),
            )
        except asyncio.TimeoutError:
            return {"action": "deny_all", "message": "Permission prompt timed out."}

    def can_batch_prompt(self) -> bool:
        return self._batch_prompt_handler is not None

    def requires_prompt(self, tool_name: str, params: dict[str, Any], tool: Any) -> bool:
        """Return True when this invocation would need interactive approval."""
        tool_name = self._canonical_tool_name(tool_name)
        settings_decision = self._settings_decision(tool_name, params)
        if settings_decision in {"allow", "deny"}:
            return False
        if settings_decision == "ask":
            return True
        if tool_name == "bash":
            command = str(params.get("command", "")).strip()
            if command and self._bash_denied(command):
                return False
        if getattr(tool, "is_read_only", lambda _: False)(params) and (
            tool_name not in self._READ_ONLY_BUT_APPROVAL_REQUIRED
        ):
            return False
        if self._matches_allow_rule(tool_name, params):
            return False
        if self._default_mode == "bypassPermissions":
            return False
        if self._default_mode == "acceptEdits" and tool_name in {"write_file", "edit_file"}:
            return False
        return self._tool_is_sensitive(tool_name, params, tool)

    def list_rules(self) -> dict[str, Any]:
        return {
            "persisted": self._rules,
            "settings": {
                "user": str(self.user_settings_path),
                "project": str(self.project_settings_path),
                "local": str(self.local_settings_path),
                "defaultMode": self._default_mode,
                "allow": [rule.raw for rule in self._settings_rules["allow"]],
                "deny": [rule.raw for rule in self._settings_rules["deny"]],
                "ask": [rule.raw for rule in self._settings_rules["ask"]],
            },
            "session": self._session_rules,
        }

    async def clear_session_rules(self) -> None:
        self._session_rules = {
            "allow_tools": [],
            "bash_prefixes": [],
            "bash_programs": [],
        }
