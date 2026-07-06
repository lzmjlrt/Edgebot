"""
edgebot/agent/memory/dream_tools.py - Dream-scoped tools
(read/edit restricted to workspace memory files).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from edgebot.tools.base import BaseTool


def _normalized_path_key(path: Path) -> str:
    key = os.path.abspath(path)
    if os.name == "nt":
        key = os.path.normcase(key)
    return key


def _is_allowed_skill_file(path: Path, skills_dir: Path) -> bool:
    try:
        rel = path.resolve().relative_to(skills_dir.resolve())
    except ValueError:
        return False
    return len(rel.parts) == 2 and rel.parts[0] not in {"", ".", ".."} and rel.parts[1] == "SKILL.md"


class _DreamReadTool(BaseTool):
    """read_file scoped to the workspace for Dream agent."""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read file contents. Use this to check current memory file contents."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file to read."},
                "limit": {"type": "integer", "description": "Maximum lines to read."},
                "offset": {"type": "integer", "description": "1-based line offset.", "minimum": 1},
                "force": {
                    "type": "boolean",
                    "description": "Force full read even if the global file cache thinks it is unchanged.",
                },
            },
            "required": ["path"],
        }

    def is_read_only(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def __init__(self, workspace: Path):
        self._workspace = workspace

    def execute(self, **kwargs: Any) -> Any:
        from edgebot.tools.filesystem import run_read
        return run_read(
            kwargs["path"],
            kwargs.get("limit"),
            kwargs.get("offset", 1),
            kwargs.get("force", True),
        )


class _DreamEditTool(BaseTool):
    """edit_file scoped to workspace memory files for Dream agent."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Replace exact text in a file. Use this to update USER.md, "
            "SOUL.md, or MEMORY.md with new information."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to edit."},
                "old_text": {"type": "string", "description": "Exact text to find."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
        }

    def __init__(
        self,
        workspace: Path,
        *,
        allowed_files: tuple[Path, ...] | None = None,
        allowed_skill_dir: Path | None = None,
    ):
        self._workspace = workspace
        runtime_dir = workspace / ".edgebot"
        files = allowed_files if allowed_files is not None else (
            runtime_dir / "USER.md",
            runtime_dir / "SOUL.md",
            runtime_dir / "memory" / "MEMORY.md",
        )
        self._allowed_files = {_normalized_path_key(path) for path in files}
        self._allowed_skill_dir = allowed_skill_dir

    def execute(self, **kwargs: Any) -> Any:
        from edgebot.tools.base import safe_path
        from edgebot.tools.filesystem import run_edit

        try:
            target = safe_path(kwargs["path"])
        except Exception as exc:
            return f"Error: {exc}"
        allowed = _normalized_path_key(target) in self._allowed_files
        if not allowed and self._allowed_skill_dir is not None:
            allowed = _is_allowed_skill_file(target, self._allowed_skill_dir)
        if not allowed:
            return (
                "Error: Dream edit_file may only update USER.md, SOUL.md, "
                "memory/MEMORY.md, or skills/<name>/SKILL.md."
            )
        return run_edit(
            kwargs["path"], kwargs["old_text"], kwargs["new_text"],
            kwargs.get("replace_all", False),
        )


class _DreamWriteTool(BaseTool):
    """write_file scoped to new Dream skill files."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Create .edgebot/skills/<name>/SKILL.md for reusable workflows."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Skill file to write."},
                "content": {"type": "string", "description": "Complete SKILL.md content."},
            },
            "required": ["path", "content"],
        }

    def __init__(
        self,
        workspace: Path,
        *,
        skills_dir: Path | None = None,
    ):
        self._workspace = workspace
        self._skills_dir = skills_dir or (workspace / ".edgebot" / "skills")

    def execute(self, **kwargs: Any) -> Any:
        from edgebot.tools.base import safe_path
        from edgebot.tools.filesystem import run_write

        try:
            target = safe_path(kwargs["path"])
        except Exception as exc:
            return f"Error: {exc}"
        if not _is_allowed_skill_file(target, self._skills_dir):
            return "Error: Dream write_file may only write skills/<name>/SKILL.md."
        if target.exists() and target.read_text(encoding="utf-8").strip():
            return "Error: Skill already exists. Use edit_file to update existing skills."
        return run_write(kwargs["path"], kwargs["content"])
