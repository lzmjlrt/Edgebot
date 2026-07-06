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


def test_tool_call_request_validates_openai_compatible_names() -> None:
    assert ToolCallRequest("call_1", "bash", {}).has_valid_name()
    assert ToolCallRequest("call_2", "mcp_filesystem_read_file", {}).has_valid_name()
    assert ToolCallRequest("call_3", "TodoWrite", {}).has_valid_name()

    assert not ToolCallRequest("bad_empty", "", {}).has_valid_name()
    assert not ToolCallRequest("bad_blank", "   ", {}).has_valid_name()
    assert not ToolCallRequest("bad_space", "read file", {}).has_valid_name()
    assert not ToolCallRequest("bad_dot", "mcp.server.tool", {}).has_valid_name()
    assert not ToolCallRequest("bad_long", "a" * 65, {}).has_valid_name()


def test_tool_call_request_is_valid_checks_id_and_name() -> None:
    # Valid: non-empty id and valid name
    assert ToolCallRequest("call_1", "bash", {}).is_valid()
    assert ToolCallRequest("x", "TodoWrite", {}).is_valid()

    # Invalid: empty id
    assert not ToolCallRequest("", "bash", {}).is_valid()
    assert not ToolCallRequest("", "TodoWrite", {}).is_valid()

    # Invalid: invalid name
    assert not ToolCallRequest("call_1", "", {}).is_valid()
    assert not ToolCallRequest("call_1", "read file", {}).is_valid()

    # Invalid: both empty
    assert not ToolCallRequest("", "", {}).is_valid()


def test_provider_sync_parser_filters_invalid_tool_names() -> None:
    from edgebot.providers.litellm_provider import LiteLLMProvider

    class _FakeToolCall:
        def __init__(self, call_id, name):
            self.id = call_id
            self.function = type("_Fn", (), {"name": name, "arguments": "{}"})()

    class _FakeChoice:
        def __init__(self, tool_calls):
            self.message = type("_Msg", (), {"content": None, "tool_calls": tool_calls or []})()
            self.finish_reason = "tool_calls"

    class _FakeResponse:
        def __init__(self, tool_calls):
            self.choices = [_FakeChoice(tool_calls)]
            self.usage = None

    # All valid
    resp = LiteLLMProvider._parse_sync_response(_FakeResponse([
        _FakeToolCall("id_1", "bash"),
        _FakeToolCall("id_2", "read_file"),
    ]))
    assert len(resp.tool_calls) == 2
    assert resp.tool_calls[0].name == "bash"
    assert resp.tool_calls[1].name == "read_file"

    # All invalid
    resp = LiteLLMProvider._parse_sync_response(_FakeResponse([
        _FakeToolCall("id_1", ""),
        _FakeToolCall("id_2", "bad name"),
    ]))
    assert len(resp.tool_calls) == 0

    # Mixed valid/invalid
    resp = LiteLLMProvider._parse_sync_response(_FakeResponse([
        _FakeToolCall("id_1", ""),
        _FakeToolCall("id_2", "bash"),
        _FakeToolCall("id_3", "bad.name"),
    ]))
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "bash"

    # No tool calls
    resp = LiteLLMProvider._parse_sync_response(_FakeResponse([]))
    assert len(resp.tool_calls) == 0


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


def test_runner_reprompts_without_persisting_malformed_only_tool_calls() -> None:
    executed = False

    def bad_tool():
        nonlocal executed
        executed = True
        raise AssertionError("malformed tool call should not execute")

    provider = CapturingProvider([
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest("bad_call", "", {})],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "please use a tool"}],
        provider=provider,
        tools=[],
        tool_handlers={"": bad_tool},
        model="test-model",
        max_iterations=2,
        emit_output=False,
    )))

    assert executed is False
    assert result.new_messages == [
        {
            "role": "user",
            "content": (
                "The previous model response contained malformed tool calls with "
                "invalid function names. Retry with valid tool names matching "
                "^[A-Za-z0-9_-]{1,64}$, or answer without tools."
            ),
        },
        {"role": "assistant", "content": "done"},
    ]
    assert not any(msg.get("role") == "assistant" and msg.get("tool_calls") for msg in result.new_messages)
    assert "malformed tool calls" in provider.calls[1][-1]["content"]


def test_runner_executes_and_persists_only_valid_mixed_tool_calls() -> None:
    """When tool calls are mixed valid/invalid, none should execute.

    The runner sends a repair prompt listing which names were invalid and
    which were valid, so the model can retry cleanly.
    """
    executed: list[str] = []
    provider = CapturingProvider([
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest("bad_call", "", {}),
                ToolCallRequest("good_call", "known_tool", {}),
                ToolCallRequest("bad_space", "known tool", {}),
            ],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "run tool"}],
        provider=provider,
        tools=[],
        tool_handlers={"known_tool": lambda: executed.append("known_tool") or "ok"},
        model="test-model",
        max_iterations=2,
        emit_output=False,
    )))

    # Mixed valid/invalid: NONE should execute.
    assert executed == []
    # The repair prompt must mention the malformed names and the valid names.
    repair_msg = result.new_messages[0]
    assert repair_msg["role"] == "user"
    assert "known_tool" in repair_msg["content"]
    assert "(empty)" in repair_msg["content"]
    assert "known tool" in repair_msg["content"]
    # No assistant tool_calls persisted.
    assert not any(
        msg.get("role") == "assistant" and msg.get("tool_calls")
        for msg in result.new_messages
    )
    assert result.tool_names_used == []


def test_model_facing_history_strips_malformed_tool_calls_and_results() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old command"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("bad_call", "")]},
        {"role": "tool", "tool_call_id": "bad_call", "name": "", "content": "bad output"},
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

    assert result.messages[:len(messages)] == messages
    request = provider.calls[0]
    assert not any(msg.get("role") == "assistant" and msg.get("tool_calls") for msg in request)
    assert not any(msg.get("role") == "tool" and msg.get("tool_call_id") == "bad_call" for msg in request)
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
