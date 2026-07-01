"""Shared workspace path boundary helpers.

These helpers are application-level guards for Edgebot's single-workspace CLI
model. They make path decisions consistent across tools, but they are not a
replacement for an OS sandbox.
"""

from __future__ import annotations

from pathlib import Path

from edgebot import config

WORKSPACE_BOUNDARY_NOTE = (
    " (this is a hard policy boundary, not a transient failure; "
    "do not retry with shell tricks or alternative tools, and ask "
    "the user how to proceed if the resource is genuinely required)"
)


class WorkspaceBoundaryError(PermissionError):
    """Raised when a requested path escapes the configured workspace."""


def workspace_root() -> Path:
    """Return the current resolved Edgebot workspace root."""
    return Path(config.WORKDIR).expanduser().resolve(strict=False)


def _resolve_workspace_path(
    path: str | Path,
    *,
    workspace: str | Path,
    strict: bool = False,
) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(workspace).expanduser() / candidate
    return candidate.resolve(strict=strict)


def resolve_workspace_path(path: str | Path, *, strict: bool = False) -> Path:
    """Resolve a path against WORKDIR without enforcing containment."""
    return _resolve_workspace_path(path, workspace=workspace_root(), strict=strict)


def _is_path_within(path: str | Path, root: str | Path) -> bool:
    try:
        resolved_path = Path(path).expanduser().resolve(strict=False)
        resolved_root = Path(root).expanduser().resolve(strict=False)
        resolved_path.relative_to(resolved_root)
        return True
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def is_path_within_workspace(path: str | Path) -> bool:
    """Return True when path resolves to WORKDIR or one of its descendants."""
    try:
        resolved = resolve_workspace_path(path)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return _is_path_within(resolved, workspace_root())


def _require_workspace_path(
    path: str | Path,
    *,
    workspace: str | Path,
    raw_label: str | None = None,
) -> Path:
    label = raw_label if raw_label is not None else str(path)
    resolved = _resolve_workspace_path(path, workspace=workspace)
    root = Path(workspace).expanduser().resolve(strict=False)
    if not _is_path_within(resolved, root):
        raise WorkspaceBoundaryError(
            f"Path {label} is outside allowed workspace {root}"
            + WORKSPACE_BOUNDARY_NOTE
        )
    return resolved


def require_workspace_path(path: str | Path, *, raw_label: str | None = None) -> Path:
    """Resolve a path and require it to stay inside WORKDIR."""
    return _require_workspace_path(
        path,
        workspace=workspace_root(),
        raw_label=raw_label,
    )
