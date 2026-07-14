"""
edgebot/agent/memory/dream_tools.py - Dream-scoped tools
(read/edit restricted to runtime memory files).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from edgebot.tools.base import BaseTool


_DEFAULT_READ_LIMIT = 2_000
_MAX_READ_CHARS = 128_000
_MAX_EDIT_FILE_SIZE = 1024 * 1024 * 1024


def _normalized_path_key(path: Path) -> str:
    key = os.path.abspath(path.resolve(strict=False))
    if os.name == "nt":
        key = os.path.normcase(key)
    return key


def _resolve_runtime_path(path: str, workspace: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return candidate.resolve(strict=False)


def _is_allowed_skill_file(path: Path, skills_dir: Path) -> bool:
    try:
        rel = path.resolve().relative_to(skills_dir.resolve())
    except ValueError:
        return False
    return len(rel.parts) == 2 and rel.parts[0] not in {"", ".", ".."} and rel.parts[1] == "SKILL.md"


def _is_allowed_topic_file(path: Path, topics_dir: Path) -> bool:
    try:
        rel = path.resolve(strict=False).relative_to(topics_dir.resolve())
    except ValueError:
        return False
    return len(rel.parts) == 1 and rel.suffix == ".md" and rel.stem not in {"", ".", ".."}


def _is_allowed_path(
    path: Path,
    *,
    allowed_files: set[str],
    allowed_skill_dir: Path | None,
    allowed_topics_dir: Path | None,
) -> bool:
    if _normalized_path_key(path) in allowed_files:
        return True
    return (
        (allowed_skill_dir is not None and _is_allowed_skill_file(path, allowed_skill_dir))
        or (allowed_topics_dir is not None and _is_allowed_topic_file(path, allowed_topics_dir))
    )


def _runtime_read(path: Path, label: str, limit: int | None, offset: int) -> str:
    try:
        if not path.exists():
            return f"Error: File not found: {label}"
        if not path.is_file():
            return f"Error: Not a file: {label}"
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        all_lines = text.splitlines()
        if not all_lines:
            return f"(Empty file: {label})"
        offset = max(offset, 1)
        total = len(all_lines)
        if offset > total:
            return f"Error: offset {offset} is beyond end of file ({total} lines)"
        end = min(total, offset - 1 + (limit or _DEFAULT_READ_LIMIT))
        result = "\n".join(
            f"{line_number}| {line}"
            for line_number, line in enumerate(all_lines[offset - 1:end], start=offset)
        )
        if len(result) > _MAX_READ_CHARS:
            result = result[:_MAX_READ_CHARS] + "\n\n(Output truncated at ~128K chars)"
        if end < total:
            return result + f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
        return result + f"\n\n(End of file — {total} lines total)"
    except UnicodeDecodeError:
        return f"Error: Cannot read binary file {label}. Only UTF-8 text is supported."
    except OSError as exc:
        return f"Error: {exc}"


def _runtime_write(path: Path, content: str) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} characters to {path}"
    except OSError as exc:
        return f"Error writing file: {exc}"


def _runtime_edit(path: Path, label: str, old_text: str, new_text: str, replace_all: bool) -> str:
    try:
        if not path.exists():
            if old_text:
                return f"Error: File not found: {label}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_text, encoding="utf-8")
            return f"Successfully created {path}"
        if not path.is_file():
            return f"Error: Not a file: {label}"
        if path.stat().st_size > _MAX_EDIT_FILE_SIZE:
            return "Error: File too large to edit. Maximum is 1 GiB."
        content = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        if not old_text:
            if content.strip():
                return f"Error: Cannot create file — {label} already exists and is not empty."
            path.write_text(new_text, encoding="utf-8")
            return f"Successfully edited {path}"
        count = content.count(old_text)
        if count == 0:
            return f"Error: old_text not found in {label}."
        if count > 1 and not replace_all:
            return (
                f"Warning: old_text appears {count} times. "
                "Provide more context to make it unique, or set replace_all=true."
            )
        path.write_text(content.replace(old_text, new_text, -1 if replace_all else 1), encoding="utf-8")
        return f"Successfully edited {path}"
    except UnicodeDecodeError:
        return f"Error editing file: Cannot read binary file {label}. Only UTF-8 text is supported."
    except OSError as exc:
        return f"Error editing file: {exc}"


class _DreamReadTool(BaseTool):
    """read_file scoped to Dream's runtime memory files."""

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
                    "description": "Accepted for compatibility; Dream always reads current contents.",
                },
            },
            "required": ["path"],
        }

    def is_read_only(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def __init__(
        self,
        workspace: Path,
        *,
        allowed_files: tuple[Path, ...] = (),
        allowed_skill_dir: Path | None = None,
        allowed_topics_dir: Path | None = None,
    ):
        self._workspace = Path(workspace)
        self._allowed_files = {_normalized_path_key(path) for path in allowed_files}
        self._allowed_skill_dir = allowed_skill_dir
        self._allowed_topics_dir = allowed_topics_dir

    def execute(self, **kwargs: Any) -> Any:
        target = _resolve_runtime_path(kwargs["path"], self._workspace)
        if not _is_allowed_path(
            target,
            allowed_files=self._allowed_files,
            allowed_skill_dir=self._allowed_skill_dir,
            allowed_topics_dir=self._allowed_topics_dir,
        ):
            return "Error: Dream read_file may only access runtime memory files or skills."
        return _runtime_read(target, kwargs["path"], kwargs.get("limit"), kwargs.get("offset", 1))


class _DreamEditTool(BaseTool):
    """edit_file scoped to Dream's runtime memory files."""

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
        allowed_topics_dir: Path | None = None,
    ):
        self._workspace = Path(workspace)
        files = allowed_files or ()
        self._allowed_files = {_normalized_path_key(path) for path in files}
        self._allowed_skill_dir = allowed_skill_dir
        self._allowed_topics_dir = allowed_topics_dir

    def execute(self, **kwargs: Any) -> Any:
        target = _resolve_runtime_path(kwargs["path"], self._workspace)
        if not _is_allowed_path(
            target,
            allowed_files=self._allowed_files,
            allowed_skill_dir=self._allowed_skill_dir,
            allowed_topics_dir=self._allowed_topics_dir,
        ):
            return (
                "Error: Dream edit_file may only update USER.md, SOUL.md, "
                "memory/MEMORY.md, memory/topics/<topic>.md, or skills/<name>/SKILL.md."
            )
        return _runtime_edit(
            target,
            kwargs["path"],
            kwargs["old_text"],
            kwargs["new_text"],
            kwargs.get("replace_all", False),
        )


class _DreamWriteTool(BaseTool):
    """write_file scoped to new Dream skill and durable topic files."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Create runtime skills/<name>/SKILL.md or memory/topics/<topic>.md files."

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
        topics_dir: Path | None = None,
    ):
        self._workspace = Path(workspace)
        self._skills_dir = skills_dir
        self._topics_dir = topics_dir

    def execute(self, **kwargs: Any) -> Any:
        if self._skills_dir is None and self._topics_dir is None:
            return "Error: Dream write_file has no configured runtime directories."
        target = _resolve_runtime_path(kwargs["path"], self._workspace)
        is_skill = self._skills_dir is not None and _is_allowed_skill_file(target, self._skills_dir)
        is_topic = self._topics_dir is not None and _is_allowed_topic_file(target, self._topics_dir)
        if not is_skill and not is_topic:
            return "Error: Dream write_file may only write skills/<name>/SKILL.md or memory/topics/<topic>.md."
        if target.exists() and target.read_text(encoding="utf-8").strip():
            label = "Skill" if is_skill else "Topic"
            return f"Error: {label} already exists. Use edit_file to update it."
        return _runtime_write(target, kwargs["content"])
