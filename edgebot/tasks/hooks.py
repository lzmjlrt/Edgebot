"""
edgebot/tasks/hooks.py - Lightweight task lifecycle hooks.

Inspired by claude-code's task hooks, but intentionally minimal:
 - configured via `.edgebot/task_hooks.json`
 - lifecycle events: `task_created`, `task_completed`
 - non-zero exit codes block the lifecycle transition
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from edgebot.config import TASK_HOOKS_PATH, WORKDIR

_DEFAULT_TIMEOUT_SECONDS = 15
_VALID_EVENTS = {"task_created", "task_completed"}


def _load_hooks_config() -> dict[str, list[dict[str, Any]]]:
    if not TASK_HOOKS_PATH.exists():
        return {}
    try:
        raw = json.loads(TASK_HOOKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    config: dict[str, list[dict[str, Any]]] = {}
    for event, entries in raw.items():
        if event not in _VALID_EVENTS or not isinstance(entries, list):
            continue
        normalized: list[dict[str, Any]] = []
        for entry in entries:
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
            timeout = entry.get("timeout", _DEFAULT_TIMEOUT_SECONDS)
            try:
                timeout_value = max(1, int(timeout))
            except (TypeError, ValueError):
                timeout_value = _DEFAULT_TIMEOUT_SECONDS
            normalized.append({
                "command": command,
                "timeout": timeout_value,
                "blocking": bool(entry.get("blocking", True)),
            })
        if normalized:
            config[event] = normalized
    return config


def run_task_hooks(event: str, payload: dict[str, Any]) -> list[str]:
    """Run configured hooks for a task lifecycle event and return blocking errors."""
    config = _load_hooks_config()
    entries = config.get(event, [])
    if not entries:
        return []

    errors: list[str] = []
    base_env = os.environ.copy()
    base_env["EDGEBOT_TASK_EVENT"] = event
    base_env["EDGEBOT_TASK_PAYLOAD"] = json.dumps(payload, ensure_ascii=False)

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

        if result.returncode == 0:
            continue
        if not entry["blocking"]:
            continue
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        errors.append(f"{event} hook blocked task: {entry['command']} ({detail})")
    return errors
