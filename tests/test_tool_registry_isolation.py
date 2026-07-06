import asyncio
import json
from typing import Any

from edgebot.agent.runner import AgentRunSpec, AgentRunner
from edgebot.providers.base import LLMResponse, ToolCallRequest
from edgebot.subagent.runner import SubagentRunner
from edgebot.tools.base import BaseTool
from edgebot.tools.registry import ToolRegistry, build_runtime_tool_registry
from edgebot.tools import orchestration


class EchoTool(BaseTool):
    def __init__(self, name: str, *, value: str = "ok", read_only: bool = True):
        self._name = name
        self._value = value
        self.calls: list[dict[str, Any]] = []
        self.contexts: list[dict[str, Any]] = []
        self._read_only = read_only

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Echo {self._name}"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["text"],
        }

    def is_read_only(self, params: dict[str, Any] | None = None) -> bool:
        return self._read_only

    def set_runtime_context(
        self,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        session_key: str | None = None,
    ) -> None:
        self.contexts.append({
            "channel": channel,
            "chat_id": chat_id,
            "session_key": session_key,
        })

    def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"{self._value}:{kwargs['text']}:{kwargs.get('count')}"


class CapturingProvider:
    def __init__(self):
        self.requests: list[dict[str, Any]] = []
        self.responses = [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest("call_echo", "local_echo", {"text": "hi"})],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]

    async def chat_with_retry(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_tool_registry_instances_are_isolated_and_cache_definitions() -> None:
    first = ToolRegistry()
    second = ToolRegistry()
    first_tool = EchoTool("local_echo", value="first")
    second_tool = EchoTool("local_echo", value="second")

    first.register(first_tool)
    second.register(second_tool)

    assert first.get("local_echo") is first_tool
    assert second.get("local_echo") is second_tool
    assert first.get_definitions() is first.get_definitions()

    first.unregister("local_echo")

    assert first.get("local_echo") is None
    assert second.get("local_echo") is second_tool
    assert first.get_definitions() == []


def test_registry_prepare_call_coerces_and_validates_params() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool("local_echo"))

    tool, params, error = registry.prepare_call(
        "local_echo",
        {"arguments": '{"text": "hi", "count": "3"}'},
    )

    assert error is None
    assert tool is registry.get("local_echo")
    assert params == {"text": "hi", "count": 3}


def test_execute_tool_batches_uses_supplied_registry_without_global_fallback(monkeypatch) -> None:
    registry = ToolRegistry()
    tool = EchoTool("local_echo", value="registry")
    registry.register(tool)

    async def fail_global_execute(name, params):
        raise AssertionError("global registry should not execute supplied registry tools")

    monkeypatch.setattr(orchestration, "execute_registered_tool", fail_global_execute)

    results = asyncio.run(orchestration.execute_tool_batches(
        [_tool_call("call_echo", "local_echo", {"text": "hi", "count": "2"})],
        tool_registry=registry,
    ))

    assert results[0]["output"] == "registry:hi:2"
    assert tool.calls == [{"text": "hi", "count": 2}]


def test_agent_runner_uses_registry_definitions_and_execution(monkeypatch) -> None:
    registry = ToolRegistry()
    tool = EchoTool("local_echo", value="runner")
    registry.register(tool)
    provider = CapturingProvider()

    async def fail_global_execute(name, params):
        raise AssertionError("runner should execute through spec.tool_registry")

    monkeypatch.setattr(orchestration, "execute_registered_tool", fail_global_execute)

    result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "echo"}],
        provider=provider,
        tools=[],
        tool_handlers={},
        tool_registry=registry,
        model="test-model",
        max_iterations=2,
        emit_output=False,
    )))

    assert result.final_content == "done"
    assert provider.requests[0]["tools"] == registry.get_definitions()
    assert tool.calls == [{"text": "hi"}]


def test_subagent_builds_registry_from_capability_allowlist() -> None:
    runner = SubagentRunner()

    registry = runner._build_tool_registry(("read_file", "list_dir"))

    assert isinstance(registry, ToolRegistry)
    assert sorted(registry.tool_names) == ["list_dir", "read_file"]
    assert registry.get("bash") is None


def test_runtime_registry_adapts_extra_schema_handlers() -> None:
    base = ToolRegistry()
    base.register(EchoTool("local_echo", value="base"))
    calls = []

    def extra_handler(value: int) -> str:
        calls.append(value)
        return f"extra:{value}"

    extra_schema = {
        "type": "function",
        "function": {
            "name": "mcp_demo_add",
            "description": "Extra MCP-style handler",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
        },
    }

    runtime = build_runtime_tool_registry(
        base,
        [*base.get_definitions(), extra_schema],
        {"mcp_demo_add": extra_handler},
    )

    assert runtime.get("local_echo") is base.get("local_echo")
    assert runtime.get("mcp_demo_add") is not None
    assert "mcp_demo_add" in [
        schema["function"]["name"]
        for schema in runtime.get_definitions()
    ]
    assert asyncio.run(runtime.execute("mcp_demo_add", {"value": "7"})) == "extra:7"
    assert calls == [7]
