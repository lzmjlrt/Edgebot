import asyncio
from pathlib import Path

from edgebot.agent.memory import DreamProcessor, MemoryStore
from edgebot.providers.base import LLMResponse


class FakeProvider:
    def __init__(self, analysis: str):
        self.analysis = analysis

    async def chat_with_retry(self, **kwargs):
        return LLMResponse(content=self.analysis, finish_reason="stop")


def _processor(tmp_path: Path, analysis: str) -> tuple[MemoryStore, DreamProcessor]:
    store = MemoryStore(tmp_path, memory_dir=tmp_path / ".edgebot" / "memory")
    store.ensure_git_initialized = lambda: True
    store.git.auto_commit = lambda message: "deadbeef"
    store.append_history("User explicitly said they prefer Chinese replies.")
    return store, DreamProcessor(store, FakeProvider(analysis), emit_output=False)


def test_dream_mixed_skip_and_user_finding_still_runs_phase2(monkeypatch, tmp_path: Path) -> None:
    store, processor = _processor(
        tmp_path,
        "[SKIP] transient debug logs\n[USER] prefers Chinese replies",
    )
    phase2_calls: list[str] = []

    async def fake_phase2(self, analysis, user_content, soul_content, memory_content):
        phase2_calls.append(analysis)
        return [{"name": "edit_file", "status": "ok", "detail": "Successfully edited"}]

    monkeypatch.setattr(DreamProcessor, "_phase2_execute", fake_phase2)

    changed = asyncio.run(processor.run([]))

    assert changed is True
    assert phase2_calls == ["[USER] prefers Chinese replies"]
    assert store.get_last_dream_cursor() == 1


def test_dream_only_skip_advances_cursor_without_phase2(monkeypatch, tmp_path: Path) -> None:
    store, processor = _processor(tmp_path, "[skip] transient debug logs")
    phase2_calls: list[str] = []

    async def fake_phase2(self, analysis, user_content, soul_content, memory_content):
        phase2_calls.append(analysis)
        return [{"name": "edit_file", "status": "ok", "detail": "Successfully edited"}]

    monkeypatch.setattr(DreamProcessor, "_phase2_execute", fake_phase2)

    changed = asyncio.run(processor.run([]))

    assert changed is False
    assert phase2_calls == []
    assert store.get_last_dream_cursor() == 1


def test_dream_skip_and_actionable_tags_are_case_insensitive(monkeypatch, tmp_path: Path) -> None:
    store, processor = _processor(
        tmp_path,
        "[sKiP] transient debug logs\n[uSeR] prefers Chinese replies",
    )
    phase2_calls: list[str] = []

    async def fake_phase2(self, analysis, user_content, soul_content, memory_content):
        phase2_calls.append(analysis)
        return [{"name": "edit_file", "status": "ok", "detail": "Successfully edited"}]

    monkeypatch.setattr(DreamProcessor, "_phase2_execute", fake_phase2)

    changed = asyncio.run(processor.run([]))

    assert changed is True
    assert phase2_calls == ["[USER] prefers Chinese replies"]
    assert store.get_last_dream_cursor() == 1
