"""
edgebot/agent/memory/store.py - MemoryStore: pure file I/O layer for
Edgebot memory files (MEMORY.md, history.jsonl, cursors, skills).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Iterator

from edgebot.config import MEMORY_DIR, runtime_dir_for_workspace
from edgebot.utils.gitstore import GitStore

from edgebot.agent.memory.heuristics import (
    _normalize_history_tags,
    _read_file,
    _truncate_text,
)

MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
HISTORY_FILE = MEMORY_DIR / "history.jsonl"
CURSOR_FILE = MEMORY_DIR / ".cursor"
DREAM_CURSOR_FILE = MEMORY_DIR / ".dream_cursor"

_SKILLS_CONTEXT_MAX_CHARS = 16_000
_SKILL_FILE_MAX_CHARS = 8_000
_HISTORY_ENTRY_HARD_CAP = 64_000
_MEMORY_INDEX_MAX_CHARS = 25_000
_MEMORY_INDEX_MAX_LINES = 200
_MAX_TOPIC_FILES = 200
_TOPIC_METADATA_MAX_CHARS = 8_000
_TOPIC_EXCERPT_MAX_CHARS = 1_500
_MAX_HISTORY_RECALL_ENTRIES = 200
_MAX_HISTORY_RECALL_RESULTS = 3


class MemoryStore:
    """Pure file I/O for Edgebot memory files."""

    _append_locks: ClassVar[dict[Path, threading.Lock]] = {}
    _append_locks_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, workspace: Path, *, memory_dir: Path | None = None):
        self.workspace = Path(workspace)
        runtime_dir = (
            Path(memory_dir).parent
            if memory_dir is not None
            else runtime_dir_for_workspace(self.workspace)
        )
        self.memory_dir = Path(memory_dir) if memory_dir is not None else runtime_dir / "memory"
        runtime_dir = self.memory_dir.parent
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.topics_dir = self.memory_dir / "topics"
        self.history_file = self.memory_dir / "history.jsonl"
        self.cursor_file = self.memory_dir / ".cursor"
        self.dream_cursor_file = self.memory_dir / ".dream_cursor"
        self.soul_file = runtime_dir / "SOUL.md"
        self.user_file = runtime_dir / "USER.md"
        self.skills_dir = runtime_dir / "skills"
        lock_key = self.memory_dir.resolve()
        with self._append_locks_guard:
            self._append_lock = self._append_locks.setdefault(lock_key, threading.Lock())
        self.git = GitStore(
            runtime_dir,
            tracked_files=[
                "SOUL.md",
                "USER.md",
                "memory/MEMORY.md",
                "memory/.dream_cursor",
            ],
            tracked_dirs=["skills", "memory/topics"],
            allow_nested=True,
        )
        legacy_memory_dir = workspace / "memory"
        if not self.memory_dir.exists() and legacy_memory_dir.exists():
            shutil.copytree(legacy_memory_dir, self.memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def ensure_git_initialized(self) -> bool:
        """Initialize the Dream git store after templates have been seeded."""
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_file.exists():
            self.memory_file.write_text(
                "---\n"
                "schema: edgebot-memory-index-v1\n"
                "updated_at: \n"
                "---\n\n"
                "# Memory Index\n\n"
                "Dream maintains topic summaries here. Use recall_memory for details.\n",
                encoding="utf-8",
            )
        if self.git.is_initialized():
            return True
        return self.git.init()

    def read_memory(self) -> str:
        return _read_file(self.memory_file)

    def read_user(self) -> str:
        return _read_file(self.user_file)

    def read_soul(self) -> str:
        return _read_file(self.soul_file)

    def iter_skill_files(self) -> Iterator[Path]:
        if not self.skills_dir.exists():
            return iter(())
        skill_files: list[Path] = []
        for skill_dir in sorted(self.skills_dir.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if skill_dir.is_dir() and skill_file.is_file():
                skill_files.append(skill_file)
        return iter(skill_files)

    def read_skills_context(self) -> str:
        sections: list[str] = []
        runtime_dir = self.memory_dir.parent
        for skill_file in self.iter_skill_files():
            try:
                rel = skill_file.relative_to(runtime_dir).as_posix()
            except ValueError:
                rel = str(skill_file)
            content = _truncate_text(_read_file(skill_file), _SKILL_FILE_MAX_CHARS)
            sections.append(f"### {rel}\n{content}")
        if not sections:
            return "(no skills)"
        return _truncate_text("\n\n".join(sections), _SKILLS_CONTEXT_MAX_CHARS)

    def get_memory_context(self) -> str:
        content = self._bounded_memory_index().strip()
        return f"## Long-term Memory\n\n{content}" if content and content != "(empty)" else ""

    def _bounded_memory_index(self) -> str:
        """Return the always-loaded topic index within its explicit limits."""
        content = self.read_memory()
        lines = content.splitlines()
        truncated = len(lines) > _MEMORY_INDEX_MAX_LINES
        content = "\n".join(lines[:_MEMORY_INDEX_MAX_LINES])
        if len(content) > _MEMORY_INDEX_MAX_CHARS:
            content = content[:_MEMORY_INDEX_MAX_CHARS]
            truncated = True
        if truncated:
            content = content.rstrip() + "\n\n[Memory index truncated; use recall_memory for topic details.]"
        return content

    @staticmethod
    def _query_terms(query: str) -> set[str]:
        return {
            term.casefold()
            for term in re.findall(r"[\w-]+", query, flags=re.UNICODE)
            if len(term) > 1
        }

    @staticmethod
    def _frontmatter_and_body(content: str) -> tuple[dict[str, str], str]:
        """Parse the small, explicit frontmatter schema used by topic files."""
        if not content.startswith("---\n"):
            return {}, content
        closing = content.find("\n---", 4)
        if closing < 0:
            return {}, content
        metadata: dict[str, str] = {}
        for line in content[4:closing].splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in {
                "title", "summary", "type", "created_at", "updated_at", "scope",
            }:
                metadata[key.strip()] = value.strip().strip('"')
        return metadata, content[closing + 4:].lstrip("\r\n")

    def _iter_topic_paths(self) -> Iterator[Path]:
        """Yield a bounded, runtime-contained set of topic files."""
        if not self.topics_dir.is_dir():
            return iter(())
        root = self.topics_dir.resolve()
        paths: list[Path] = []
        for path in sorted(self.topics_dir.rglob("*.md")):
            try:
                path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            paths.append(path)
            if len(paths) >= _MAX_TOPIC_FILES:
                break
        return iter(paths)

    def recall_memory(self, query: str, *, max_results: int = 5) -> dict[str, list[dict[str, Any]]]:
        """Return bounded, relevance-ranked topic and archive excerpts."""
        terms = self._query_terms(query)
        max_results = max(1, min(int(max_results), 5))
        ranked_topics: list[tuple[int, str, Path, dict[str, str]]] = []
        for path in self._iter_topic_paths():
            try:
                metadata_text = path.read_text(encoding="utf-8")[:_TOPIC_METADATA_MAX_CHARS]
            except (OSError, UnicodeDecodeError):
                continue
            metadata, _body = self._frontmatter_and_body(metadata_text)
            searchable = " ".join(
                metadata.get(field, "") for field in ("title", "summary", "type", "scope")
            ).casefold()
            score = sum(term in searchable for term in terms)
            if score <= 0:
                continue
            ranked_topics.append((score, metadata.get("updated_at", ""), path, metadata))

        ranked_topics.sort(key=lambda item: (-item[0], item[1], item[2].as_posix()))
        topics: list[dict[str, Any]] = []
        for _score, _updated, path, metadata in ranked_topics[:max_results]:
            try:
                content = path.read_text(encoding="utf-8")
                _parsed_metadata, body = self._frontmatter_and_body(content)
                relative = path.resolve().relative_to(self.memory_dir.resolve()).as_posix()
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            topics.append({
                "path": relative,
                "title": metadata.get("title", path.stem),
                "summary": metadata.get("summary", ""),
                "type": metadata.get("type", ""),
                "scope": metadata.get("scope", ""),
                "excerpt": _truncate_text(body.strip(), _TOPIC_EXCERPT_MAX_CHARS),
            })

        history: list[dict[str, Any]] = []
        entries = list(self._iter_valid_entries())[-_MAX_HISTORY_RECALL_ENTRIES:]
        ranked_history: list[tuple[int, int, dict[str, Any]]] = []
        for entry, cursor in entries:
            content = str(entry.get("content") or "")
            score = sum(term in content.casefold() for term in terms)
            if score:
                ranked_history.append((score, cursor, entry))
        ranked_history.sort(key=lambda item: (-item[0], -item[1]))
        for _score, cursor, entry in ranked_history[:_MAX_HISTORY_RECALL_RESULTS]:
            history.append({
                "cursor": cursor,
                "timestamp": str(entry.get("timestamp") or ""),
                "source": str(entry.get("source") or ""),
                "excerpt": _truncate_text(str(entry.get("content") or ""), _TOPIC_EXCERPT_MAX_CHARS),
            })
        return {"topics": topics, "history": history}

    def queue_remember(self, content: str, *, session_key: str | None = None) -> int:
        """Record an explicit durable-memory request for Dream to process."""
        return self.append_history(
            content,
            max_chars=4_000,
            session_key=session_key,
            source="remember_request",
            tags=["durable", "explicit_remember"],
        )

    def _next_cursor(self) -> int:
        if self.cursor_file.exists():
            try:
                return int(self.cursor_file.read_text(encoding="utf-8").strip()) + 1
            except (OSError, ValueError):
                pass
        last = self._read_last_entry()
        if last and isinstance(last.get("cursor"), int):
            return last["cursor"] + 1
        return max((cursor for _entry, cursor in self._iter_valid_entries()), default=0) + 1

    def _read_last_entry(self) -> dict | None:
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [line for line in data.splitlines() if line.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def append_history(
        self,
        content: str,
        *,
        max_chars: int | None = None,
        session_key: str | None = None,
        source: str | None = None,
        tags: list[str] | tuple[str, ...] | set[str] | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self._append_lock:
            cursor = self._next_cursor()
            limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
            cleaned = content.strip()
            if len(cleaned) > limit:
                cleaned = _truncate_text(cleaned, limit)
            extra = dict(metadata or {})
            record_source = source or extra.pop("source", None) or "unknown"
            record_tags = tags if tags is not None else extra.pop("tags", [])
            record = {
                "cursor": cursor,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "source": str(record_source),
                "tags": _normalize_history_tags(record_tags),
                "content": cleaned,
            }
            if session_key:
                record["session_key"] = session_key
            if extra:
                record.update(extra)
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self.cursor_file.write_text(str(cursor), encoding="utf-8")
            return cursor

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cursor = self._valid_cursor(entry.get("cursor"))
                    if cursor is None:
                        continue
                    yield entry, cursor
        except FileNotFoundError:
            return

    def read_unprocessed_history(
        self,
        since_cursor: int,
        *,
        session_key: str | None = None,
    ) -> list[dict]:
        entries = [
            entry
            for entry, cursor in self._iter_valid_entries()
            if cursor > since_cursor
        ]
        if session_key is None:
            return entries
        return [
            entry
            for entry in entries
            if entry.get("session_key") == session_key
        ]

    def get_last_dream_cursor(self) -> int:
        if self.dream_cursor_file.exists():
            try:
                return int(self.dream_cursor_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pass
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self.dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    _MAX_HISTORY_ENTRIES = 1000

    def compact_history(self) -> None:
        """Drop oldest entries if history.jsonl exceeds the cap."""
        if self._MAX_HISTORY_ENTRIES <= 0:
            return
        entries = [entry for entry, _cursor in self._iter_valid_entries()]
        if not entries:
            return
        if len(entries) <= self._MAX_HISTORY_ENTRIES:
            return
        kept = entries[-self._MAX_HISTORY_ENTRIES:]
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            for entry in kept:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.history_file)
        with suppress(PermissionError, OSError):
            fd = os.open(str(self.history_file.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
