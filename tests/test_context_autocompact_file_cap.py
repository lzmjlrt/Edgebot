import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from edgebot.cli.repl import load_session_for_resume
from edgebot.agent.autocompact import AutoCompact
from edgebot.agent.consolidator import Consolidator
from edgebot.providers.base import LLMResponse
from edgebot.session.store import SessionStore


class FakeProvider:
    async def chat_with_retry(self, **kwargs):
        return LLMResponse(content="- idle work summarized", finish_reason="stop")


def _messages(count: int) -> list[dict]:
    roles = ("user", "assistant")
    return [
        {"role": roles[index % 2], "content": f"message {index}"}
        for index in range(count)
    ]


def test_autocompact_archives_idle_session_with_consolidator(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    state = store.load_state("s1")
    state["messages"] = _messages(12)
    state["updated_at"] = datetime.now(timezone.utc) - timedelta(minutes=30)
    store.save_state("s1", state)

    autocompact = AutoCompact(
        session_store=store,
        provider=FakeProvider(),
        model="test-model",
        ttl_minutes=1,
        memory_dir=tmp_path / "memory",
    )

    asyncio.run(autocompact._archive("s1"))

    loaded = store.load_state("s1")
    assert loaded["messages"] == _messages(12)[4:]
    assert loaded["metadata"]["last_consolidated"] == 0
    assert loaded["metadata"]["session_summary"] == "- idle work summarized"
    assert loaded["metadata"]["_last_summary"]["text"] == "- idle work summarized"

    records = [
        json.loads(line)
        for line in (tmp_path / "memory" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["session_key"] == "s1"
    assert records[0]["archived_message_count"] == 4
    assert "- idle work summarized" in records[0]["content"]


def test_consolidator_file_cap_archives_only_unconsolidated_dropped_messages(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    messages = _messages(7)
    store.save_state("s1", {"messages": messages})
    store.set_last_consolidated("s1", 2)

    changed = Consolidator(
        session_store=store,
        memory_dir=tmp_path / "memory",
    ).enforce_session_file_cap("s1", max_messages=4)

    loaded = store.load_state("s1")
    assert changed is True
    assert loaded["messages"] == messages[4:]
    assert loaded["metadata"]["last_consolidated"] == 0

    records = [
        json.loads(line)
        for line in (tmp_path / "memory" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["session_key"] == "s1"
    assert records[0]["archived_message_count"] == 2
    assert "message 2" in records[0]["content"]
    assert "message 3" in records[0]["content"]
    assert "message 0" not in records[0]["content"]


def test_resume_loads_idle_session_without_compacting(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    messages = _messages(14)
    state = store.load_state("s1")
    state["messages"] = messages
    state["metadata"]["session_summary"] = "- existing summary"
    state["updated_at"] = datetime.now(timezone.utc) - timedelta(hours=2)
    store.save_state("s1", state)

    history, summary = load_session_for_resume(store, "s1")

    assert history == messages
    assert summary == "- existing summary"
    assert store.load_state("s1")["messages"] == messages
