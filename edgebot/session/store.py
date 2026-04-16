"""
edgebot/session/store.py - JSONL-backed session persistence.

Each session is stored as a .jsonl file where every line is one message dict.
Sessions survive restarts and compression rewrites.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def find_legal_start(messages: list[dict]) -> int:
    """
    Find the first index where the message list doesn't start with an
    orphan tool result (a tool result whose matching assistant tool_call
    is missing). This prevents invalid API calls after history truncation.
    """
    declared: set[str] = set()
    start = 0
    for i, msg in enumerate(messages):
        # Track tool_call IDs declared by assistant messages
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict):
                    declared.add(tc.get("id", ""))
        # Check if this tool result is orphaned
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id not in declared:
                start = i + 1
                # Rebuild declared set from start to i
                declared.clear()
                for j in range(start, i + 1):
                    m = messages[j]
                    if m.get("role") == "assistant":
                        for tc in m.get("tool_calls", []):
                            if isinstance(tc, dict):
                                declared.add(tc.get("id", ""))
    return start


class SessionStore:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_key: str) -> Path:
        safe = session_key.replace("/", "_").replace(":", "_")
        return self.sessions_dir / f"{safe}.jsonl"

    def load(self, session_key: str) -> list[dict]:
        """Load messages for *session_key*, trimming orphan tool results."""
        path = self._path(session_key)
        if not path.exists():
            return []
        messages = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        # Trim orphan tool results at the front
        start = find_legal_start(messages)
        if start:
            messages = messages[start:]
        return messages

    def append(self, session_key: str, message: dict) -> None:
        """Append a single message dict to the session file."""
        path = self._path(session_key)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")

    def save_all(self, session_key: str, messages: list[dict]) -> None:
        """Overwrite the session file with *messages* (used after compression)."""
        path = self._path(session_key)
        with open(path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")

    def delete(self, session_key: str) -> bool:
        """Delete a session file. Returns True if deleted."""
        path = self._path(session_key)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_sessions(self) -> list[dict]:
        """
        List all saved sessions, sorted by most recently updated.
        Returns [{key, path, updated_at, message_count}].
        """
        sessions = []
        for f in self.sessions_dir.glob("*.jsonl"):
            key = f.stem
            try:
                stat = f.stat()
                updated = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                count = sum(1 for line in f.read_text(encoding="utf-8").splitlines() if line.strip())
            except Exception:
                continue
            sessions.append({
                "key": key,
                "path": str(f),
                "updated_at": updated,
                "message_count": count,
            })
        sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        return sessions
