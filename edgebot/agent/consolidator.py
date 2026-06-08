"""
edgebot/agent/consolidator.py - Incremental session history archiving.

Consolidator records older, stable conversation slices into memory/history.jsonl
and advances a per-session message-index cursor. It does not remove messages
from the session; later context-replay code can use the cursor to decide what
to send to the model.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from edgebot.agent.compression import (
    estimate_tokens,
    merge_session_summaries,
    summarize_messages,
)
from edgebot.session.store import SessionStore

_HISTORY_ENTRY_HARD_CAP = 64_000
_FALLBACK_JSON_CAP = 60_000


class Consolidator:
    """Archive already-seen session history behind a metadata cursor."""

    def __init__(
        self,
        session_store: SessionStore,
        provider=None,
        *,
        model: str | None = None,
        memory_dir: Path | None = None,
        keep_recent_messages: int = 8,
    ) -> None:
        if memory_dir is None:
            from edgebot.config import MEMORY_DIR
            memory_dir = MEMORY_DIR
        self.sessions = session_store
        self.provider = provider
        self.model = model
        self.memory_dir = Path(memory_dir)
        self.history_file = self.memory_dir / "history.jsonl"
        self.cursor_file = self.memory_dir / ".cursor"
        self.keep_recent_messages = max(1, keep_recent_messages)

    async def maybe_consolidate_by_tokens(
        self,
        session_key: str,
        *,
        max_unconsolidated_tokens: int,
    ) -> bool:
        """Archive the oldest unconsolidated prefix when it exceeds a token cap."""
        state = self.sessions.load_state(session_key)
        messages = list(state.get("messages", []))
        start = self.sessions.get_last_consolidated(session_key)
        if start >= len(messages):
            return False

        pending = messages[start:]
        if estimate_tokens(pending) <= max_unconsolidated_tokens:
            return False

        boundary = self._find_archive_boundary(messages, start)
        if boundary is None:
            return False

        archive_messages = messages[start:boundary]
        if not archive_messages:
            return False

        content, summary = await self._build_archive_content(
            session_key,
            archive_messages,
        )
        self._append_history_record(
            session_key=session_key,
            start_index=start,
            end_index=boundary,
            content=content,
            archived_message_count=len(archive_messages),
        )
        self.sessions.set_last_consolidated(session_key, boundary)
        if summary:
            state = self.sessions.load_state(session_key)
            previous = state.get("metadata", {}).get("session_summary")
            self.sessions.update_metadata(
                session_key,
                session_summary=merge_session_summaries(previous, summary),
            )
        return True

    def _find_archive_boundary(
        self,
        messages: list[dict[str, Any]],
        start: int,
    ) -> int | None:
        latest = len(messages) - self.keep_recent_messages
        if latest <= start:
            return None
        for boundary in range(latest, start, -1):
            if messages[boundary].get("role") != "user":
                continue
            if self._is_complete_tool_slice(messages[start:boundary]):
                return boundary
        return None

    @staticmethod
    def _is_complete_tool_slice(messages: list[dict[str, Any]]) -> bool:
        declared: set[str] = set()
        fulfilled: set[str] = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tool_call in msg.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        continue
                    call_id = tool_call.get("id")
                    if call_id:
                        declared.add(str(call_id))
            elif msg.get("role") == "tool":
                call_id = msg.get("tool_call_id")
                if not call_id:
                    continue
                call_id = str(call_id)
                if call_id not in declared:
                    return False
                fulfilled.add(call_id)
        return declared.issubset(fulfilled)

    async def _build_archive_content(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
    ) -> tuple[str, str | None]:
        try:
            summary = await summarize_messages(
                messages,
                provider=self.provider,
                model=self.model,
            )
        except Exception:
            summary = None
        if summary and summary.strip():
            summary = summary.strip()
            return (
                _truncate_text(
                    f"Context archive for session {session_key}:\n{summary}",
                    _HISTORY_ENTRY_HARD_CAP,
                ),
                summary,
            )

        raw = json.dumps(messages, ensure_ascii=False, default=str)
        return (
            _truncate_text(
                "Context archive fallback for session "
                f"{session_key}; summarization failed.\n{raw}",
                _FALLBACK_JSON_CAP,
            ),
            None,
        )

    def _append_history_record(
        self,
        *,
        session_key: str,
        start_index: int,
        end_index: int,
        content: str,
        archived_message_count: int,
    ) -> None:
        cursor = self._next_history_cursor()
        record = {
            "cursor": cursor,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "content": content,
            "session_key": session_key,
            "start_index": start_index,
            "end_index": end_index,
            "archived_message_count": archived_message_count,
        }
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self.cursor_file.write_text(str(cursor), encoding="utf-8")

    def _next_history_cursor(self) -> int:
        last = self._last_history_cursor()
        if last is not None:
            return last + 1
        if self.cursor_file.exists():
            try:
                return int(self.cursor_file.read_text(encoding="utf-8").strip()) + 1
            except (OSError, ValueError):
                pass
        return 1

    def _last_history_cursor(self) -> int | None:
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in reversed(f.read().splitlines()):
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    cursor = record.get("cursor")
                    if isinstance(cursor, int) and not isinstance(cursor, bool):
                        return cursor
                    return None
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return None


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n... (truncated)"
    return text[: max(0, max_chars - len(marker))] + marker
