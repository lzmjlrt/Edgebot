"""Read-only memory recall and audited Dream queue tools."""

from __future__ import annotations

from typing import Any

from edgebot.agent.memory.store import MemoryStore
from edgebot.config import WORKDIR
from edgebot.tools.base import BaseTool


class RecallMemoryTool(BaseTool):
    """Search only the current workspace's runtime memory store."""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self.store = store or MemoryStore(WORKDIR)

    @property
    def name(self) -> str:
        return "recall_memory"

    @property
    def description(self) -> str:
        return "Search relevant durable memory topics and archived excerpts for this workspace."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to recall."},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum topic files to return (1-5).",
                    "minimum": 1,
                    "maximum": 5,
                },
            },
            "required": ["query"],
        }

    def is_read_only(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def execute(self, *, query: str, max_results: int = 5) -> dict[str, list[dict[str, Any]]]:
        return self.store.recall_memory(query, max_results=max_results)


class RememberMemoryTool(BaseTool):
    """Queue explicitly requested durable facts for the restricted Dream flow."""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self.store = store or MemoryStore(WORKDIR)
        self._session_key: str | None = None

    @property
    def name(self) -> str:
        return "remember"

    @property
    def description(self) -> str:
        return "Queue an explicit durable-memory request for Dream; it never edits memory directly."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The durable fact or preference to remember."},
            },
            "required": ["content"],
        }

    def set_runtime_context(self, *, session_key: str | None = None, **_: Any) -> None:
        self._session_key = session_key

    def execute(self, *, content: str) -> dict[str, Any]:
        cleaned = content.strip()
        if not cleaned:
            return {"queued": False, "error": "Memory content cannot be empty."}
        cursor = self.store.queue_remember(cleaned, session_key=self._session_key)
        return {"queued": True, "cursor": cursor}
