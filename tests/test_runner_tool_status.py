import asyncio

from edgebot.agent import runner
from edgebot.agent.runner import AgentRunSpec, AgentRunner
from edgebot.providers.base import LLMResponse, ToolCallRequest
from edgebot.tools import orchestration
from edgebot.tools.registry import PERMISSIONS


class FakeProvider:
    def __init__(self):
        self.responses = [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest("call_1", "bash", {"command": "python -c pass"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]

    async def chat_with_retry(self, **kwargs):
        return self.responses.pop(0)


class FakeAskProvider:
    def __init__(self):
        self.responses = [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest("call_ask", "ask_user", {
                    "questions": [{
                        "question": "Choose auth?",
                        "header": "Auth",
                        "options": [
                            {"label": "JWT", "description": "Token auth"},
                            {"label": "Session", "description": "Cookie auth"},
                        ],
                    }]
                })],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]

    async def chat_with_retry(self, **kwargs):
        return self.responses.pop(0)


class FakeStatus:
    def __init__(self, events: list[str]):
        self.events = events

    def start(self) -> None:
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")


class FakeConsole:
    def __init__(self):
        self.status_texts: list[str] = []
        self.status_events: list[str] = []

    def status(self, text: str, spinner: str):
        self.status_texts.append(text)
        return FakeStatus(self.status_events)

    def print(self, *args, **kwargs) -> None:
        return None


def test_runner_shows_thinking_status_while_approved_tool_runs(monkeypatch) -> None:
    console = FakeConsole()

    async def prompt_handler(request):
        return {"action": "allow"}

    async def fake_execute_registered_tool(name, params):
        assert name == "bash"
        assert params["command"] == "python -c pass"
        return "ok"

    monkeypatch.setattr(runner, "_console", console)
    monkeypatch.setattr(orchestration, "execute_registered_tool", fake_execute_registered_tool)
    PERMISSIONS.set_prompt_handler(prompt_handler)
    provider = FakeProvider()
    try:
        result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": "run it"}],
            provider=provider,
            tools=[],
            tool_handlers={},
            model="test-model",
            max_iterations=2,
            emit_output=True,
        )))
    finally:
        PERMISSIONS.set_prompt_handler(None)

    assert result.final_content == "done"
    assert console.status_texts == ["[dim]Edgebot is thinking...[/dim]"] * 3
    assert console.status_events == ["start", "stop"] * 3


def test_runner_does_not_show_tool_status_while_ask_user_blocks(monkeypatch) -> None:
    console = FakeConsole()
    seen_questions = []

    async def fake_execute_registered_tool(name, params):
        assert name == "ask_user"
        seen_questions.append(params["questions"][0]["question"])
        return '{"answers":{"Choose auth?":"JWT"},"annotations":{}}'

    monkeypatch.setattr(runner, "_console", console)
    monkeypatch.setattr(orchestration, "execute_registered_tool", fake_execute_registered_tool)
    provider = FakeAskProvider()

    result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "ask first"}],
        provider=provider,
        tools=[],
        tool_handlers={},
        model="test-model",
        max_iterations=2,
        emit_output=True,
    )))

    assert result.final_content == "done"
    assert seen_questions == ["Choose auth?"]
    assert console.status_texts == ["[dim]Edgebot is thinking...[/dim]"] * 2
    assert console.status_events == ["start", "stop"] * 2


def test_runner_executes_structured_ask_user_options(monkeypatch) -> None:
    captured_params = []

    async def fake_execute_registered_tool(name, params):
        captured_params.append((name, params))
        return '{"answers":{"Choose auth?":"JWT"},"annotations":{}}'

    monkeypatch.setattr(orchestration, "execute_registered_tool", fake_execute_registered_tool)
    provider = FakeAskProvider()

    result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "ask first"}],
        provider=provider,
        tools=[],
        tool_handlers={},
        model="test-model",
        max_iterations=2,
        emit_output=False,
    )))

    assert result.final_content == "done"
    assert captured_params == [(
        "ask_user",
        {
            "questions": [{
                "question": "Choose auth?",
                "header": "Auth",
                "options": [
                    {"label": "JWT", "description": "Token auth"},
                    {"label": "Session", "description": "Cookie auth"},
                ],
            }]
        },
    )]
