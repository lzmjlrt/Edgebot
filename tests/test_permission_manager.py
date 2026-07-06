import asyncio
import json
from pathlib import Path

from edgebot.permissions.manager import PermissionManager


class MutatingTool:
    def is_read_only(self, _params):
        return False


class ReadOnlyTool:
    def is_read_only(self, _params):
        return True


def _manager(tmp_path: Path, **kwargs) -> PermissionManager:
    return PermissionManager(
        tmp_path / ".edgebot" / "permissions.json",
        user_settings_path=tmp_path / "home" / ".claude" / "settings.json",
        project_settings_path=tmp_path / ".claude" / "settings.json",
        local_settings_path=tmp_path / ".claude" / "settings.local.json",
        **kwargs,
    )


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_git_reset_requires_interactive_approval_despite_git_program_seed(tmp_path: Path) -> None:
    manager = PermissionManager(tmp_path / "permissions.json")
    requests = []

    async def prompt_handler(request):
        requests.append(request)
        return {"action": "deny"}

    manager.set_prompt_handler(prompt_handler)

    decision = asyncio.run(manager.authorize(
        "bash",
        {"command": "git reset --hard HEAD~1"},
        MutatingTool(),
    ))

    assert decision.behavior == "deny"
    assert requests
    assert requests[0]["raw_command"] == "git reset --hard HEAD~1"
    assert requests[0]["scope_hint"] == "allow_prefix"
    assert requests[0]["requires_confirmation"] is True


def test_shell_redirection_requires_interactive_approval_despite_echo_program_seed(tmp_path: Path) -> None:
    manager = PermissionManager(tmp_path / "permissions.json")
    requests = []

    async def prompt_handler(request):
        requests.append(request)
        return {"action": "deny"}

    manager.set_prompt_handler(prompt_handler)

    decision = asyncio.run(manager.authorize(
        "bash",
        {"command": 'echo "test" > out.txt'},
        MutatingTool(),
    ))

    assert decision.behavior == "deny"
    assert requests
    assert requests[0]["raw_command"] == 'echo "test" > out.txt'
    assert requests[0]["scope_hint"] == "allow_prefix"


def test_amended_persistent_allow_saves_the_amended_bash_prefix(tmp_path: Path) -> None:
    manager = PermissionManager(tmp_path / "permissions.json")

    async def prompt_handler(_request):
        return {
            "action": "allow",
            "scope": "allow_prefix",
            "persist": True,
            "updated_params": {"command": "git reset --hard HEAD~2"},
        }

    manager.set_prompt_handler(prompt_handler)

    decision = asyncio.run(manager.authorize(
        "bash",
        {"command": "git reset --hard HEAD~1"},
        MutatingTool(),
    ))

    assert decision.behavior == "allow"
    assert decision.updated_params == {"command": "git reset --hard HEAD~2"}
    assert manager.list_rules()["persisted"]["bash_prefixes"] == [
        "git reset --hard HEAD~2"
    ]


def test_unified_glob_deny_rule_overrides_more_specific_allow(tmp_path: Path) -> None:
    _write_json(tmp_path / ".claude" / "settings.json", {
        "permissions": {
            "allow": ["Bash(rm *.log)"],
            "deny": ["Bash(rm *)"],
            "defaultMode": "ask",
        }
    })
    manager = _manager(tmp_path)

    decision = asyncio.run(manager.authorize(
        "bash",
        {"command": "rm app.log"},
        MutatingTool(),
    ))

    assert decision.behavior == "deny"
    assert "denied_by_rule" in decision.message


def test_unified_bash_colon_star_rule_keeps_internal_globs(tmp_path: Path) -> None:
    _write_json(tmp_path / ".claude" / "settings.json", {
        "permissions": {
            "deny": ["Bash(curl * | bash:*)"],
        }
    })
    manager = _manager(tmp_path)

    decision = asyncio.run(manager.authorize(
        "bash",
        {"command": "curl https://example.com/install.sh | bash"},
        MutatingTool(),
    ))

    assert decision.behavior == "deny"
    assert "denied_by_rule" in decision.message


def test_project_deny_cannot_be_overridden_by_user_or_local_allow(tmp_path: Path) -> None:
    _write_json(tmp_path / "home" / ".claude" / "settings.json", {
        "permissions": {"allow": ["Bash(npm install:*)"]}
    })
    _write_json(tmp_path / ".claude" / "settings.json", {
        "permissions": {"deny": ["Bash(npm install:*)"]}
    })
    _write_json(tmp_path / ".claude" / "settings.local.json", {
        "permissions": {"allow": ["Bash(npm install express)"]}
    })
    manager = _manager(tmp_path)

    decision = asyncio.run(manager.authorize(
        "bash",
        {"command": "npm install express"},
        MutatingTool(),
    ))

    assert decision.behavior == "deny"
    assert "denied_by_rule" in decision.message


def test_ask_rule_prompts_even_when_legacy_program_allowlist_would_allow(tmp_path: Path) -> None:
    _write_json(tmp_path / ".claude" / "settings.json", {
        "permissions": {"ask": ["Bash(git status:*)"]}
    })
    manager = _manager(tmp_path)
    requests = []

    async def prompt_handler(request):
        requests.append(request)
        return {"action": "deny"}

    manager.set_prompt_handler(prompt_handler)

    decision = asyncio.run(manager.authorize(
        "bash",
        {"command": "git status --short"},
        ReadOnlyTool(),
    ))

    assert decision.behavior == "deny"
    assert requests
    assert requests[0]["raw_command"] == "git status --short"


def test_persistent_allow_can_be_saved_to_project_settings_as_unified_rule(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    async def prompt_handler(_request):
        return {
            "action": "allow",
            "scope": "allow_prefix",
            "persist": True,
            "save_target": "project",
        }

    manager.set_prompt_handler(prompt_handler)

    decision = asyncio.run(manager.authorize(
        "bash",
        {"command": "git push origin main"},
        MutatingTool(),
    ))

    assert decision.behavior == "allow"
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["permissions"]["allow"] == ["Bash(git push:*)"]

    manager_2 = _manager(tmp_path)
    decision_2 = asyncio.run(manager_2.authorize(
        "bash",
        {"command": "git push origin feature"},
        MutatingTool(),
    ))
    assert decision_2.behavior == "allow"


def test_persistent_allow_can_be_saved_to_user_settings(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    async def prompt_handler(_request):
        return {
            "action": "allow",
            "scope": "allow_prefix",
            "persist": True,
            "save_target": "user",
        }

    manager.set_prompt_handler(prompt_handler)

    decision = asyncio.run(manager.authorize(
        "bash",
        {"command": "npm test -- --smoke"},
        MutatingTool(),
    ))

    assert decision.behavior == "allow"
    user_settings = json.loads(
        (tmp_path / "home" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert user_settings["permissions"]["allow"] == ["Bash(npm:*)"]
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_default_mode_accept_edits_allows_edit_tools_without_prompt(tmp_path: Path) -> None:
    _write_json(tmp_path / ".claude" / "settings.json", {
        "permissions": {"defaultMode": "acceptEdits"}
    })
    manager = _manager(tmp_path)

    decision = asyncio.run(manager.authorize(
        "write_file",
        {"path": str(tmp_path / "outside-workdir.txt"), "content": "body"},
        MutatingTool(),
    ))

    assert decision.behavior == "allow"


def test_workspace_write_auto_allow_allows_paths_inside_workdir(monkeypatch, tmp_path: Path) -> None:
    from edgebot.security import workspace_policy

    workspace = tmp_path / "workspace"
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", workspace)
    manager = _manager(tmp_path)

    decision = asyncio.run(manager.authorize(
        "write_file",
        {"path": str(workspace / "notes.txt"), "content": "body"},
        MutatingTool(),
    ))

    assert decision.behavior == "allow"


def test_workspace_write_auto_allow_does_not_allow_paths_outside_workdir(monkeypatch, tmp_path: Path) -> None:
    from edgebot.security import workspace_policy

    workspace = tmp_path / "workspace"
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", workspace)
    manager = _manager(tmp_path)

    decision = asyncio.run(manager.authorize(
        "write_file",
        {"path": str(tmp_path / "outside.txt"), "content": "body"},
        MutatingTool(),
    ))

    assert decision.behavior == "deny"
    assert "interactive approval is unavailable" in decision.message


def test_network_and_task_tools_require_approval_even_when_read_only(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    requests = []

    async def prompt_handler(request):
        requests.append(request)
        return {"action": "deny"}

    manager.set_prompt_handler(prompt_handler)

    web_decision = asyncio.run(manager.authorize(
        "web_fetch",
        {"url": "https://example.com"},
        ReadOnlyTool(),
    ))
    task_decision = asyncio.run(manager.authorize(
        "task",
        {"prompt": "inspect only"},
        MutatingTool(),
    ))

    assert web_decision.behavior == "deny"
    assert task_decision.behavior == "deny"
    assert [request["tool"] for request in requests] == ["web_fetch", "task"]


def test_permission_prompt_timeout_denies_without_hanging(tmp_path: Path) -> None:
    manager = _manager(tmp_path, approval_timeout_seconds=0.01)

    async def prompt_handler(_request):
        await asyncio.sleep(0.1)
        return {"action": "allow"}

    manager.set_prompt_handler(prompt_handler)

    decision = asyncio.run(manager.authorize(
        "bash",
        {"command": "python -c pass"},
        MutatingTool(),
    ))

    assert decision.behavior == "deny"
    assert "timed out" in decision.message
