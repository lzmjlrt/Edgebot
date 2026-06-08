from pathlib import Path

from edgebot.agent.compression import estimate_tokens
from edgebot.agent.runner import AgentRunSpec, _apply_input_token_budget
from edgebot.agent.token_budget import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    CONTEXT_SAFETY_MARGIN_TOKENS,
    consolidation_token_target,
    input_token_budget,
    model_context_window_tokens,
)
from edgebot.session.store import SessionStore


def test_input_budget_uses_model_window_completion_and_margin() -> None:
    assert model_context_window_tokens("openai/gpt-4o") == 128_000
    assert input_token_budget(
        "openai/gpt-4o",
        max_completion_tokens=8_000,
    ) == 128_000 - 8_000 - CONTEXT_SAFETY_MARGIN_TOKENS


def test_unknown_model_uses_safe_default_context_window() -> None:
    assert model_context_window_tokens("local/unknown-model") == DEFAULT_CONTEXT_WINDOW_TOKENS
    assert consolidation_token_target(
        "local/unknown-model",
        max_completion_tokens=4_000,
        consolidation_ratio=0.5,
    ) == int((DEFAULT_CONTEXT_WINDOW_TOKENS - 4_000 - CONTEXT_SAFETY_MARGIN_TOKENS) * 0.5)


def test_get_history_replays_only_unconsolidated_tail_with_message_cap(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    messages = [
        {"role": "user", "content": "archived user"},
        {"role": "assistant", "content": "archived assistant"},
        {"role": "user", "content": "recent user one"},
        {"role": "assistant", "content": "recent assistant one"},
        {"role": "user", "content": "recent user two"},
        {"role": "assistant", "content": "recent assistant two"},
        {"role": "user", "content": "recent user three"},
    ]
    store.save_state("s1", {"messages": messages})
    store.set_last_consolidated("s1", 2)

    history = store.get_history("s1", max_messages=3, max_tokens=10_000)

    assert history == messages[4:]


def test_get_history_replays_tail_under_token_cap(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    large = {"role": "user", "content": "old " + ("x" * 2000)}
    recent = [
        {"role": "user", "content": "latest request"},
        {"role": "assistant", "content": "latest answer"},
    ]
    messages = [
        {"role": "user", "content": "archived"},
        large,
        *recent,
    ]
    store.save_state("s1", {"messages": messages})
    store.set_last_consolidated("s1", 1)

    history = store.get_history(
        "s1",
        max_messages=10,
        max_tokens=estimate_tokens(recent) + 5,
    )

    assert history == recent


def test_get_history_drops_orphan_tool_result_after_truncation(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }
    messages = [
        {"role": "user", "content": "archived"},
        {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "file"},
        {"role": "user", "content": "current request"},
    ]
    store.save_state("s1", {"messages": messages})
    store.set_last_consolidated("s1", 1)

    history = store.get_history("s1", max_messages=2, max_tokens=10_000)

    assert history == [{"role": "user", "content": "current request"}]


def test_runner_input_budget_snips_model_request_copy() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old " + ("x" * 2000)},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current request"},
    ]
    spec = AgentRunSpec(
        initial_messages=messages,
        provider=object(),
        tools=[],
        tool_handlers={},
        model="openai/gpt-4o",
        max_tokens=8_000,
        max_input_tokens=estimate_tokens(messages[-1:]) + 20,
    )

    budgeted = _apply_input_token_budget(messages, spec)

    assert budgeted == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current request"},
    ]
    assert messages[1]["content"].startswith("old ")
