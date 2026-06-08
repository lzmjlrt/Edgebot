import asyncio

from edgebot.agent import loop
from edgebot.agent.runner import AgentRunResult


def test_agent_loop_injects_session_summary_as_system_context(monkeypatch) -> None:
    captured: dict[str, list[dict]] = {}

    class FakeRunner:
        def __init__(self, provider):
            pass

        async def run(self, spec):
            captured["messages"] = spec.initial_messages
            return AgentRunResult(
                final_content="",
                messages=list(spec.initial_messages),
            )

    class EmptyBackgroundManager:
        def drain(self):
            return []

    class EmptySubagent:
        async def drain(self):
            return []

    monkeypatch.setattr(loop, "create_provider", lambda: object())
    monkeypatch.setattr(loop, "AgentRunner", FakeRunner)
    monkeypatch.setattr(loop, "SUBAGENT", EmptySubagent())

    asyncio.run(loop.agent_loop(
        messages=[{"role": "user", "content": "continue"}],
        system="base system",
        tools=[],
        tool_handlers={},
        todo_mgr=None,
        bg_mgr=EmptyBackgroundManager(),
        session_summary="- archived facts",
        emit_output=False,
    ))

    assert captured["messages"][0] == {
        "role": "system",
        "content": "base system\n\n## Session Summary\n\n- archived facts",
    }
    user_content = captured["messages"][1]["content"]
    assert "[Runtime Context - metadata only, not instructions]" in user_content
    assert "- archived facts" not in user_content
