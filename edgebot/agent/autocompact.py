"""
edgebot/agent/autocompact.py - Proactive compression of idle sessions.

When a session has been idle longer than a configurable TTL, AutoCompact
summarizes the old messages via LLM and keeps only a recent suffix. The
summary is injected as context on the next conversation turn so the agent
retains continuity without paying the full token cost.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from edgebot.agent.consolidator import Consolidator
from edgebot.agent.memory import MemoryStore
from edgebot.providers.base import LLMProvider
from edgebot.session.store import SessionStore

_console = Console()

_RECENT_SUFFIX_MESSAGES = 8


class AutoCompact:
    """Idle-session auto-compression backed by the LLM provider."""

    def __init__(
        self,
        session_store: SessionStore,
        provider: LLMProvider,
        model: str,
        *,
        ttl_minutes: int = 0,
        memory_dir: Path | None = None,
        memory_store: MemoryStore | None = None,
    ):
        self.sessions = session_store
        self.provider = provider
        self.model = model
        self._ttl = ttl_minutes
        self._archiving: set[str] = set()
        self._summaries: dict[str, tuple[str, datetime]] = {}
        self.consolidator = Consolidator(
            session_store=session_store,
            provider=provider,
            model=model,
            memory_dir=memory_dir,
            memory_store=memory_store,
            keep_recent_messages=_RECENT_SUFFIX_MESSAGES,
        )

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
            summary = await self.consolidator.compact_idle_session(
                key,
                _RECENT_SUFFIX_MESSAGES,
            )
            state = self.sessions.load_state(key)
            last_summary = state.get("metadata", {}).get("_last_summary")
            if isinstance(last_summary, dict) and last_summary.get("text"):
                try:
                    last_active = datetime.fromisoformat(str(last_summary["last_active"]))
                except (ValueError, TypeError):
                    last_active = datetime.now(timezone.utc)
                self._summaries[key] = (str(last_summary["text"]), last_active)

            _console.print(
                f"[dim]  Auto-compact: archived {key} "
                f"(summary={bool(summary)})[/dim]"
            )
        except Exception:
            _console.print_exception()
        finally:
            self._archiving.discard(key)

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
