from pathlib import Path

import pytest

from edgebot.security import workspace_policy
from edgebot.tools import base as tool_base


def test_resolve_workspace_path_resolves_relative_paths_under_workdir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", tmp_path)

    resolved = workspace_policy.resolve_workspace_path("src/app.py")

    assert resolved == (tmp_path / "src" / "app.py").resolve(strict=False)


def test_require_workspace_path_allows_absolute_paths_inside_workdir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", tmp_path)
    target = tmp_path / "nested" / "new.txt"

    resolved = workspace_policy.require_workspace_path(target)

    assert resolved == target.resolve(strict=False)


def test_require_workspace_path_rejects_absolute_paths_outside_workdir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", tmp_path / "workspace")
    outside = tmp_path / "outside.txt"

    with pytest.raises(workspace_policy.WorkspaceBoundaryError) as exc:
        workspace_policy.require_workspace_path(outside)

    message = str(exc.value)
    assert "outside allowed workspace" in message
    assert "hard policy boundary" in message
    assert "do not retry" in message


def test_require_workspace_path_rejects_parent_traversal(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", workspace)

    with pytest.raises(workspace_policy.WorkspaceBoundaryError):
        workspace_policy.require_workspace_path("../outside.txt")


def test_resolve_workspace_path_allows_missing_files_inside_workdir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(workspace_policy.config, "WORKDIR", tmp_path)

    resolved = workspace_policy.resolve_workspace_path("missing/new-file.txt")

    assert resolved == (tmp_path / "missing" / "new-file.txt").resolve(strict=False)
    assert not resolved.exists()


def test_safe_path_compatibility_wrapper_respects_tools_base_workdir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tool_base, "WORKDIR", tmp_path)

    resolved = tool_base.safe_path("README.md")

    assert resolved == (tmp_path / "README.md").resolve(strict=False)
