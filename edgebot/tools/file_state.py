"""Track file-read state for read-before-edit warnings and read deduplication."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ReadState:
    mtime: float
    offset: int
    limit: int | None
    content_hash: str | None
    can_dedup: bool


_state: dict[str, ReadState] = {}


def _hash_file(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def record_read(path: str | Path, offset: int = 1, limit: int | None = None) -> None:
    resolved = str(Path(path).resolve())
    try:
        mtime = os.path.getmtime(resolved)
    except OSError:
        return
    _state[resolved] = ReadState(
        mtime=mtime,
        offset=offset,
        limit=limit,
        content_hash=_hash_file(resolved),
        can_dedup=True,
    )


def record_write(path: str | Path) -> None:
    resolved = str(Path(path).resolve())
    try:
        mtime = os.path.getmtime(resolved)
    except OSError:
        _state.pop(resolved, None)
        return
    _state[resolved] = ReadState(
        mtime=mtime,
        offset=1,
        limit=None,
        content_hash=_hash_file(resolved),
        can_dedup=False,
    )


def check_read(path: str | Path) -> str | None:
    resolved = str(Path(path).resolve())
    entry = _state.get(resolved)
    if entry is None:
        return "Warning: file has not been read yet. Read it first to verify content before editing."
    try:
        current_mtime = os.path.getmtime(resolved)
    except OSError:
        return None
    current_hash = _hash_file(resolved)
    if current_mtime != entry.mtime or current_hash != entry.content_hash:
        return "Warning: file has been modified since last read. Re-read to verify content before editing."
    return None
