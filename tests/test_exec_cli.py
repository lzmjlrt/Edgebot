import asyncio

import pytest


def test_main_dispatches_exec_command(monkeypatch, capsys) -> None:
    import edgebot.__main__ as main_module

    calls = []

    async def fake_exec_main(instruction: str) -> str:
        calls.append(instruction)
        return "final answer"

    monkeypatch.setattr(main_module.sys, "argv", ["edgebot", "exec", "do this"])
    monkeypatch.setattr("edgebot.cli.exec_once.exec_main", fake_exec_main)

    main_module.main()

    assert calls == ["do this"]
    assert capsys.readouterr().out == "final answer\n"


def test_main_rejects_exec_without_instruction(monkeypatch, capsys) -> None:
    import edgebot.__main__ as main_module

    monkeypatch.setattr(main_module.sys, "argv", ["edgebot", "exec"])

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 2
    assert "Usage: edgebot exec" in capsys.readouterr().err


def test_exec_main_runs_agent_once_and_prints_final_answer(monkeypatch) -> None:
    import edgebot.cli.exec_once as exec_once

    events = []

    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            events.append("store")

        def update_metadata(self, session_key, **metadata):
            events.append(("metadata", session_key, metadata))

        def append(self, session_key, message):
            events.append(("append", session_key, message))

    async def fake_load_mcp(_path):
        events.append("load_mcp")
        return None

    async def fake_agent_loop(**kwargs):
        events.append(("agent_loop", kwargs))
        return "done"

    monkeypatch.setattr(exec_once, "SessionStore", FakeStore)
    monkeypatch.setattr(exec_once, "load_mcp", fake_load_mcp)
    monkeypatch.setattr(exec_once, "agent_loop", fake_agent_loop)
    monkeypatch.setattr(exec_once, "seed_workspace_templates", lambda: events.append("seed"))
    monkeypatch.setattr(exec_once.SKILLS, "reload", lambda: events.append("skills_reload"))
    monkeypatch.setattr(exec_once, "cleanup_memory_files_once", lambda: events.append("memory_cleanup"))
    monkeypatch.setattr(exec_once, "build_system_prompt", lambda session_key: f"system:{session_key}")
    monkeypatch.setattr(exec_once, "set_ask_handler", lambda handler: events.append(("ask", handler)))
    monkeypatch.setattr(exec_once, "set_permission_prompt_handler", lambda handler: events.append(("permission", handler)))
    monkeypatch.setattr(exec_once, "set_batch_permission_prompt_handler", lambda handler: events.append(("batch", handler)))

    result = asyncio.run(exec_once.exec_main("only reply ok"))

    assert result == "done"
    agent_event = next(event for event in events if isinstance(event, tuple) and event[0] == "agent_loop")
    kwargs = agent_event[1]
    assert kwargs["messages"] == [{"role": "user", "content": "only reply ok"}]
    assert kwargs["system"].startswith("system:exec_")
    assert kwargs["emit_output"] is False
    assert kwargs["assistant_label"] == "Edgebot"
    assert events[-3:] == [("permission", None), ("batch", None), ("ask", None)]


def test_exec_mode_auto_allows_permissions(monkeypatch) -> None:
    import edgebot.cli.exec_once as exec_once

    captured = {}

    monkeypatch.setattr(exec_once, "set_permission_prompt_handler", lambda handler: captured.setdefault("single", handler))
    monkeypatch.setattr(exec_once, "set_batch_permission_prompt_handler", lambda handler: captured.setdefault("batch", handler))
    monkeypatch.setattr(exec_once, "set_ask_handler", lambda handler: captured.setdefault("ask", handler))

    exec_once._install_noninteractive_handlers(eval_mode=True)

    assert asyncio.run(captured["single"]({"requires_confirmation": True})) == {"action": "allow"}
    assert asyncio.run(captured["batch"]([{"tool": "bash"}])) == {"action": "allow_all"}
    assert asyncio.run(captured["ask"]("question")) == ""
