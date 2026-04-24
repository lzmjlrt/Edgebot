"""Built-in subagent management tools."""
from typing import Any
import json
from edgebot.tools.base import BaseTool
from edgebot.tools.registry import SUBAGENT


class TaskTool(BaseTool):
    @property
    def name(self) -> str: return "task"
    @property
    def description(self) -> str: return "Spawn a subagent for isolated exploration or work."
    @property
    def parameters(self) -> dict: return {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "description": {"type": "string"},
            "name": {"type": "string"},
            "agent_type": {"type": "string", "enum": ["Explore", "general-purpose", "reviewer"]},
            "run_in_background": {"type": "boolean"},
            "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 600000},
        },
        "required": ["prompt"],
    }
    @staticmethod
    def _map_capability(agent_type: str | None) -> str:
        mapping = {
            "Explore": "explore",
            "general-purpose": "builder",
            "reviewer": "reviewer",
        }
        return mapping.get(agent_type or "Explore", "explore")
    async def execute(self, **kwargs: Any) -> Any:
        capability = self._map_capability(kwargs.get("agent_type"))
        description = kwargs.get("description", "")
        name = kwargs.get("name", "")
        if kwargs.get("run_in_background", True):
            result = SUBAGENT.spawn(
                capability,
                kwargs["prompt"],
                name=name,
                description=description,
            )
        else:
            result = await SUBAGENT.run_and_wait(
                capability,
                kwargs["prompt"],
                name=name,
                description=description,
                timeout_ms=kwargs.get("timeout_ms", 30000),
            )
        return json.dumps(result, indent=2, ensure_ascii=False)


class CheckSubagentTool(BaseTool):
    @property
    def name(self) -> str: return "check_subagent"
    @property
    def description(self) -> str: return "Check one isolated subagent task."
    @property
    def parameters(self) -> dict: return {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "include_output": {"type": "boolean"},
        },
        "required": ["task_id"],
    }
    def is_read_only(self, params: dict[str, Any] | None = None) -> bool: return True
    def execute(self, **kwargs: Any) -> Any:
        getter = SUBAGENT.detail if kwargs.get("include_output") else SUBAGENT.status
        return json.dumps(getter(kwargs["task_id"]), indent=2, ensure_ascii=False)


class ListSubagentsTool(BaseTool):
    @property
    def name(self) -> str: return "list_subagents"
    @property
    def description(self) -> str: return "List isolated subagent tasks."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {}}
    def is_read_only(self, params: dict[str, Any] | None = None) -> bool: return True
    def execute(self, **kwargs: Any) -> Any:
        return json.dumps(SUBAGENT.list_all(), indent=2, ensure_ascii=False)


class ControlSubagentTool(BaseTool):
    @property
    def name(self) -> str: return "control_subagent"
    @property
    def description(self) -> str: return "Change a subagent state: move it to foreground/background, or stop it."
    @property
    def parameters(self) -> dict: return {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "action": {"type": "string", "enum": ["foreground", "background", "stop"]},
            "reason": {"type": "string"},
        },
        "required": ["task_id", "action"],
    }
    def execute(self, **kwargs: Any) -> Any:
        action = kwargs["action"]
        if action == "foreground":
            result = SUBAGENT.set_backgrounded(kwargs["task_id"], False)
        elif action == "background":
            result = SUBAGENT.set_backgrounded(kwargs["task_id"], True)
        else:
            result = SUBAGENT.stop(kwargs["task_id"], reason=kwargs.get("reason", "stopped by user"))
        return json.dumps(result, indent=2, ensure_ascii=False)


class WaitSubagentTool(BaseTool):
    @property
    def name(self) -> str: return "wait_subagent"
    @property
    def description(self) -> str: return "Wait for a subagent, optionally foregrounding it first."
    @property
    def parameters(self) -> dict: return {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 600000},
            "foreground": {"type": "boolean"},
            "include_output": {"type": "boolean"},
        },
        "required": ["task_id"],
    }
    async def execute(self, **kwargs: Any) -> Any:
        result = await SUBAGENT.wait(
            kwargs["task_id"],
            timeout_ms=kwargs.get("timeout_ms", 30000),
            foreground=kwargs.get("foreground", False),
            include_output=kwargs.get("include_output", True),
        )
        return json.dumps(result, indent=2, ensure_ascii=False)
