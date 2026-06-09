"""
edgebot/agent/consolidator.py - Incremental session history archiving.

Consolidator records older, stable conversation slices into memory/history.jsonl
and advances a per-session message-index cursor. It does not remove messages
from the session; later context-replay code can use the cursor to decide what
to send to the model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edgebot.agent.compression import (
    estimate_tokens,
    merge_session_summaries,
    summarize_messages,
)
from edgebot.agent.memory import MemoryStore
from edgebot.session.store import FILE_MAX_MESSAGES, SessionStore

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
        memory_store: MemoryStore | None = None,
        keep_recent_messages: int = 8,
    ) -> None:
        if memory_store is None and memory_dir is None:
            from edgebot.config import MEMORY_DIR
            memory_dir = MEMORY_DIR
        self.sessions = session_store
        self.provider = provider
        self.model = model
        self.memory_store = memory_store or MemoryStore(
            Path(memory_dir).parent,
            memory_dir=Path(memory_dir),
        )
        self.memory_dir = self.memory_store.memory_dir
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

    async def compact_idle_session(
        self,
        session_key: str,
        max_suffix: int = 8,
    ) -> str | None:
        """Archive an idle session's unconsolidated prefix and keep a legal tail."""
        from edgebot.session.store import _retain_recent_legal_suffix

        state = self.sessions.load_state(session_key)
        messages = list(state.get("messages", []))
        start = self.sessions.get_last_consolidated(session_key)
        tail = messages[start:]
        if not tail:
            state["updated_at"] = datetime.now(timezone.utc)
            self.sessions.save_state(session_key, state)
            return ""

        probe = {
            "messages": tail,
            "metadata": {"last_consolidated": 0},
        }
        dropped, already_consolidated = _retain_recent_legal_suffix(
            probe,
            max_suffix,
        )
        kept = list(probe.get("messages", []))
        archive_messages = dropped[already_consolidated:]
        if not archive_messages and not kept:
            state["messages"] = []
            state.setdefault("metadata", {})["last_consolidated"] = 0
            state["updated_at"] = datetime.now(timezone.utc)
            self.sessions.save_state(session_key, state)
            return ""

        last_active = state.get("updated_at", datetime.now(timezone.utc))
        summary: str | None = ""
        if archive_messages:
            content, summary = await self._build_archive_content(
                session_key,
                archive_messages,
            )
            self._append_history_record(
                session_key=session_key,
                start_index=start,
                end_index=start + len(archive_messages),
                content=content,
                archived_message_count=len(archive_messages),
            )

        metadata = state.setdefault("metadata", {})
        if summary and summary != "(nothing)":
            metadata["session_summary"] = merge_session_summaries(
                metadata.get("session_summary"),
                summary,
            )
            metadata["_last_summary"] = {
                "text": summary,
                "last_active": (
                    last_active.isoformat()
                    if isinstance(last_active, datetime)
                    else str(last_active)
                ),
            }

        state["messages"] = kept
        metadata["last_consolidated"] = 0
        state["updated_at"] = datetime.now(timezone.utc)
        self.sessions.save_state(session_key, state)
        return summary

    def enforce_session_file_cap(
        self,
        session_key: str,
        *,
        max_messages: int = FILE_MAX_MESSAGES,
    ) -> bool:
        """Trim an oversized session file and raw-archive newly dropped history."""
        return self.sessions.enforce_file_cap(
            session_key,
            limit=max_messages,
            on_archive=lambda chunk: self.raw_archive_messages(
                session_key,
                chunk,
                reason="session file cap",
            ),
        )

    def raw_archive_messages(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
        *,
        reason: str = "raw archive",
    ) -> None:
        """Append raw messages to history.jsonl when no LLM summary is needed."""
        if not messages:
            return
        raw = json.dumps(messages, ensure_ascii=False, default=str)
        content = _truncate_text(
            f"Context archive fallback for session {session_key}; {reason}.\n{raw}",
            _FALLBACK_JSON_CAP,
        )
        self._append_history_record(
            session_key=session_key,
            start_index=-1,
            end_index=-1,
            content=content,
            archived_message_count=len(messages),
        )

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
        self.memory_store.append_history(
            content,
            session_key=session_key,
            max_chars=_HISTORY_ENTRY_HARD_CAP,
            metadata={
                "start_index": start_index,
                "end_index": end_index,
                "archived_message_count": archived_message_count,
            },
        )


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n... (truncated)"
    return text[: max(0, max_chars - len(marker))] + marker
