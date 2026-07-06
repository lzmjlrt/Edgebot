import asyncio
import time
from pathlib import Path

import pytest

from edgebot.agent.runner import AgentRunSpec, AgentRunner
from edgebot.providers.base import LLMResponse, ToolCallRequest
from edgebot.tools import file_state
import edgebot.tools.base as tool_base
from edgebot.tools.filesystem import run_edit, run_read


class SequencedProvider:
    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)

    async def chat_with_retry(self, **kwargs):
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def isolated_file_state(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(tool_base, "WORKDIR", tmp_path)
    file_state.clear()
    yield tmp_path
    file_state.clear()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_bound_sessions_keep_read_dedup_separate(isolated_file_state: Path) -> None:
    _write(isolated_file_state / "notes.txt", "alpha")
    store = file_state.FileStateStore()
    session_a = store.for_session("a")
    session_b = store.for_session("b")

    token = file_state.bind_file_states(session_a)
    try:
        assert "alpha" in run_read("notes.txt")
        assert run_read("notes.txt").startswith("[File unchanged")
    finally:
        file_state.reset_file_states(token)

    token = file_state.bind_file_states(session_b)
    try:
        assert "alpha" in run_read("notes.txt")
        assert not run_read("notes.txt", force=True).startswith("[File unchanged")
    finally:
        file_state.reset_file_states(token)


def test_bound_session_read_does_not_satisfy_edit_warning_for_another_session(
    isolated_file_state: Path,
) -> None:
    _write(isolated_file_state / "notes.txt", "alpha")
    store = file_state.FileStateStore()

    token = file_state.bind_file_states(store.for_session("reader"))
    try:
        assert "alpha" in run_read("notes.txt")
    finally:
        file_state.reset_file_states(token)

    token = file_state.bind_file_states(store.for_session("editor"))
    try:
        result = run_edit("notes.txt", "alpha", "beta")
    finally:
        file_state.reset_file_states(token)

    assert result.startswith("Warning: file has not been read yet")
    assert "Successfully edited" in result


def test_check_read_allows_identical_content_after_mtime_change(
    isolated_file_state: Path,
) -> None:
    target = isolated_file_state / "notes.txt"
    _write(target, "alpha")

    assert "alpha" in run_read("notes.txt")
    time.sleep(0.01)
    target.write_text("alpha", encoding="utf-8")

    assert file_state.check_read(target) is None


def test_check_read_warns_when_content_changes(isolated_file_state: Path) -> None:
    target = isolated_file_state / "notes.txt"
    _write(target, "alpha")

    assert "alpha" in run_read("notes.txt")
    target.write_text("changed", encoding="utf-8")

    assert file_state.check_read(target) == (
        "Warning: file has been modified since last read. "
        "Re-read to verify content before editing."
    )


def _read_then_stop_provider() -> SequencedProvider:
    return SequencedProvider([
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest("call_read", "read_file", {"path": "notes.txt"})],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])


def _run_read_turn(session_key: str) -> str:
    provider = _read_then_stop_provider()
    result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "read notes"}],
        provider=provider,
        tools=[],
        tool_handlers={},
        model="test-model",
        max_iterations=2,
        session_key=session_key,
        emit_output=False,
    )))
    return next(
        str(message["content"])
        for message in result.new_messages
        if message.get("role") == "tool"
    )


def test_runner_file_state_persists_for_same_session_across_turns(
    isolated_file_state: Path,
) -> None:
    _write(isolated_file_state / "notes.txt", "alpha")

    assert "alpha" in _run_read_turn("same")
    assert _run_read_turn("same").startswith("[File unchanged")


def test_runner_file_state_does_not_cross_session_keys(
    isolated_file_state: Path,
) -> None:
    _write(isolated_file_state / "notes.txt", "alpha")

    assert "alpha" in _run_read_turn("one")
    second = _run_read_turn("two")

    assert "alpha" in second
    assert not second.startswith("[File unchanged")


def test_concurrent_runner_file_state_does_not_cross_session_keys(
    isolated_file_state: Path,
) -> None:
    _write(isolated_file_state / "notes.txt", "alpha")

    async def run_turn(session_key: str) -> str:
        provider = _read_then_stop_provider()
        result = await AgentRunner(provider).run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": "read notes"}],
            provider=provider,
            tools=[],
            tool_handlers={},
            model="test-model",
            max_iterations=2,
            session_key=session_key,
            emit_output=False,
        ))
        return next(
            str(message["content"])
            for message in result.new_messages
            if message.get("role") == "tool"
        )

    async def run_both() -> tuple[str, str]:
        first, second = await asyncio.gather(
            run_turn("parallel-one"),
            run_turn("parallel-two"),
        )
        return first, second

    first, second = asyncio.run(run_both())

    assert "alpha" in first
    assert "alpha" in second
    assert not first.startswith("[File unchanged")
    assert not second.startswith("[File unchanged")
