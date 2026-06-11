"""Permission hook runner for PreToolUse/PostToolUse events."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from edgebot.config import WORKDIR

_DEFAULT_TIMEOUT_SECONDS = 15
_VALID_EVENTS = {"PreToolUse", "PostToolUse"}


def _settings_paths() -> tuple[Path, Path, Path]:
    return (
        Path.home() / ".claude" / "settings.json",
        WORKDIR / ".claude" / "settings.json",
        WORKDIR / ".claude" / "settings.local.json",
    )


def _normalize_entries(raw_entries: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_entries, list):
        return []
    normalized: list[dict[str, Any]] = []
    for entry in raw_entries:
        if isinstance(entry, str) and entry.strip():
            normalized.append({
                "command": entry.strip(),
                "timeout": _DEFAULT_TIMEOUT_SECONDS,
                "blocking": True,
            })
            continue
        if not isinstance(entry, dict):
            continue
        command = str(entry.get("command", "")).strip()
        if not command:
            continue
        try:
            timeout = max(1, int(entry.get("timeout", _DEFAULT_TIMEOUT_SECONDS)))
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT_SECONDS
        normalized.append({
            "command": command,
            "timeout": timeout,
            "blocking": bool(entry.get("blocking", True)),
        })
    return normalized


def _load_hook_entries(event: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in _settings_paths():
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            continue
        entries.extend(_normalize_entries(hooks.get(event)))
    return entries


def run_permission_hooks(event: str, payload: dict[str, Any]) -> list[str]:
    """Run configured permission hooks and return blocking errors."""
    if event not in _VALID_EVENTS:
        return []
    entries = _load_hook_entries(event)
    if not entries:
        return []

    base_env = os.environ.copy()
    base_env["EDGEBOT_PERMISSION_HOOK_EVENT"] = event
    base_env["EDGEBOT_PERMISSION_HOOK_PAYLOAD"] = json.dumps(payload, ensure_ascii=False)

    errors: list[str] = []
    for entry in entries:
        try:
            result = subprocess.run(
                entry["command"],
                shell=True,
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=entry["timeout"],
                env=base_env,
            )
        except subprocess.TimeoutExpired:
            if entry["blocking"]:
                errors.append(
                    f"{event} hook timed out after {entry['timeout']}s: {entry['command']}"
                )
            continue
        except Exception as exc:
            if entry["blocking"]:
                errors.append(f"{event} hook failed to start: {entry['command']} ({exc})")
            continue

        if result.returncode == 0 or not entry["blocking"]:
            continue
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        errors.append(f"{event} hook blocked tool: {entry['command']} ({detail})")
    return errors
