"""
edgebot/cli/session_resume.py - Session loading for /resume.
"""

from edgebot.agent.compression import extract_session_summary
from edgebot.session.store import SessionStore


def _resolve_session_summary(state: dict) -> str | None:
    """Prefer explicit session metadata, then fall back to compacted history."""
    metadata = state.get("metadata", {})
    summary = metadata.get("session_summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return extract_session_summary(state.get("messages", []))


def load_session_for_resume(
    store: SessionStore,
    session_key: str,
) -> tuple[list[dict], str | None]:
    """Load persisted session history and summary for /resume."""
    state = store.load_state(session_key)
    return list(state["messages"]), _resolve_session_summary(state)
