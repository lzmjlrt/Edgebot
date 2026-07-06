from edgebot.agent.context_governance import (
    ContextGovernanceConfig,
    prepare_messages_for_model,
)


def _tool_call(call_id: str, name: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def test_context_governance_returns_model_copy_without_mutating_history() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old command"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("bad", "")]},
        {"role": "tool", "tool_call_id": "bad", "name": "", "content": "bad output"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("missing", "bash")]},
        {"role": "user", "content": "continue"},
    ]

    prepared = prepare_messages_for_model(
        messages,
        ContextGovernanceConfig(
            model="test-model",
            max_input_tokens=10_000,
            max_tool_result_tokens=None,
        ),
    )

    assert messages[2]["tool_calls"][0]["function"]["name"] == ""
    assert not any(
        msg.get("role") == "tool" and msg.get("tool_call_id") == "bad"
        for msg in prepared
    )
    assert any(
        msg.get("role") == "tool"
        and msg.get("tool_call_id") == "missing"
        and "unavailable" in msg.get("content", "")
        for msg in prepared
    )
