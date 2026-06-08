import asyncio
import json
from pathlib import Path

from edgebot.providers.base import LLMResponse
from edgebot.session.store import SessionStore


def _tool_call(call_id: str, name: str = "read_file") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def test_session_store_persists_last_consolidated_cursor(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)

    state = store.load_state("s1")
    assert state["metadata"]["last_consolidated"] == 0

    state["messages"] = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    store.save_state("s1", state)
    store.set_last_consolidated("s1", 2)

    loaded = store.load_state("s1")
    assert loaded["metadata"]["last_consolidated"] == 2
    assert store.get_last_consolidated("s1") == 2


def test_consolidator_archives_incremental_prefix_at_user_boundary(tmp_path: Path) -> None:
    from edgebot.agent.consolidator import Consolidator

    class FakeProvider:
        async def chat_with_retry(self, **kwargs):
            return LLMResponse(content="- old work summarized", finish_reason="stop")

    store = SessionStore(tmp_path / "sessions")
    messages = [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "tool request"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("call_1")]},
        {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "file contents"},
        {"role": "user", "content": "recent request"},
        {"role": "assistant", "content": "recent answer"},
    ]
    store.save_state("s1", {"messages": messages})

    consolidator = Consolidator(
        session_store=store,
        provider=FakeProvider(),
        model="test-model",
        memory_dir=tmp_path / "memory",
        keep_recent_messages=2,
    )

    changed = asyncio.run(consolidator.maybe_consolidate_by_tokens(
        "s1",
        max_unconsolidated_tokens=1,
    ))

    assert changed is True
    loaded = store.load_state("s1")
    assert loaded["messages"] == messages
    assert loaded["metadata"]["last_consolidated"] == 5

    records = [
        json.loads(line)
        for line in (tmp_path / "memory" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["session_key"] == "s1"
    assert records[0]["start_index"] == 0
    assert records[0]["end_index"] == 5
    assert records[0]["archived_message_count"] == 5
    assert "- old work summarized" in records[0]["content"]


def test_consolidator_skips_boundary_that_would_split_tool_calls(tmp_path: Path) -> None:
    from edgebot.agent.consolidator import Consolidator

    class FakeProvider:
        async def chat_with_retry(self, **kwargs):
            return LLMResponse(content="should not be used", finish_reason="stop")

    store = SessionStore(tmp_path / "sessions")
    store.save_state("s1", {
        "messages": [
            {"role": "user", "content": "old request"},
            {"role": "assistant", "content": None, "tool_calls": [_tool_call("call_1")]},
            {"role": "user", "content": "new request"},
            {"role": "assistant", "content": "new answer"},
        ],
    })

    consolidator = Consolidator(
        session_store=store,
        provider=FakeProvider(),
        model="test-model",
        memory_dir=tmp_path / "memory",
        keep_recent_messages=2,
    )

    changed = asyncio.run(consolidator.maybe_consolidate_by_tokens(
        "s1",
        max_unconsolidated_tokens=1,
    ))

    assert changed is False
    assert store.get_last_consolidated("s1") == 0
    assert not (tmp_path / "memory" / "history.jsonl").exists()
