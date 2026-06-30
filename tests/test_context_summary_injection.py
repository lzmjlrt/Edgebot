import asyncio

from edgebot.agent import context
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

    class FakeProvider:
        class generation:
            temperature = 0.7

    monkeypatch.setattr(loop, "create_provider", lambda: FakeProvider())
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


def test_agent_loop_appends_runner_new_messages_instead_of_slicing(monkeypatch) -> None:
    class FakeResult:
        def __init__(self, messages):
            self.final_content = "final"
            self.messages = messages
            self.new_messages = [{"role": "assistant", "content": "final"}]
            self.tool_names_used = []
            self.usage = {}
            self.stop_reason = "completed"

    class FakeRunner:
        def __init__(self, provider):
            pass

        async def run(self, spec):
            shifted_messages = list(spec.initial_messages)
            shifted_messages.insert(1, {
                "role": "tool",
                "tool_call_id": "call_lost",
                "name": "bash",
                "content": "synthetic backfill",
            })
            shifted_messages.append({"role": "assistant", "content": "final"})
            return FakeResult(shifted_messages)

    class EmptyBackgroundManager:
        def drain(self):
            return []

    class EmptySubagent:
        async def drain(self):
            return []

    class FakeProvider:
        class generation:
            temperature = 0.7

    monkeypatch.setattr(loop, "create_provider", lambda: FakeProvider())
    monkeypatch.setattr(loop, "AgentRunner", FakeRunner)
    monkeypatch.setattr(loop, "SUBAGENT", EmptySubagent())
    monkeypatch.setattr(loop, "_archive_turn_summary", lambda *args, **kwargs: None)

    messages = [{"role": "user", "content": "current"}]
    asyncio.run(loop.agent_loop(
        messages=messages,
        system="base system",
        tools=[],
        tool_handlers={},
        todo_mgr=None,
        bg_mgr=EmptyBackgroundManager(),
        emit_output=False,
    ))

    assert messages == [
        {"role": "user", "content": "current"},
        {"role": "assistant", "content": "final"},
    ]


def test_system_prompt_recent_history_is_scoped_to_session(monkeypatch) -> None:
    class FakeMemory:
        def get_memory_context(self):
            return ""

        def get_last_dream_cursor(self):
            return 0

        def read_unprocessed_history(self, since_cursor, *, session_key=None):
            entries = [
                {
                    "timestamp": "2026-06-08 10:00",
                    "content": "old session detail",
                    "session_key": "s1",
                },
                {
                    "timestamp": "2026-06-08 10:01",
                    "content": "current session detail",
                    "session_key": "s2",
                },
            ]
            if session_key is None:
                return entries
            return [entry for entry in entries if entry.get("session_key") == session_key]

    class FakeSkills:
        def reload(self):
            pass

        def get_always_skills(self):
            return []

        def load_skills_for_context(self, names):
            return ""

        def build_skills_summary(self, exclude=None):
            return "(no skills)"

    from edgebot.agent import memory
    from edgebot.tools import registry

    monkeypatch.setattr(memory, "_STORE", FakeMemory())
    monkeypatch.setattr(registry, "SKILLS", FakeSkills())

    system = context.build_system_prompt(session_key="s2")

    assert "current session detail" in system
    assert "old session detail" not in system
