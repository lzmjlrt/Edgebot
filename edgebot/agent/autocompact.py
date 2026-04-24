"""
edgebot/agent/autocompact.py - Proactive compression of idle sessions.

When a session has been idle longer than a configurable TTL, AutoCompact
summarizes the old messages via LLM and keeps only a recent suffix. The
summary is injected as context on the next conversation turn so the agent
retains continuity without paying the full token cost.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from rich.console import Console

from edgebot.providers.base import LLMProvider
from edgebot.session.store import SessionStore, find_legal_start

_console = Console()

_RECENT_SUFFIX_MESSAGES = 8
_SUMMARY_PROMPT = (
    "Summarize the following conversation excerpt for continuity.\n"
    "Keep key decisions, code changes, and important context.\n"
    "Be concise — the summary replaces the full excerpt in the agent's context.\n\n"
)


class AutoCompact:
    """Idle-session auto-compression backed by the LLM provider."""

    def __init__(
        self,
        session_store: SessionStore,
        provider: LLMProvider,
        model: str,
        *,
        ttl_minutes: int = 0,
    ):
        self.sessions = session_store
        self.provider = provider
        self.model = model
        self._ttl = ttl_minutes
        self._archiving: set[str] = set()
        self._summaries: dict[str, tuple[str, datetime]] = {}

    def _is_expired(self, ts: datetime | str | None, now: datetime | None = None) -> bool:
        if self._ttl <= 0 or not ts:
            return False
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() >= self._ttl * 60

    @staticmethod
    def _format_summary(text: str, last_active: datetime) -> str:
        now = datetime.now(timezone.utc)
        if last_active.tzinfo is None:
            last_active = last_active.replace(tzinfo=timezone.utc)
        idle_min = int((now - last_active).total_seconds() / 60)
        return (
            f"Session idle for {idle_min} minutes.\n"
            f"Previous conversation summary: {text}"
        )

    def _split_session_tail(
        self, state: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split messages into (archiveable_prefix, retained_recent_suffix)."""
        messages = state.get("messages", [])
        if not messages:
            return [], []

        tail = messages[-_RECENT_SUFFIX_MESSAGES:] if len(messages) > _RECENT_SUFFIX_MESSAGES else list(messages)
        start = find_legal_start(tail)
        tail = tail[start:]

        prefix_len = len(messages) - len(tail)
        return messages[:prefix_len], tail

    def check_expired(
        self,
        schedule_background,
        active_session_keys: set[str] | None = None,
    ) -> None:
        """Schedule archival for idle sessions, skipping those with in-flight tasks."""
        if self._ttl <= 0:
            return
        now = datetime.now(timezone.utc)
        active = active_session_keys or set()
        for info in self.sessions.list_sessions():
            key = info.get("key", "")
            if not key or key in self._archiving or key in active:
                continue
            updated_at = info.get("updated_at")
            if self._is_expired(updated_at, now):
                self._archiving.add(key)
                schedule_background(self._archive(key))

    async def _archive(self, key: str) -> None:
        try:
            state = self.sessions.load_state(key)
            messages = state.get("messages", [])
            if not messages:
                return

            archive_msgs, kept_msgs = self._split_session_tail(state)
            if not archive_msgs:
                state["messages"] = kept_msgs
                state["updated_at"] = datetime.now(timezone.utc)
                self.sessions.save_state(key, state)
                return

            last_active = state.get("updated_at", datetime.now(timezone.utc))
            summary = await self._summarize(archive_msgs)

            if summary and summary != "(nothing)":
                self._summaries[key] = (summary, last_active)
                state.setdefault("metadata", {})["_last_summary"] = {
                    "text": summary,
                    "last_active": last_active.isoformat() if isinstance(last_active, datetime) else str(last_active),
                }

            state["messages"] = kept_msgs
            state["updated_at"] = datetime.now(timezone.utc)
            self.sessions.save_state(key, state)

            _console.print(
                f"[dim]  Auto-compact: archived {key} "
                f"(archived={len(archive_msgs)}, kept={len(kept_msgs)})[/dim]"
            )
        except Exception:
            _console.print_exception()
        finally:
            self._archiving.discard(key)

    async def _summarize(self, messages: list[dict[str, Any]]) -> str | None:
        if not messages:
            return None
        conv_text = json.dumps(messages, ensure_ascii=False, default=str)[:80000]
        try:
            response = await self.provider.chat_with_retry(
                messages=[{"role": "user", "content": _SUMMARY_PROMPT + conv_text}],
                tools=None,
                model=self.model,
                max_tokens=2000,
                temperature=0.3,
            )
            if response.finish_reason == "error":
                return None
            return response.content or None
        except Exception:
            return None

    def prepare_session(
        self, session_key: str,
    ) -> tuple[bool, str | None]:
        """Check whether the session was auto-compacted and return a summary.

        Returns (should_reload, summary_text).
        If should_reload is True the caller must re-load the session from disk.
        """
        if self._ttl <= 0:
            return False, None

        # In-memory summary (hot path — process hasn't restarted)
        entry = self._summaries.pop(session_key, None)
        if entry:
            return False, self._format_summary(entry[0], entry[1])

        # On-disk summary (cold path — process was restarted)
        state = self.sessions.load_state(session_key)
        meta = state.get("metadata", {})
        last_summary = meta.get("_last_summary")
        if isinstance(last_summary, dict) and last_summary.get("text"):
            # Clean up metadata so it doesn't leak permanently
            meta.pop("_last_summary", None)
            state["updated_at"] = datetime.now(timezone.utc)
            self.sessions.save_state(session_key, state)
            try:
                last_active = datetime.fromisoformat(last_summary["last_active"])
            except (ValueError, TypeError):
                last_active = datetime.now(timezone.utc)
            return False, self._format_summary(last_summary["text"], last_active)

        return False, None
