"""
edgebot/agent/memory/heuristics.py - Text helpers, history-entry filters,
Phase 1 output dedup, and one-shot memory-file cleanup.
"""

from __future__ import annotations

import re
from typing import Any

from rich.console import Console

from edgebot.config import MEMORY_DIR, SOUL_MD_PATH, USER_MD_PATH

_console = Console()

_HISTORY_ENTRY_PREVIEW_MAX_CHARS = 4_000
_CONVERSATION_MAX_CHARS = 48_000


def _read_file(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "(empty)"


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n... (truncated)"
    return text[: max(0, max_chars - len(marker))] + marker


def _normalize_history_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        raw_tags = [tags]
    elif isinstance(tags, (list, tuple, set)):
        raw_tags = list(tags)
    else:
        raw_tags = [str(tags)]
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in raw_tags:
        value = str(tag).strip().lower()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def _history_tags(entry: dict[str, Any]) -> list[str]:
    return _normalize_history_tags(entry.get("tags"))


def _has_durable_history_signal(entry: dict[str, Any]) -> bool:
    tags = set(_history_tags(entry))
    return bool(tags & {"durable", "permanent", "correction"})


def _is_dream_visible_history(entry: dict[str, Any]) -> bool:
    tags = set(_history_tags(entry))
    source = str(entry.get("source") or "unknown").strip().lower()
    if "skip" in tags:
        return False
    if source == "raw_archive" and not _has_durable_history_signal(entry):
        return False
    if "ephemeral" in tags and not _has_durable_history_signal(entry):
        return False
    return True


def _format_history_entry_for_dream(entry: dict[str, Any]) -> str:
    source = str(entry.get("source") or "unknown").strip() or "unknown"
    tags = ",".join(_history_tags(entry)) or "none"
    session = entry.get("session_key")
    session_part = f" session={session}" if session else ""
    return (
        f"[{entry['timestamp']}] "
        f"[source={source} tags={tags}{session_part} cursor={entry['cursor']}] "
        f"{_truncate_text(str(entry.get('content', '')), _HISTORY_ENTRY_PREVIEW_MAX_CHARS)}"
    )


def _format_messages(messages: list[dict]) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if role == "tool":
            continue
        if not content:
            continue
        if isinstance(content, str):
            lines.append(f"[{role}] {_truncate_text(content, 500)}")
    return _truncate_text("\n".join(lines), _CONVERSATION_MAX_CHARS)


def _filter_dedup(analysis: str, existing_blob: str) -> str:
    """Drop Phase 1 lines substantially covered by existing memory.
    Pass through [FILE-REMOVE] and [SKIP] lines unconditionally.
    """
    existing_lower = existing_blob.lower()
    kept: list[str] = []
    for raw in analysis.splitlines():
        line = raw.strip()
        if not line:
            kept.append(raw)
            continue
        m = re.match(
            r"^\[(USER|SOUL|MEMORY|SKILL|SKIP|(?:USER|SOUL|MEMORY|SKILL)-REMOVE)\]\s*(.*)$",
            line, re.I,
        )
        if not m:
            kept.append(raw)
            continue
        tag = m.group(1).upper()
        content = m.group(2).lower()
        if tag == "SKIP" or tag.endswith("-REMOVE"):
            kept.append(raw)
            continue
        words = [w for w in re.findall(r"[a-z0-9_一-鿿]+", content) if len(w) > 1]
        if not words:
            kept.append(raw)
            continue
        hit = sum(1 for w in words if w in existing_lower)
        if hit / len(words) >= 0.7:
            continue
        kept.append(raw)
    return "\n".join(kept)


def _extract_actionable_findings(analysis: str) -> str:
    """Return normalized Phase 1 findings that Phase 2 can execute."""
    findings: list[str] = []
    seen: set[tuple[str, str]] = set()
    for raw in analysis.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^\[(USER|SOUL|MEMORY|SKILL)(-REMOVE)?\]\s*(.*)$", line, re.I)
        if not m:
            continue
        tag = m.group(1).upper() + (m.group(2).upper() if m.group(2) else "")
        content = m.group(3).strip()
        if not content:
            continue
        key = (tag, _normalize_line(content))
        if key in seen:
            continue
        seen.add(key)
        findings.append(f"[{tag}] {content}")
    return "\n".join(findings)


def _normalize_line(line: str) -> str:
    s = line.strip().lstrip("-*").strip()
    s = re.sub(r"\*\*|__|\*|_", "", s)
    s = re.sub(r"\s+", " ", s).lower()
    return s


def cleanup_memory_files_once() -> None:
    """One-shot cleanup for duplicates in USER.md / SOUL.md / MEMORY.md."""
    from edgebot.agent.memory.store import MEMORY_FILE

    marker = MEMORY_DIR / ".memory_cleaned"
    if marker.exists():
        return
    _KV_RE = re.compile(r"^[\s\-*]*\*?\*?([A-Za-z][A-Za-z \w/]*?)\*?\*?\s*:\s*(.+)$")
    results: list[str] = []
    for fname, path in (
        ("USER.md", USER_MD_PATH),
        ("SOUL.md", SOUL_MD_PATH),
        ("MEMORY.md", MEMORY_FILE),
    ):
        if not path.exists():
            continue
        original = path.read_text(encoding="utf-8")
        if fname == "USER.md":
            kvs: dict[str, str] = {}
            rest: list[str] = []
            for ln in original.splitlines():
                m = _KV_RE.match(ln.strip())
                if m and m.group(2).strip():
                    kvs[_normalize_line(m.group(1))] = ln.rstrip()
                else:
                    rest.append(ln.rstrip())
            rebuilt = "\n".join(rest).rstrip() + ("\n\n" + "\n".join(kvs.values()) if kvs else "") + "\n"
        else:
            seen: set[str] = set()
            kept: list[str] = []
            for ln in original.splitlines():
                n = _normalize_line(ln)
                if n and n in seen:
                    continue
                if n:
                    seen.add(n)
                kept.append(ln)
            rebuilt = "\n".join(kept).rstrip() + "\n"
        if rebuilt != original:
            path.write_text(rebuilt, encoding="utf-8")
            results.append(fname)
    try:
        marker.write_text("cleaned\n", encoding="utf-8")
    except Exception:
        pass
    if results:
        _console.print(
            f"[dim]  [memory] cleaned duplicates in {', '.join(results)}[/dim]"
        )
