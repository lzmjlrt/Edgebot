"""
edgebot/permissions/manager.py - Lightweight permission approval layer.

This is structurally inspired by claude-code's permission pipeline:
 - central decision point before tool execution
 - persisted/session allow rules
 - interactive approval when a sensitive action needs confirmation
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable


PromptHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


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
        "spawn_teammate",
        "shutdown_request",
    }

    def __init__(self, rules_path: Path):
        self.rules_path = Path(rules_path)
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        self._prompt_handler: PromptHandler | None = None
        self._session_rules: dict[str, Any] = {
            "allow_tools": [],
            "bash_prefixes": [],
        }
        self._rules = self._load_rules()

    def set_prompt_handler(self, handler: PromptHandler | None) -> None:
        self._prompt_handler = handler

    def _load_rules(self) -> dict[str, Any]:
        default = {
            "version": 1,
            "allow_tools": [],
            "bash_prefixes": [],
        }
        if not self.rules_path.exists():
            return default
        try:
            data = json.loads(self.rules_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        if not isinstance(data, dict):
            return default
        for key in ("allow_tools", "bash_prefixes"):
            value = data.get(key, [])
            data[key] = [item for item in value if isinstance(item, str)]
        data["version"] = 1
        return {**default, **data}

    def _save_rules(self) -> None:
        self.rules_path.write_text(
            json.dumps(self._rules, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _tool_is_sensitive(self, tool_name: str, params: dict[str, Any], tool: Any) -> bool:
        if tool_name in self._TOOL_ALWAYS_ASK:
            return True
        if tool_name == "bash":
            command = str(params.get("command", "")).strip()
            return bool(command) and not getattr(tool, "is_read_only", lambda _: False)(params)
        return not getattr(tool, "is_read_only", lambda _: False)(params)

    def _matches_allow_rule(self, tool_name: str, params: dict[str, Any]) -> bool:
        allow_tools = set(self._rules.get("allow_tools", [])) | set(self._session_rules.get("allow_tools", []))
        if tool_name in allow_tools:
            return True
        if tool_name == "bash":
            command = str(params.get("command", "")).strip()
            prefixes = list(self._rules.get("bash_prefixes", [])) + list(self._session_rules.get("bash_prefixes", []))
            return any(command.startswith(prefix) for prefix in prefixes if prefix)
        return False

    def _build_request(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "bash":
            command = str(params.get("command", "")).strip()
            return {
                "tool": tool_name,
                "message": f"Edgebot requests permission to run shell command:\n{command}",
                "scope_hint": "allow_prefix",
                "scope_value": command,
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

    def _apply_allow(self, request: dict[str, Any], persist: bool, scope: str | None) -> None:
        target = self._rules if persist else self._session_rules
        if request["tool"] == "bash" and scope == "allow_prefix":
            value = str(request.get("scope_value", "")).strip()
            if value and value not in target["bash_prefixes"]:
                target["bash_prefixes"].append(value)
        elif scope == "allow_tool":
            value = str(request.get("scope_value", "")).strip()
            if value and value not in target["allow_tools"]:
                target["allow_tools"].append(value)
        if persist:
            self._save_rules()

    async def authorize(self, tool_name: str, params: dict[str, Any], tool: Any) -> PermissionDecision:
        if getattr(tool, "is_read_only", lambda _: False)(params):
            return PermissionDecision("allow", updated_params=params)
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
        }
