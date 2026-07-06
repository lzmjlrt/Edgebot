import asyncio
from contextlib import nullcontext

from prompt_toolkit.keys import Keys

from edgebot.cli import repl


def _press(captured, key, data=""):
    binding = next(
        item
        for item in captured["key_bindings"].bindings
        if any(bound_key == key for bound_key in item.keys)
    )

    class FakeApp:
        def __init__(self):
            self.result = None

        def invalidate(self):
            pass

        def exit(self, result=None):
            self.result = result

    class FakeEvent:
        def __init__(self):
            self.app = FakeApp()
            self.data = data

    event = FakeEvent()
    binding.handler(event)
    return event.app.result


def test_permission_prompt_renders_claude_style_bash_picker(monkeypatch) -> None:
    captured = {}
    rendered = ""

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            nonlocal rendered
            body = captured["layout"].container.children[0].content
            fragments = body.text()
            rendered = "".join(text for _style, text in fragments)
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._permission_prompt({
        "tool": "bash",
        "raw_command": "git reset --hard HEAD~1",
        "message": "Edgebot requests permission to run shell command:\ngit reset --hard HEAD~1",
        "scope_hint": "allow_prefix",
        "scope_value": "git reset --hard HEAD~1",
    }))

    assert result == {"action": "allow"}
    assert "Bash command" in rendered
    assert "git reset --hard HEAD~1" in rendered
    assert "This command requires approval" in rendered
    assert "Do you want to proceed?" in rendered
    assert "> 1. Yes" in rendered
    assert "2. Yes, and don't ask again for: git reset *" in rendered
    assert "3. No" in rendered
    assert "Esc to cancel" in rendered
    assert "Tab to amend" in rendered
    assert "ctrl+e to explain" in rendered


def test_permission_prompt_returns_persistent_allow_from_second_option(monkeypatch) -> None:
    captured = {}

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            _press(captured, Keys.Down)
            _press(captured, Keys.ControlM)
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._permission_prompt({
        "tool": "bash",
        "raw_command": "git reset --hard HEAD~1",
        "message": "Edgebot requests permission to run shell command:\ngit reset --hard HEAD~1",
        "scope_hint": "allow_prefix",
        "scope_value": "git reset --hard HEAD~1",
    }))

    assert result == {
        "action": "allow",
        "scope": "allow_prefix",
        "persist": True,
        "save_target": "project",
    }


def test_permission_prompt_second_option_shows_save_location_picker(monkeypatch) -> None:
    captured = {}
    save_render = ""

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            nonlocal save_render
            _press(captured, Keys.Down)
            _press(captured, Keys.ControlM)
            save_render = "".join(
                text
                for _style, text in captured["layout"].container.children[0].content.text()
            )
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._permission_prompt({
        "tool": "bash",
        "raw_command": "git push origin main",
        "message": "Edgebot requests permission to run shell command:\ngit push origin main",
        "scope_hint": "allow_prefix",
        "scope_value": "git push origin main",
        "rule_preview": "Bash(git push:*)",
    }))

    assert result["save_target"] == "project"
    assert "Rule Preview" in save_render
    assert "Bash(git push:*)" in save_render
    assert "Save to project settings" in save_render
    assert "Save to user settings" in save_render
    assert "Cancel" in save_render


def test_permission_prompt_save_location_cancel_allows_once(monkeypatch) -> None:
    captured = {}

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            _press(captured, Keys.Down)
            _press(captured, Keys.ControlM)
            _press(captured, Keys.Down)
            _press(captured, Keys.Down)
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._permission_prompt({
        "tool": "bash",
        "raw_command": "git push origin main",
        "message": "Edgebot requests permission to run shell command:\ngit push origin main",
        "scope_hint": "allow_prefix",
        "rule_preview": "Bash(git push:*)",
    }))

    assert result == {"action": "allow"}


def test_permission_prompt_escape_denies(monkeypatch) -> None:
    captured = {}

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            return _press(captured, Keys.Escape)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._permission_prompt({
        "tool": "bash",
        "raw_command": "git push origin main",
        "message": "Edgebot requests permission to run shell command:\ngit push origin main",
        "scope_hint": "allow_prefix",
    }))

    assert result == {"action": "deny"}


def test_permission_prompt_ctrl_e_collects_feedback(monkeypatch) -> None:
    captured = {}
    rendered = ""

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            nonlocal rendered
            _press(captured, Keys.ControlE)
            _press(captured, Keys.Any, data="c")
            _press(captured, Keys.Any, data="r")
            rendered = "".join(
                text
                for _style, text in captured["layout"].container.children[0].content.text()
            )
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._permission_prompt({
        "tool": "bash",
        "raw_command": "git push origin main",
        "message": "Edgebot requests permission to run shell command:\ngit push origin main",
        "scope_hint": "allow_prefix",
    }))

    assert result == {"action": "deny", "feedback": "cr"}
    assert "Tell Edgebot what to do differently:" in rendered


def test_permission_prompt_tab_amends_bash_command_before_allowing(monkeypatch) -> None:
    captured = {}
    rendered = ""

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            nonlocal rendered
            _press(captured, Keys.Tab)
            _press(captured, Keys.ControlH)
            _press(captured, Keys.Any, data="2")
            rendered = "".join(
                text
                for _style, text in captured["layout"].container.children[0].content.text()
            )
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._permission_prompt({
        "tool": "bash",
        "raw_command": "git reset --hard HEAD~1",
        "message": "Edgebot requests permission to run shell command:\ngit reset --hard HEAD~1",
        "scope_hint": "allow_prefix",
    }))

    assert result == {
        "action": "allow",
        "updated_params": {"command": "git reset --hard HEAD~2"},
    }
    assert "Amend command before running:" in rendered


def test_permission_prompt_tab_amends_file_path_without_dropping_other_params(monkeypatch) -> None:
    captured = {}

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            _press(captured, Keys.Tab)
            _press(captured, Keys.ControlH)
            _press(captured, Keys.Any, data="x")
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._permission_prompt({
        "tool": "write_file",
        "message": "Edgebot requests permission to modify file:\ndocs/a",
        "scope_hint": "allow_tool",
        "params": {"path": "docs/a", "content": "body"},
    }))

    assert result == {
        "action": "allow",
        "updated_params": {"path": "docs/x", "content": "body"},
    }


def test_high_risk_permission_prompt_requires_exact_command_before_picker(monkeypatch) -> None:
    captured = {}
    initial_render = ""
    picker_render = ""

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            nonlocal initial_render
            nonlocal picker_render
            body = captured["layout"].container.children[0].content
            initial_render = "".join(text for _style, text in body.text())
            for char in "git reset --hard HEAD~1":
                _press(captured, Keys.Any, data=char)
            _press(captured, Keys.ControlM)
            picker_render = "".join(text for _style, text in body.text())
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._permission_prompt({
        "tool": "bash",
        "raw_command": "git reset --hard HEAD~1",
        "message": "Edgebot requests permission to run shell command:\ngit reset --hard HEAD~1",
        "scope_hint": "allow_prefix",
        "scope_value": "git reset --hard HEAD~1",
        "requires_confirmation": True,
    }))

    assert result == {"action": "allow"}
    assert "HIGH RISK COMMAND" in initial_render
    assert "Type the command exactly to continue" in initial_render
    assert "Do you want to proceed?" in picker_render


def test_high_risk_permission_prompt_denies_when_confirmation_differs(monkeypatch) -> None:
    captured = {}

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            for char in "git reset":
                _press(captured, Keys.Any, data=char)
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._permission_prompt({
        "tool": "bash",
        "raw_command": "git reset --hard HEAD~1",
        "message": "Edgebot requests permission to run shell command:\ngit reset --hard HEAD~1",
        "scope_hint": "allow_prefix",
        "requires_confirmation": True,
    }))

    assert result == {"action": "deny"}


def test_batch_permission_prompt_renders_summary_and_allows_all(monkeypatch) -> None:
    captured = {}
    rendered = ""

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            nonlocal rendered
            body = captured["layout"].container.children[0].content
            rendered = "".join(text for _style, text in body.text())
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._batch_permission_prompt([
        {
            "tool": "bash",
            "raw_command": "python -c pass",
            "message": "Edgebot requests permission to run shell command:\npython -c pass",
        },
        {
            "tool": "background_run",
            "message": "Edgebot requests permission to start background task:\npython -m http.server",
        },
    ]))

    assert result == {"action": "allow_all"}
    assert "2 Permissions Required" in rendered
    assert "[1/2] Bash command" in rendered
    assert "python -c pass" in rendered
    assert "Approve ALL" in rendered
    assert "Deny ALL" in rendered
    assert "Review one-by-one" in rendered


def test_batch_permission_prompt_can_review_one_by_one(monkeypatch) -> None:
    captured = {}

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            _press(captured, Keys.Down)
            _press(captured, Keys.Down)
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    result = asyncio.run(repl._batch_permission_prompt([
        {"tool": "bash", "raw_command": "python -c pass"},
        {"tool": "background_run", "message": "run"},
    ]))

    assert result == {"action": "review_one_by_one"}
