"""Track file-read state for read-before-edit warnings and read deduplication."""

from __future__ import annotations

import hashlib
import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ReadState:
    mtime: float
    offset: int
    limit: int | None
    content_hash: str | None
    can_dedup: bool


def _hash_file(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


class FileStates:
    """Read/write freshness state for one logical agent session."""

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state: dict[str, ReadState] = {}

    def record_read(
        self,
        path: str | Path,
        offset: int = 1,
        limit: int | None = None,
    ) -> None:
        resolved = str(Path(path).resolve())
        try:
            mtime = os.path.getmtime(resolved)
        except OSError:
            return
        self._state[resolved] = ReadState(
            mtime=mtime,
            offset=offset,
            limit=limit,
            content_hash=_hash_file(resolved),
            can_dedup=True,
        )

    def record_write(self, path: str | Path) -> None:
        resolved = str(Path(path).resolve())
        try:
            mtime = os.path.getmtime(resolved)
        except OSError:
            self._state.pop(resolved, None)
            return
        self._state[resolved] = ReadState(
            mtime=mtime,
            offset=1,
            limit=None,
            content_hash=_hash_file(resolved),
            can_dedup=False,
        )

    def check_read(self, path: str | Path) -> str | None:
        resolved = str(Path(path).resolve())
        entry = self._state.get(resolved)
        if entry is None:
            return (
                "Warning: file has not been read yet. "
                "Read it first to verify content before editing."
            )
        try:
            current_mtime = os.path.getmtime(resolved)
        except OSError:
            return None
        if current_mtime != entry.mtime:
            current_hash = _hash_file(resolved)
            if entry.content_hash and current_hash == entry.content_hash:
                entry.mtime = current_mtime
                return None
            return (
                "Warning: file has been modified since last read. "
                "Re-read to verify content before editing."
            )
        if entry.content_hash and _hash_file(resolved) != entry.content_hash:
            return (
                "Warning: file has been modified since last read. "
                "Re-read to verify content before editing."
            )
        return None

    def is_unchanged(
        self,
        path: str | Path,
        offset: int = 1,
        limit: int | None = None,
    ) -> bool:
        resolved = str(Path(path).resolve())
        entry = self._state.get(resolved)
        if entry is None or not entry.can_dedup:
            return False
        if entry.offset != offset or entry.limit != limit:
            return False
        try:
            current_mtime = os.path.getmtime(resolved)
        except OSError:
            return False
        if current_mtime != entry.mtime:
            current_hash = _hash_file(resolved)
            if current_hash != entry.content_hash:
                entry.can_dedup = False
                return False
            entry.can_dedup = False
            return True
        if entry.content_hash and _hash_file(resolved) != entry.content_hash:
            entry.can_dedup = False
            return False
        entry.mtime = current_mtime
        return True

    def clear(self) -> None:
        self._state.clear()


class FileStateStore:
    """Process-local lookup for per-session file states."""

    __slots__ = ("_states_by_key",)

    def __init__(self) -> None:
        self._states_by_key: dict[str, FileStates] = {}

    def for_session(self, session_key: str | None) -> FileStates:
        key = session_key or "__default__"
        states = self._states_by_key.get(key)
        if states is None:
            states = FileStates()
            self._states_by_key[key] = states
        return states

    def clear(self) -> None:
        for states in self._states_by_key.values():
            states.clear()
        self._states_by_key.clear()


_current_file_states: ContextVar[FileStates | None] = ContextVar(
    "edgebot_file_states",
    default=None,
)
_default = FileStates()
_store = FileStateStore()

# Backward compatibility for callers/tests that reached into file_state._state.
_state = _default._state


def current_file_states(default: FileStates | None = None) -> FileStates:
    return _current_file_states.get() or default or _default


def bind_file_states(file_states: FileStates) -> Token[FileStates | None]:
    return _current_file_states.set(file_states)


def reset_file_states(token: Token[FileStates | None]) -> None:
    _current_file_states.reset(token)


def for_session(session_key: str | None) -> FileStates:
    return _store.for_session(session_key)


def record_read(path: str | Path, offset: int = 1, limit: int | None = None) -> None:
    current_file_states().record_read(path, offset=offset, limit=limit)


def record_write(path: str | Path) -> None:
    current_file_states().record_write(path)


def check_read(path: str | Path) -> str | None:
    return current_file_states().check_read(path)


def is_unchanged(
    path: str | Path,
    offset: int = 1,
    limit: int | None = None,
) -> bool:
    return current_file_states().is_unchanged(path, offset=offset, limit=limit)


def clear() -> None:
    _default.clear()
    _store.clear()
