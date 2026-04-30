"""
edgebot/tools/builtin/filesystem.py - File tools ported to BaseTool.
"""
from typing import Any
from edgebot.tools.base import BaseTool
from edgebot.tools.filesystem import run_edit, run_list_dir, run_read, run_write

class ReadFileTool(BaseTool):
    @property
    def name(self) -> str: return "read_file"
    @property
    def description(self) -> str: return "Read file contents."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}, "offset": {"type": "integer", "minimum": 1}}, "required": ["path"]}
    def is_read_only(self, params: dict[str, Any] | None = None) -> bool: return True
    def execute(self, **kwargs: Any) -> Any: return run_read(kwargs["path"], kwargs.get("limit"), kwargs.get("offset", 1))

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
    def parameters(self) -> dict: return {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["path", "old_text", "new_text"]}
    def execute(self, **kwargs: Any) -> Any: return run_edit(kwargs["path"], kwargs["old_text"], kwargs["new_text"], kwargs.get("replace_all", False))

class ListDirTool(BaseTool):
    @property
    def name(self) -> str: return "list_dir"
    @property
    def description(self) -> str: return "List directory contents."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}, "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000}}, "required": ["path"]}
    def is_read_only(self, params: dict[str, Any] | None = None) -> bool: return True
    def execute(self, **kwargs: Any) -> Any: return run_list_dir(kwargs["path"], kwargs.get("recursive", False), kwargs.get("max_entries", 200))
