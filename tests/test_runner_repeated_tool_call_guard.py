import asyncio

from edgebot.agent import runner
from edgebot.agent.runner import AgentRunSpec, AgentRunner
from edgebot.providers.base import LLMResponse, ToolCallRequest
from edgebot.tools import orchestration


class SequenceProvider:
    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("provider called too many times")
        return self.responses.pop(0)


def _read_call(call_id: str, *, offset: int = 120, limit: int = 40) -> ToolCallRequest:
    return ToolCallRequest(
        call_id,
        "read_file",
        {"path": "/testbed/django/db/models/deletion.py", "offset": offset, "limit": limit},
    )


def _run_with_provider(monkeypatch, provider: SequenceProvider, *, max_iterations: int = 10):
    executed: list[dict] = []

    async def execute_registered_tool(name, kwargs):
        assert name == "read_file"
        executed.append(kwargs)
        return "[File unchanged since last read: /testbed/django/db/models/deletion.py]"

    monkeypatch.setattr(orchestration, "execute_registered_tool", execute_registered_tool)

    result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "fix the bug"}],
        provider=provider,
        tools=[],
        tool_handlers={},
        model="test-model",
        max_iterations=max_iterations,
        emit_output=False,
    )))
    return result, executed


def test_tool_call_signature_ignores_id_and_normalizes_dict_order() -> None:
    first = runner._tool_call_signature(
        "read_file",
        {"path": "a.py", "offset": 10, "limit": 5},
    )
    reordered = runner._tool_call_signature(
        "read_file",
        {"limit": 5, "path": "a.py", "offset": 10},
    )
    different_range = runner._tool_call_signature(
        "read_file",
        {"path": "a.py", "offset": 20, "limit": 5},
    )
    different_tool = runner._tool_call_signature(
        "grep",
        {"path": "a.py", "offset": 10, "limit": 5},
    )

    assert first == reordered
    assert first != different_range
    assert first != different_tool

    batch_a = runner._tool_call_batch_signature([_read_call("call_a")])
    batch_b = runner._tool_call_batch_signature([_read_call("call_b")])
    assert batch_a == batch_b


def test_runner_injects_repair_prompt_instead_of_executing_third_identical_call(monkeypatch) -> None:
    provider = SequenceProvider([
        LLMResponse(content=None, tool_calls=[_read_call("call_1")], finish_reason="tool_calls"),
        LLMResponse(content=None, tool_calls=[_read_call("call_2")], finish_reason="tool_calls"),
        LLMResponse(content=None, tool_calls=[_read_call("call_3")], finish_reason="tool_calls"),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    result, executed = _run_with_provider(monkeypatch, provider)

    assert result.stop_reason == "completed"
    assert result.final_content == "done"
    assert len(executed) == 2
    repair_message = result.new_messages[-2]
    assert repair_message["role"] == "user"
    assert "same read_file range" in repair_message["content"]
    assert not any(
        msg.get("role") == "assistant"
        and any(tc["id"] == "call_3" for tc in msg.get("tool_calls", []))
        for msg in result.new_messages
    )
    assert not any(msg.get("tool_call_id") == "call_3" for msg in result.new_messages)
    assert result.telemetry["tool_call_count"] == 3
    assert result.telemetry["read_file_count"] == 3
    assert result.telemetry["max_identical_tool_call_run"] == 3
    assert result.telemetry["repeated_tool_call_repairs"] == 1
    assert result.telemetry["repeated_tool_call_stops"] == 0


def test_runner_stops_repeated_tool_call_and_finalizes_without_tools(monkeypatch) -> None:
    provider = SequenceProvider([
        LLMResponse(content=None, tool_calls=[_read_call("call_1")], finish_reason="tool_calls"),
        LLMResponse(content=None, tool_calls=[_read_call("call_2")], finish_reason="tool_calls"),
        LLMResponse(content=None, tool_calls=[_read_call("call_3")], finish_reason="tool_calls"),
        LLMResponse(content=None, tool_calls=[_read_call("call_4")], finish_reason="tool_calls"),
        LLMResponse(content=None, tool_calls=[_read_call("call_5")], finish_reason="tool_calls"),
        LLMResponse(content="stopped summary", finish_reason="stop"),
    ])

    result, executed = _run_with_provider(monkeypatch, provider)

    assert result.stop_reason == "repeated_tool_call"
    assert result.final_content == "stopped summary"
    assert len(executed) == 2
    assert provider.calls[-1]["tools"] is None
    assert result.telemetry["tool_call_count"] == 5
    assert result.telemetry["read_file_count"] == 5
    assert result.telemetry["max_identical_tool_call_run"] == 5
    assert result.telemetry["repeated_tool_call_repairs"] == 2
    assert result.telemetry["repeated_tool_call_stops"] == 1
    assert not any(
        msg.get("role") == "assistant"
        and any(tc["id"] in {"call_3", "call_4", "call_5"} for tc in msg.get("tool_calls", []))
        for msg in result.new_messages
    )


def test_runner_allows_recovery_after_repeated_tool_call_repair_prompt(monkeypatch) -> None:
    provider = SequenceProvider([
        LLMResponse(content=None, tool_calls=[_read_call("call_1")], finish_reason="tool_calls"),
        LLMResponse(content=None, tool_calls=[_read_call("call_2")], finish_reason="tool_calls"),
        LLMResponse(content=None, tool_calls=[_read_call("call_3")], finish_reason="tool_calls"),
        LLMResponse(content=None, tool_calls=[_read_call("call_4", offset=160)], finish_reason="tool_calls"),
        LLMResponse(content="done after recovery", finish_reason="stop"),
    ])

    result, executed = _run_with_provider(monkeypatch, provider)

    assert result.stop_reason == "completed"
    assert result.final_content == "done after recovery"
    assert len(executed) == 3
    assert executed[-1]["offset"] == 160
