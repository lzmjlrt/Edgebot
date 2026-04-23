"""Shell command execution helpers with basic safety guards."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from edgebot.config import WORKDIR

_MAX_OUTPUT = 10_000
_TIMEOUT = 120
_DENY_PATTERNS = [
    r"\brm\s+-[rf]{1,2}\b",
    r"\bdel\s+/[fq]\b",
    r"\brmdir\s+/s\b",
    r"(?:^|[;&|]\s*)format\b",
    r"\b(mkfs|diskpart)\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r"\b(shutdown|reboot|poweroff)\b",
    r":\(\)\s*\{.*\};\s*:",
]


def _extract_absolute_paths(command: str) -> list[str]:
    win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]*", command)
    posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command)
    home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command)
    return win_paths + posix_paths + home_paths


def _guard_command(command: str, cwd: str) -> str | None:
    lower = command.strip().lower()
    for pattern in _DENY_PATTERNS:
        if re.search(pattern, lower):
            return "Error: Command blocked by safety guard (dangerous pattern detected)"

    from edgebot.security.network import contains_internal_url

    if contains_internal_url(command):
        return "Error: Command blocked by safety guard (internal/private URL detected)"
    if "..\\" in command or "../" in command:
        return "Error: Command blocked by safety guard (path traversal detected)"

    cwd_path = Path(cwd).resolve()
    for raw in _extract_absolute_paths(command):
        try:
            p = Path(os.path.expandvars(raw.strip())).expanduser().resolve()
        except Exception:
            continue
        if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
            return "Error: Command blocked by safety guard (path outside working dir)"
    return None


def run_bash(command: str) -> str:
    guard_error = _guard_command(command, str(WORKDIR))
    if guard_error:
        return guard_error
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {_TIMEOUT} seconds"
    except Exception as e:
        return f"Error executing command: {e}"

    output_parts = []
    if result.stdout:
        output_parts.append(result.stdout)
    if result.stderr and result.stderr.strip():
        output_parts.append(f"STDERR:\n{result.stderr}")
    output_parts.append(f"\nExit code: {result.returncode}")
    output = "\n".join(output_parts) if output_parts else "(no output)"
    if len(output) > _MAX_OUTPUT:
        half = _MAX_OUTPUT // 2
        output = (
            output[:half]
            + f"\n\n... ({len(output) - _MAX_OUTPUT:,} chars truncated) ...\n\n"
            + output[-half:]
        )
    return output
