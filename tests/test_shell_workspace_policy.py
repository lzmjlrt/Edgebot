from pathlib import Path

from edgebot.background.manager import BackgroundManager
from edgebot.security import workspace_policy
from edgebot.tools import shell


def test_shell_guard_allows_absolute_workspace_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", tmp_path)
    monkeypatch.setattr(shell, "WORKDIR", tmp_path)
    inside = tmp_path / "inside.txt"

    assert shell._guard_command(f"type {inside}", str(tmp_path)) is None


def test_shell_guard_rejects_absolute_path_outside_workspace(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", workspace)
    monkeypatch.setattr(shell, "WORKDIR", workspace)

    result = shell._guard_command(f"type {outside}", str(workspace))

    assert result is not None
    assert "path outside working dir" in result
    assert "hard policy boundary" in result


def test_background_run_rejects_absolute_path_outside_workspace(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", workspace)
    monkeypatch.setattr(shell, "WORKDIR", workspace)

    manager = BackgroundManager(output_dir=tmp_path / "background")
    result = manager.run(f"type {outside}")

    assert result["status"] == "error"
    assert "path outside working dir" in result["error"]
    assert "task_id" not in result


def test_run_bash_keeps_safe_read_command_working(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", tmp_path)
    monkeypatch.setattr(shell, "WORKDIR", tmp_path)

    result = shell.run_bash("echo workspace-ok")

    assert "workspace-ok" in result
    assert "Exit code: 0" in result


def test_run_bash_uses_workspace_policy_root_for_process_cwd(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", tmp_path)
    captured = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            return "ok", ""

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return FakeProcess()

    monkeypatch.setattr(shell.subprocess, "Popen", fake_popen)

    result = shell.run_bash("echo ok")

    assert "ok" in result
    assert captured["cwd"] == tmp_path.resolve(strict=False)
