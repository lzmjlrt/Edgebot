import asyncio
from pathlib import Path

from edgebot.agent.compression import estimate_tokens
from edgebot.agent.runner import AgentRunSpec, AgentRunner, _apply_input_token_budget
from edgebot.providers.base import LLMResponse, ToolCallRequest


class CapturingProvider:
    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs["messages"])
        return self.responses.pop(0)


def _tool_call(call_id: str, name: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def test_runner_offloads_large_non_read_file_tool_result(tmp_path: Path) -> None:
    original_output = "A" * 5000
    provider = CapturingProvider([
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest("call_big", "big_tool", {})],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "run command"}],
        provider=provider,
        tools=[],
        tool_handlers={"big_tool": lambda: original_output},
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=1000,
        session_key="chat/main",
        tool_result_root=tmp_path / ".edgebot" / "tool-results",
        emit_output=False,
    )))

    tool_msg = next(msg for msg in result.messages if msg.get("role") == "tool")
    assert "Tool result offloaded" in tool_msg["content"]
    assert "Original size: 5000 chars" in tool_msg["content"]
    assert "Preview:" in tool_msg["content"]
    assert len(tool_msg["content"]) < 1800

    stored = tmp_path / ".edgebot" / "tool-results" / "chat_main" / "call_big.txt"
    assert stored.read_text(encoding="utf-8") == original_output


def test_runner_tool_result_budget_compacts_old_results_for_model_request() -> None:
    recent_tool_output = "B" * 2000
    old_tool_output = "A" * 2000
    messages = [
        {"role": "user", "content": "old command"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("call_old", "bash")]},
        {"role": "tool", "tool_call_id": "call_old", "name": "bash", "content": old_tool_output},
        {"role": "user", "content": "recent command"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("call_recent", "grep")]},
        {"role": "tool", "tool_call_id": "call_recent", "name": "grep", "content": recent_tool_output},
        {"role": "user", "content": "continue"},
    ]
    provider = CapturingProvider([LLMResponse(content="done", finish_reason="stop")])

    result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=messages,
        provider=provider,
        tools=[],
        tool_handlers={},
        model="test-model",
        max_iterations=1,
        max_tool_result_tokens=estimate_tokens([{"role": "tool", "content": recent_tool_output}]) + 50,
        emit_output=False,
    )))

    request = provider.calls[0]
    old_tool = next(msg for msg in request if msg.get("tool_call_id") == "call_old")
    recent_tool = next(msg for msg in request if msg.get("tool_call_id") == "call_recent")
    assert "omitted from context due to tool-result budget" in old_tool["content"]
    assert recent_tool["content"] == recent_tool_output
    assert result.messages[2]["content"] == old_tool_output


def test_runner_new_messages_exclude_model_facing_backfills() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old command"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("call_lost", "bash")]},
        {"role": "user", "content": "current"},
    ]
    provider = CapturingProvider([LLMResponse(content="final", finish_reason="stop")])

    result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=messages,
        provider=provider,
        tools=[],
        tool_handlers={},
        model="test-model",
        max_iterations=1,
        emit_output=False,
    )))

    assert any(
        msg.get("role") == "tool" and msg.get("tool_call_id") == "call_lost"
        for msg in provider.calls[0]
    )
    assert result.messages[:len(messages)] == messages
    assert result.new_messages == [{"role": "assistant", "content": "final"}]


def test_input_budget_drops_trailing_tool_call_without_result() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "run command"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("call_1", "bash")]},
    ]
    spec = AgentRunSpec(
        initial_messages=messages,
        provider=object(),
        tools=[],
        tool_handlers={},
        model="test-model",
        max_input_tokens=10_000,
    )

    budgeted = _apply_input_token_budget(messages, spec)

    assert budgeted == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "run command"},
    ]
