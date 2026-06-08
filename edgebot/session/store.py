"""
edgebot/session/store.py - JSONL-backed session persistence with metadata.

Each session is stored as a JSONL file. The first line may be a metadata record,
followed by message dicts. This keeps old sessions backward-compatible while
adding enough state to recover interrupted turns.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LAST_CONSOLIDATED_KEY = "last_consolidated"
FILE_MAX_MESSAGES = 2000


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


def _find_history_replay_start(messages: list[dict]) -> int:
    """Find a valid, user-preferred start index for a replay slice."""
    start = find_legal_start(messages)
    for idx in range(start, len(messages)):
        role = messages[idx].get("role")
        if role == "user":
            return idx
        if role == "assistant" and messages[idx].get("tool_calls"):
            return idx
    return start


def _dedup_messages(messages: list[dict]) -> list[dict]:
    """Remove duplicate messages loaded from a session file.

    Rules:
    1. For tool results, keep only the LAST occurrence per tool_call_id.
    2. Drop consecutive messages that are identical (same role + content).
    """
    # Pass 1: deduplicate tool_call_id — keep last occurrence
    last_tc_idx: dict[str, int] = {}
    for i, msg in enumerate(messages):
        tcid = msg.get("tool_call_id")
        if tcid:
            last_tc_idx[tcid] = i
    if last_tc_idx:
        keep_tc = set(last_tc_idx.values())
        messages = [m for i, m in enumerate(messages) if m.get("role") != "tool" or i in keep_tc]

    # Pass 2: drop consecutive identical messages (same role + content)
    deduped: list[dict] = []
    for msg in messages:
        if deduped:
            prev = deduped[-1]
            if (
                msg.get("role") == prev.get("role")
                and msg.get("content") == prev.get("content")
                and msg.get("tool_call_id") == prev.get("tool_call_id")
                and msg.get("tool_calls") == prev.get("tool_calls")
            ):
                continue
        deduped.append(msg)
    return deduped


def _retain_recent_legal_suffix(
    state: dict[str, Any],
    max_messages: int,
) -> tuple[list[dict], int]:
    """Trim state messages to a legal recent suffix.

    Returns (dropped_messages, already_consolidated_dropped_count).
    """
    messages = list(state.get("messages", []))
    metadata = state.setdefault("metadata", {})
    last_consolidated = metadata.get(_LAST_CONSOLIDATED_KEY, 0)
    if isinstance(last_consolidated, bool) or not isinstance(last_consolidated, int):
        last_consolidated = 0
    last_consolidated = max(0, min(last_consolidated, len(messages)))

    if max_messages <= 0:
        dropped = messages
        metadata[_LAST_CONSOLIDATED_KEY] = 0
        state["messages"] = []
        return dropped, min(last_consolidated, len(dropped))
    if len(messages) <= max_messages:
        return [], 0

    original = messages
    retained = list(messages[-max_messages:])

    first_user = next((i for i, msg in enumerate(retained) if msg.get("role") == "user"), None)
    if first_user is not None:
        retained = retained[first_user:]
    else:
        latest_user = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
            None,
        )
        if latest_user is not None:
            retained = list(messages[latest_user: latest_user + max_messages])

    start = find_legal_start(retained)
    if start:
        retained = retained[start:]

    if len(retained) > max_messages:
        retained = retained[-max_messages:]
        start = find_legal_start(retained)
        if start:
            retained = retained[start:]

    retained_ids = {id(message) for message in retained}
    dropped = [message for message in original if id(message) not in retained_ids]
    already_consolidated = sum(
        1
        for index, message in enumerate(original)
        if index < last_consolidated and id(message) not in retained_ids
    )
    new_last_consolidated = sum(
        1
        for index, message in enumerate(original)
        if index < last_consolidated and id(message) in retained_ids
    )

    state["messages"] = retained
    metadata[_LAST_CONSOLIDATED_KEY] = new_last_consolidated
    return dropped, already_consolidated


class SessionStore:
    def __init__(
        self,
        sessions_dir: Path,
        *,
        workspace: Path | None = None,
        legacy_sessions_dir: Path | None = None,
    ):
        self.sessions_dir = sessions_dir
        self.workspace = workspace.resolve() if workspace else None
        self.legacy_sessions_dir = legacy_sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_key: str) -> Path:
        safe = session_key.replace("/", "_").replace(":", "_")
        return self.sessions_dir / f"{safe}.jsonl"

    def _legacy_path(self, session_key: str) -> Path | None:
        if not self.legacy_sessions_dir:
            return None
        safe = session_key.replace("/", "_").replace(":", "_")
        return self.legacy_sessions_dir / f"{safe}.jsonl"

    def _workspace_value(self) -> str | None:
        return str(self.workspace) if self.workspace else None

    def _apply_workspace_metadata(self, state: dict[str, Any]) -> bool:
        workspace_value = self._workspace_value()
        if not workspace_value:
            return False
        metadata = state.setdefault("metadata", {})
        if metadata.get("workspace") == workspace_value:
            return False
        metadata["workspace"] = workspace_value
        return True

    def _normalize_last_consolidated(self, state: dict[str, Any]) -> bool:
        metadata = state.setdefault("metadata", {})
        messages = state.get("messages", [])
        value = metadata.get(_LAST_CONSOLIDATED_KEY, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            value = 0
        value = max(0, min(value, len(messages)))
        if metadata.get(_LAST_CONSOLIDATED_KEY) == value:
            return False
        metadata[_LAST_CONSOLIDATED_KEY] = value
        return True

    def _read_metadata_line(self, path: Path) -> dict[str, Any] | None:
        try:
            with open(path, encoding="utf-8") as f:
                first_line = f.readline().strip()
        except OSError:
            return None
        if not first_line:
            return None
        try:
            data = json.loads(first_line)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict) and data.get("_type") == "metadata":
            return data
        return None

    def _maybe_migrate_legacy_session(self, session_key: str) -> bool:
        if self._path(session_key).exists():
            return False
        legacy_path = self._legacy_path(session_key)
        if legacy_path is None or not legacy_path.exists():
            return False

        metadata = self._read_metadata_line(legacy_path) or {}
        legacy_workspace = (metadata.get("metadata") or {}).get("workspace")
        workspace_value = self._workspace_value()
        if workspace_value and legacy_workspace != workspace_value:
            return False

        try:
            shutil.move(str(legacy_path), str(self._path(session_key)))
            return True
        except Exception:
            return False

    def _migrate_visible_legacy_sessions(self) -> None:
        if not self.legacy_sessions_dir or not self.legacy_sessions_dir.exists():
            return
        workspace_value = self._workspace_value()
        if not workspace_value:
            return

        for legacy_path in self.legacy_sessions_dir.glob("*.jsonl"):
            target_path = self.sessions_dir / legacy_path.name
            if target_path.exists():
                continue
            metadata = self._read_metadata_line(legacy_path) or {}
            legacy_workspace = (metadata.get("metadata") or {}).get("workspace")
            if legacy_workspace != workspace_value:
                continue
            try:
                shutil.move(str(legacy_path), str(target_path))
            except Exception:
                continue

    def _default_state(self, session_key: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        state = {
            "key": session_key,
            "created_at": now,
            "updated_at": now,
            "metadata": {_LAST_CONSOLIDATED_KEY: 0},
            "messages": [],
        }
        self._apply_workspace_metadata(state)
        return state

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
        if isinstance(checkpoint, dict) and checkpoint.get("phase") != "final_response":
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
            self._maybe_migrate_legacy_session(session_key)
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
        messages = _dedup_messages(messages)
        state["messages"] = messages

        touched = self._restore_state(state)
        if self._normalize_last_consolidated(state):
            touched = True
        if self._apply_workspace_metadata(state):
            touched = True
        if touched:
            self._write_state(path, state)
        return state

    def load(self, session_key: str) -> list[dict]:
        """Load messages for *session_key*, trimming orphan tool results."""
        return self.load_state(session_key)["messages"]

    def get_history(
        self,
        session_key: str,
        *,
        max_messages: int | None = None,
        max_tokens: int | None = None,
    ) -> list[dict]:
        """Return the unconsolidated session tail bounded by messages and tokens."""
        from edgebot.agent.compression import estimate_tokens

        state = self.load_state(session_key)
        messages = list(state["messages"])
        start = state["metadata"].get(_LAST_CONSOLIDATED_KEY, 0)
        if isinstance(start, bool) or not isinstance(start, int):
            start = 0
        start = max(0, min(start, len(messages)))

        history = messages[start:]
        if max_messages is not None:
            max_messages = max(0, int(max_messages))
            history = history[-max_messages:] if max_messages else []

        if max_tokens is not None:
            max_tokens = max(0, int(max_tokens))
            selected: list[dict] = []
            for message in reversed(history):
                candidate = [message] + selected
                if selected and estimate_tokens(candidate) > max_tokens:
                    break
                selected = candidate
                if estimate_tokens(selected) > max_tokens:
                    break
            history = selected

        replay_start = _find_history_replay_start(history)
        return [dict(message) for message in history[replay_start:]]

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
        self._normalize_last_consolidated(state)
        self._apply_workspace_metadata(state)
        self._write_state(path, state)

    def get_last_consolidated(self, session_key: str) -> int:
        """Return the message index boundary already archived for this session."""
        state = self.load_state(session_key)
        self._normalize_last_consolidated(state)
        return int(state["metadata"][_LAST_CONSOLIDATED_KEY])

    def set_last_consolidated(self, session_key: str, cursor: int) -> None:
        """Persist the message index boundary already archived for this session."""
        state = self.load_state(session_key)
        state["metadata"][_LAST_CONSOLIDATED_KEY] = cursor
        self._normalize_last_consolidated(state)
        state["updated_at"] = datetime.now(timezone.utc)
        self.save_state(session_key, state)

    def append(self, session_key: str, message: dict) -> None:
        """Append a single message dict to the session file."""
        state = self.load_state(session_key)
        state["messages"].append(message)
        state["updated_at"] = datetime.now(timezone.utc)
        self.save_state(session_key, state)

    def batch_append(self, session_key: str, new_messages: list[dict]) -> None:
        """Append multiple messages in a single load/save cycle."""
        state = self.load_state(session_key)
        state["messages"].extend(new_messages)
        state["updated_at"] = datetime.now(timezone.utc)
        self.save_state(session_key, state)

    def save_all(self, session_key: str, messages: list[dict]) -> None:
        """Overwrite the session file with *messages* (used after compression)."""
        state = self.load_state(session_key)
        state["messages"] = list(messages)
        state["updated_at"] = datetime.now(timezone.utc)
        self.save_state(session_key, state)

    def enforce_file_cap(
        self,
        session_key: str,
        *,
        limit: int = FILE_MAX_MESSAGES,
        on_archive=None,
    ) -> bool:
        """Bound a session file by trimming a legal suffix.

        If provided, on_archive receives dropped messages that were not already
        covered by last_consolidated.
        """
        if limit <= 0:
            return False
        state = self.load_state(session_key)
        if len(state.get("messages", [])) <= limit:
            return False

        dropped, already_consolidated = _retain_recent_legal_suffix(state, limit)
        if not dropped:
            return False

        archive_chunk = dropped[already_consolidated:]
        if archive_chunk and on_archive is not None:
            on_archive(archive_chunk)

        state["updated_at"] = datetime.now(timezone.utc)
        self.save_state(session_key, state)
        return True

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
        self._migrate_visible_legacy_sessions()
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
