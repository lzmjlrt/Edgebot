import asyncio
import json
from pathlib import Path

from edgebot.agent import loop
from edgebot.agent.consolidator import Consolidator
from edgebot.agent.memory import DreamProcessor, MemoryStore
from edgebot.providers.base import LLMResponse
from edgebot.session.store import SessionStore


class CapturingProvider:
    def __init__(self, analysis: str):
        self.analysis = analysis
        self.prompts: list[str] = []

    async def chat_with_retry(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        return LLMResponse(content=self.analysis, finish_reason="stop")


def _history_records(memory_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (memory_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_history_writers_mark_source_and_tags(monkeypatch, tmp_path: Path) -> None:
    memory_store = MemoryStore(tmp_path, memory_dir=tmp_path / ".edgebot" / "memory")
    monkeypatch.setattr(loop, "_MEMORY", memory_store)

    loop._archive_turn_summary(
        [{"role": "user", "content": "remember that the project ships weekly"}],
        "noted",
        session_key="s1",
    )

    session_store = SessionStore(tmp_path / "sessions")
    session_store.save_state(
        "s1",
        {
            "messages": [
                {"role": "user", "content": "old request"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "recent request"},
                {"role": "assistant", "content": "recent answer"},
            ],
        },
    )
    consolidator = Consolidator(
        session_store=session_store,
        provider=CapturingProvider("- project ships weekly"),
        model="test-model",
        memory_store=memory_store,
        keep_recent_messages=2,
    )
    assert asyncio.run(
        consolidator.maybe_consolidate_by_tokens("s1", max_unconsolidated_tokens=1)
    )
    consolidator.raw_archive_messages(
        "s1",
        [{"role": "user", "content": "fallback-only transcript"}],
        reason="session file cap",
    )

    records = _history_records(memory_store.memory_dir)

    assert records[0]["source"] == "turn_summary"
    assert records[0]["tags"] == ["summary"]
    assert records[1]["source"] == "context_archive"
    assert records[1]["tags"] == ["durable"]
    assert records[2]["source"] == "raw_archive"
    assert records[2]["tags"] == ["ephemeral"]


def test_dream_prompt_shows_history_source_and_tags(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, memory_dir=tmp_path / ".edgebot" / "memory")
    store.ensure_git_initialized = lambda: True
    store.append_history(
        "Project ships weekly.",
        session_key="s1",
        metadata={"source": "context_archive", "tags": ["durable"]},
    )
    provider = CapturingProvider("[SKIP] already covered")
    processor = DreamProcessor(store, provider, emit_output=False)

    changed = asyncio.run(processor.run([]))

    assert changed is False
    prompt = provider.prompts[0]
    assert "[source=context_archive tags=durable session=s1 cursor=1]" in prompt
    assert "Project ships weekly." in prompt


def test_dream_deduplicates_repeated_findings_before_phase2(monkeypatch, tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, memory_dir=tmp_path / ".edgebot" / "memory")
    store.ensure_git_initialized = lambda: True
    store.git.auto_commit = lambda message: "deadbeef"
    store.append_history(
        "User: project ships weekly",
        metadata={"source": "turn_summary", "tags": ["summary"]},
    )
    store.append_history(
        "Context archive for session s1:\n- project ships weekly",
        metadata={"source": "context_archive", "tags": ["durable"]},
    )
    processor = DreamProcessor(
        store,
        CapturingProvider(
            "[MEMORY] Project ships weekly\n"
            "[MEMORY] Project ships weekly\n"
            "[memory] project ships weekly"
        ),
        emit_output=False,
    )
    phase2_calls: list[str] = []

    async def fake_phase2(self, analysis, user_content, soul_content, memory_content):
        phase2_calls.append(analysis)
        return [{"name": "edit_file", "status": "ok", "detail": "Successfully edited"}]

    monkeypatch.setattr(DreamProcessor, "_phase2_execute", fake_phase2)

    assert asyncio.run(processor.run([])) is True

    assert phase2_calls == ["[MEMORY] Project ships weekly"]
    assert store.get_last_dream_cursor() == 2


def test_dream_skips_ephemeral_raw_history_but_advances_cursor(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, memory_dir=tmp_path / ".edgebot" / "memory")
    store.ensure_git_initialized = lambda: True
    store.append_history(
        "Raw fallback transcript: user mentioned a transient crash.",
        metadata={"source": "raw_archive", "tags": ["ephemeral"]},
    )
    provider = CapturingProvider("[MEMORY] should not be called")
    processor = DreamProcessor(store, provider, emit_output=False)

    changed = asyncio.run(processor.run([]))

    assert changed is False
    assert provider.prompts == []
    assert store.get_last_dream_cursor() == 1
