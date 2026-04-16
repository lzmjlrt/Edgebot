"""
edgebot/tools/builtin/filesystem.py - File tools ported to BaseTool.
"""
from typing import Any
from edgebot.tools.base import safe_path, BaseTool
from edgebot.tools.filesystem import run_read, run_write, run_edit

class ReadFileTool(BaseTool):
    @property
    def name(self) -> str: return "read_file"
    @property
    def description(self) -> str: return "Read file contents."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}
    def execute(self, **kwargs: Any) -> Any: return run_read(kwargs["path"], kwargs.get("limit"))

class WriteFileTool(BaseTool):
    @property
    def name(self) -> str: return "write_file"
    @property
    def description(self) -> str: return "Write content to file."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}
    def execute(self, **kwargs: Any) -> Any: return run_write(kwargs["path"], kwargs["content"])

class EditFileTool(BaseTool):
    @property
    def name(self) -> str: return "edit_file"
    @property
    def description(self) -> str: return "Replace exact text in file."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}
    def execute(self, **kwargs: Any) -> Any: return run_edit(kwargs["path"], kwargs["old_text"], kwargs["new_text"])