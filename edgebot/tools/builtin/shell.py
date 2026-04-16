"""
edgebot/tools/builtin/shell.py - Shell tool ported to BaseTool.
"""
from typing import Any
from edgebot.tools.base import BaseTool
from edgebot.tools.shell import run_bash

class BashTool(BaseTool):
    @property
    def name(self) -> str: return "bash"
    @property
    def description(self) -> str: return "Run a shell command."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    def execute(self, **kwargs: Any) -> Any: return run_bash(kwargs["command"])