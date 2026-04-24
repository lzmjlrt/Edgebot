"""
edgebot/tools/builtin/shell.py - Shell tool ported to BaseTool.
"""
from typing import Any
from edgebot.tools.base import BaseTool
from edgebot.tools.shell import is_read_only_command, run_bash

class BashTool(BaseTool):
    @property
    def name(self) -> str: return "bash"
    @property
    def description(self) -> str: return "Run a shell command."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    def is_read_only(self, params: dict[str, Any] | None = None) -> bool: return is_read_only_command(str((params or {}).get("command", "")))
    def execute(self, **kwargs: Any) -> Any: return run_bash(kwargs["command"])
