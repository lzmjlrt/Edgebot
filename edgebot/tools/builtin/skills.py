"""Built-in skill loading tool."""
from typing import Any
from edgebot.tools.base import BaseTool
from edgebot.tools.registry import SKILLS


class LoadSkillTool(BaseTool):
    @property
    def name(self) -> str: return "load_skill"
    @property
    def description(self) -> str: return "Load specialized knowledge by name."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    def is_read_only(self, params: dict[str, Any] | None = None) -> bool: return True
    def execute(self, **kwargs: Any) -> Any: return SKILLS.load(kwargs["name"])
