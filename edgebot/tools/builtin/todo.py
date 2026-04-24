"""Built-in todo and compress tools."""
from typing import Any
from edgebot.tools.base import BaseTool
from edgebot.tools.registry import TODO


class TodoWriteTool(BaseTool):
    @property
    def name(self) -> str: return "TodoWrite"
    @property
    def description(self) -> str: return "Update task tracking list."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"]}}}, "required": ["items"]}
    def execute(self, **kwargs: Any) -> Any: return TODO.update(kwargs["items"])


class CompressTool(BaseTool):
    @property
    def name(self) -> str: return "compress"
    @property
    def description(self) -> str: return "Manually compress conversation context."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {}}
    def execute(self, **kwargs: Any) -> Any: return "Compressing..."
