"""Built-in background process tools."""
from typing import Any
import json
from edgebot.tools.base import BaseTool
from edgebot.tools.registry import BG, SUBAGENT


class BackgroundRunTool(BaseTool):
    @property
    def name(self) -> str: return "background_run"
    @property
    def description(self) -> str: return "Run process in background"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}
    def execute(self, **kwargs: Any) -> Any: return json.dumps(BG.run(kwargs["command"], kwargs.get("timeout", 120)), indent=2, ensure_ascii=False)


class CheckBackgroundTool(BaseTool):
    @property
    def name(self) -> str: return "check_background"
    @property
    def description(self) -> str: return "Check background process"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"task_id": {"type": "string"}}}
    def is_read_only(self, params: dict[str, Any] | None = None) -> bool: return True
    def execute(self, **kwargs: Any) -> Any: return BG.check(kwargs.get("task_id"))


class TaskOutputTool(BaseTool):
    @property
    def name(self) -> str: return "task_output"
    @property
    def description(self) -> str: return "Read output from a background shell or subagent task, optionally waiting for completion."
    @property
    def parameters(self) -> dict: return {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "block": {"type": "boolean"},
            "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 600000},
        },
        "required": ["task_id"],
    }
    def is_read_only(self, params: dict[str, Any] | None = None) -> bool: return True
    async def execute(self, **kwargs: Any) -> Any:
        result = BG.task_output(
            kwargs["task_id"],
            kwargs.get("block", True),
            kwargs.get("timeout_ms", 30000),
        )
        if result.get("retrieval_status") == "not_found":
            result = await SUBAGENT.task_output(
                kwargs["task_id"],
                block=kwargs.get("block", True),
                timeout_ms=kwargs.get("timeout_ms", 30000),
            )
        return json.dumps(result, indent=2, ensure_ascii=False)
