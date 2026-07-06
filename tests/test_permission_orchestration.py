import asyncio
import json

from edgebot.tools import orchestration
from edgebot.tools.registry import PERMISSIONS


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args),
        },
    }


def test_batch_approval_approves_multiple_sensitive_calls_before_execution(monkeypatch) -> None:
    approvals = []
    executed = []

    async def batch_handler(requests):
        approvals.append(requests)
        return {"action": "allow_all"}

    async def fake_execute_registered_tool(name, params):
        executed.append((name, params))
        return f"ok:{name}"

    monkeypatch.setattr(orchestration, "execute_registered_tool", fake_execute_registered_tool)
    PERMISSIONS.set_batch_prompt_handler(batch_handler)
    PERMISSIONS.set_prompt_handler(None)
    try:
        results = asyncio.run(orchestration.execute_tool_batches([
            _tool_call("call_1", "bash", {"command": "python -c pass"}),
            _tool_call("call_2", "background_run", {"command": "python -m http.server"}),
        ]))
    finally:
        PERMISSIONS.set_batch_prompt_handler(None)

    assert len(approvals) == 1
    assert [request["tool"] for request in approvals[0]] == ["bash", "background_run"]
    assert executed == [
        ("bash", {"command": "python -c pass"}),
        ("background_run", {"command": "python -m http.server"}),
    ]
    assert [result["output"] for result in results] == ["ok:bash", "ok:background_run"]


def test_batch_denial_rejects_all_sensitive_calls_without_execution(monkeypatch) -> None:
    executed = []

    async def batch_handler(_requests):
        return {"action": "deny_all"}

    async def fake_execute_registered_tool(name, params):
        executed.append((name, params))
        return "should-not-run"

    monkeypatch.setattr(orchestration, "execute_registered_tool", fake_execute_registered_tool)
    PERMISSIONS.set_batch_prompt_handler(batch_handler)
    PERMISSIONS.set_prompt_handler(None)
    try:
        results = asyncio.run(orchestration.execute_tool_batches([
            _tool_call("call_1", "bash", {"command": "python -c pass"}),
            _tool_call("call_2", "background_run", {"command": "python -m http.server"}),
        ]))
    finally:
        PERMISSIONS.set_batch_prompt_handler(None)

    assert executed == []
    assert all("Permission denied" in str(result["output"]) for result in results)


def test_batch_review_one_by_one_falls_back_to_single_prompt(monkeypatch) -> None:
    batch_requests = []
    single_requests = []
    executed = []

    async def batch_handler(requests):
        batch_requests.append(requests)
        return {"action": "review_one_by_one"}

    async def single_handler(request):
        single_requests.append(request)
        return {"action": "allow"}

    async def fake_execute_registered_tool(name, params):
        executed.append((name, params))
        return f"ok:{name}"

    monkeypatch.setattr(orchestration, "execute_registered_tool", fake_execute_registered_tool)
    PERMISSIONS.set_batch_prompt_handler(batch_handler)
    PERMISSIONS.set_prompt_handler(single_handler)
    try:
        results = asyncio.run(orchestration.execute_tool_batches([
            _tool_call("call_1", "bash", {"command": "python -c pass"}),
            _tool_call("call_2", "background_run", {"command": "python -m http.server"}),
        ]))
    finally:
        PERMISSIONS.set_batch_prompt_handler(None)
        PERMISSIONS.set_prompt_handler(None)

    assert len(batch_requests) == 1
    assert [request["tool"] for request in single_requests] == ["bash", "background_run"]
    assert executed == [
        ("bash", {"command": "python -c pass"}),
        ("background_run", {"command": "python -m http.server"}),
    ]
    assert [result["output"] for result in results] == ["ok:bash", "ok:background_run"]


def test_batch_approval_does_not_bypass_high_risk_confirmation(monkeypatch) -> None:
    batch_requests = []
    single_requests = []
    executed = []

    async def batch_handler(requests):
        batch_requests.append(requests)
        return {"action": "allow_all"}

    async def single_handler(request):
        single_requests.append(request)
        return {"action": "allow"}

    async def fake_execute_registered_tool(name, params):
        executed.append((name, params))
        return f"ok:{name}"

    monkeypatch.setattr(orchestration, "execute_registered_tool", fake_execute_registered_tool)
    PERMISSIONS.set_batch_prompt_handler(batch_handler)
    PERMISSIONS.set_prompt_handler(single_handler)
    try:
        results = asyncio.run(orchestration.execute_tool_batches([
            _tool_call("call_1", "bash", {"command": "git reset --hard HEAD~1"}),
            _tool_call("call_2", "background_run", {"command": "python -m http.server"}),
        ]))
    finally:
        PERMISSIONS.set_batch_prompt_handler(None)
        PERMISSIONS.set_prompt_handler(None)

    assert batch_requests == []
    assert [request["tool"] for request in single_requests] == ["bash", "background_run"]
    assert single_requests[0]["requires_confirmation"] is True
    assert executed == [
        ("bash", {"command": "git reset --hard HEAD~1"}),
        ("background_run", {"command": "python -m http.server"}),
    ]
    assert [result["output"] for result in results] == ["ok:bash", "ok:background_run"]


def test_permission_hooks_run_after_approval_around_tool_execution(monkeypatch) -> None:
    events = []

    async def prompt_handler(_request):
        events.append("prompt")
        return {"action": "allow"}

    async def fake_execute_registered_tool(name, params):
        events.append(f"execute:{name}")
        return "ok"

    def fake_run_permission_hooks(event, payload):
        events.append(f"hook:{event}:{payload['tool']}")
        return []

    monkeypatch.setattr(orchestration, "execute_registered_tool", fake_execute_registered_tool)
    monkeypatch.setattr(orchestration, "run_permission_hooks", fake_run_permission_hooks)
    PERMISSIONS.set_prompt_handler(prompt_handler)
    try:
        results = asyncio.run(orchestration.execute_tool_batches([
            _tool_call("call_1", "bash", {"command": "python -c pass"}),
        ]))
    finally:
        PERMISSIONS.set_prompt_handler(None)

    assert results[0]["output"] == "ok"
    assert events == [
        "prompt",
        "hook:PreToolUse:bash",
        "execute:bash",
        "hook:PostToolUse:bash",
    ]


def test_blocking_pre_tool_hook_denies_execution_after_user_approval(monkeypatch) -> None:
    events = []

    async def prompt_handler(_request):
        events.append("prompt")
        return {"action": "allow"}

    async def fake_execute_registered_tool(name, params):
        events.append(f"execute:{name}")
        return "should-not-run"

    def fake_run_permission_hooks(event, payload):
        events.append(f"hook:{event}:{payload['tool']}")
        if event == "PreToolUse":
            return ["blocked by hook"]
        return []

    monkeypatch.setattr(orchestration, "execute_registered_tool", fake_execute_registered_tool)
    monkeypatch.setattr(orchestration, "run_permission_hooks", fake_run_permission_hooks)
    PERMISSIONS.set_prompt_handler(prompt_handler)
    try:
        results = asyncio.run(orchestration.execute_tool_batches([
            _tool_call("call_1", "bash", {"command": "python -c pass"}),
        ]))
    finally:
        PERMISSIONS.set_prompt_handler(None)

    assert "blocked by hook" in results[0]["output"]
    assert events == [
        "prompt",
        "hook:PreToolUse:bash",
    ]
