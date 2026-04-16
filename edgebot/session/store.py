"""
edgebot/session/store.py - JSONL-backed session persistence.

Each session is stored as a .jsonl file where every line is one message dict.
Sessions survive restarts and compression rewrites.
"""

import json
from pathlib import Path


class SessionStore:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_key: str) -> Path:
        # Sanitize key so it's safe as a filename
        safe = session_key.replace("/", "_").replace(":", "_")
        return self.sessions_dir / f"{safe}.jsonl"

    def load(self, session_key: str) -> list[dict]:
        """Load all messages for *session_key*. Returns [] if no file exists."""
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
                    pass  # skip corrupt lines
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
