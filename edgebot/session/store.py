"""
edgebot/session/store.py - JSONL-backed session persistence with metadata.

Each session is stored as a JSONL file. The first line may be a metadata record,
followed by message dicts. This keeps old sessions backward-compatible while
adding enough state to recover interrupted turns.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def find_legal_start(messages: list[dict]) -> int:
    """
    Find the first index where the message list doesn't start with an
    orphan tool result (a tool result whose matching assistant tool_call
    is missing). This prevents invalid API calls after history truncation.
    """
    declared: set[str] = set()
    start = 0
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict):
                    declared.add(tc.get("id", ""))
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id not in declared:
                start = i + 1
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

    def _default_state(self, session_key: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "key": session_key,
            "created_at": now,
            "updated_at": now,
            "metadata": {},
            "messages": [],
        }

    def _serialize_metadata_line(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "_type": "metadata",
            "key": state["key"],
            "created_at": state["created_at"].isoformat(),
            "updated_at": state["updated_at"].isoformat(),
            "metadata": state["metadata"],
        }

    def _restore_state(self, state: dict[str, Any]) -> bool:
        restored = False
        metadata = state["metadata"]
        messages = state["messages"]

        if metadata.get("pending_user_turn"):
            if messages and messages[-1].get("role") == "user":
                messages.append({
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                })
            metadata.pop("pending_user_turn", None)
            restored = True

        checkpoint = metadata.get("runtime_checkpoint")
        if isinstance(checkpoint, dict):
            assistant_message = checkpoint.get("assistant_message")
            completed_tool_results = checkpoint.get("completed_tool_results") or []
            pending_tool_calls = checkpoint.get("pending_tool_calls") or []

            restored_messages: list[dict[str, Any]] = []
            if isinstance(assistant_message, dict):
                restored_messages.append(dict(assistant_message))
            for message in completed_tool_results:
                if isinstance(message, dict):
                    restored_messages.append(dict(message))
            for tool_call in pending_tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                tool_id = tool_call.get("id")
                name = ((tool_call.get("function") or {}).get("name")) or "tool"
                restored_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                })

            existing_keys = {
                (
                    message.get("role"),
                    message.get("content"),
                    message.get("tool_call_id"),
                    message.get("name"),
                    json.dumps(message.get("tool_calls"), ensure_ascii=False, default=str),
                )
                for message in messages
            }
            for message in restored_messages:
                key = (
                    message.get("role"),
                    message.get("content"),
                    message.get("tool_call_id"),
                    message.get("name"),
                    json.dumps(message.get("tool_calls"), ensure_ascii=False, default=str),
                )
                if key not in existing_keys:
                    messages.append(message)
                    existing_keys.add(key)

            metadata.pop("runtime_checkpoint", None)
            restored = True

        if restored:
            state["updated_at"] = datetime.now(timezone.utc)
        return restored

    def _write_state(self, path: Path, state: dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._serialize_metadata_line(state), ensure_ascii=False, default=str) + "\n")
            for msg in state["messages"]:
                f.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")

    def load_state(self, session_key: str) -> dict[str, Any]:
        """Load full state for *session_key*, restoring interrupted turns if needed."""
        path = self._path(session_key)
        if not path.exists():
            return self._default_state(session_key)

        state = self._default_state(session_key)
        messages: list[dict[str, Any]] = []
        metadata_seen = False

        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not metadata_seen and isinstance(data, dict) and data.get("_type") == "metadata":
                metadata_seen = True
                state["key"] = data.get("key") or session_key
                created = data.get("created_at")
                updated = data.get("updated_at")
                if created:
                    try:
                        state["created_at"] = datetime.fromisoformat(created)
                    except ValueError:
                        pass
                if updated:
                    try:
                        state["updated_at"] = datetime.fromisoformat(updated)
                    except ValueError:
                        pass
                raw_meta = data.get("metadata")
                if isinstance(raw_meta, dict):
                    state["metadata"] = raw_meta
                continue

            messages.append(data)

        start = find_legal_start(messages)
        if start:
            messages = messages[start:]
        state["messages"] = messages

        if self._restore_state(state):
            self._write_state(path, state)
        return state

    def load(self, session_key: str) -> list[dict]:
        """Load messages for *session_key*, trimming orphan tool results."""
        return self.load_state(session_key)["messages"]

    def save_state(self, session_key: str, state: dict[str, Any]) -> None:
        """Overwrite the session file with the provided full state."""
        path = self._path(session_key)
        state = {
            "key": state.get("key", session_key),
            "created_at": state.get("created_at", datetime.now(timezone.utc)),
            "updated_at": state.get("updated_at", datetime.now(timezone.utc)),
            "metadata": dict(state.get("metadata", {})),
            "messages": list(state.get("messages", [])),
        }
        self._write_state(path, state)

    def append(self, session_key: str, message: dict) -> None:
        """Append a single message dict to the session file."""
        state = self.load_state(session_key)
        state["messages"].append(message)
        state["updated_at"] = datetime.now(timezone.utc)
        self.save_state(session_key, state)

    def save_all(self, session_key: str, messages: list[dict]) -> None:
        """Overwrite the session file with *messages* (used after compression)."""
        state = self.load_state(session_key)
        state["messages"] = list(messages)
        state["updated_at"] = datetime.now(timezone.utc)
        self.save_state(session_key, state)

    def update_metadata(self, session_key: str, **updates: Any) -> None:
        """Merge keys into session metadata."""
        state = self.load_state(session_key)
        state["metadata"].update(updates)
        state["updated_at"] = datetime.now(timezone.utc)
        self.save_state(session_key, state)

    def clear_metadata_keys(self, session_key: str, *keys: str) -> None:
        """Remove keys from session metadata if present."""
        state = self.load_state(session_key)
        changed = False
        for key in keys:
            if key in state["metadata"]:
                state["metadata"].pop(key, None)
                changed = True
        if changed:
            state["updated_at"] = datetime.now(timezone.utc)
            self.save_state(session_key, state)

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
            try:
                state = self.load_state(f.stem)
            except Exception:
                continue
            sessions.append({
                "key": state["key"],
                "path": str(f),
                "updated_at": state["updated_at"],
                "message_count": len(state["messages"]),
            })
        sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        return sessions
