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
_READ_ONLY_COMMANDS = {
    "cat", "type", "more", "head", "tail", "pwd", "cd", "ls", "dir",
    "find", "grep", "rg", "git", "python", "python3", "py",
}
_READ_ONLY_GIT_SUBCOMMANDS = {
    "status", "diff", "show", "log", "branch", "rev-parse", "ls-files",
}
_READ_ONLY_SIMPLE_FLAGS = {
    "cat": set(),
    "type": set(),
    "more": set(),
    "head": {"-n"},
    "tail": {"-n"},
    "pwd": set(),
    "cd": set(),
    "ls": {"-a", "-l", "-la", "-al", "-h", "-r", "-t", "-s"},
    "dir": {"/a", "/b", "/o", "/s"},
    "find": set(),
    "grep": {"-i", "-n", "-r", "-l", "-c", "-w", "-E", "-e", "--color"},
    "rg": {"-i", "-n", "-l", "-c", "-w", "-g", "-t", "-uu", "--files"},
}
_READ_ONLY_GIT_GLOBAL_FLAGS = {"--no-pager"}
_READ_ONLY_GIT_ARG_FLAGS = {
    "status": {"--short", "--branch", "--porcelain", "-s", "-b"},
    "diff": {"--stat", "--name-only", "--name-status", "--cached", "--staged"},
    "show": {"--stat", "--name-only", "--name-status"},
    "log": {"--oneline", "--stat", "--decorate", "-n"},
    "branch": {"-a", "-r", "--show-current"},
    "rev-parse": set(),
    "ls-files": {"--others", "--cached", "--modified", "--deleted", "--exclude-standard"},
}
_WRITE_OPERATORS = (">", ">>", "2>", "2>>", "&>", "1>", "1>>", "<")
_CONTROL_OPERATORS = {"|", "&&", "||", ";"}
_READ_ONLY_PIPED_COMMANDS = {"grep", "rg", "find"}


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


def is_read_only_command(command: str) -> bool:
    """
    Conservative read-only classifier for shell commands.

    This is intentionally much smaller than claude-code's Bash/PowerShell
    validators, but it borrows the same orchestration idea: only commands that
    are clearly read-only should be eligible for parallel execution.
    """
    stripped = command.strip()
    if not stripped:
        return False
    if any(op in stripped for op in _WRITE_OPERATORS):
        return False
    if any(token in stripped for token in ("$(", "`")):
        return False
    if "|" in stripped:
        parts = [part.strip() for part in stripped.split("|")]
        if not parts or any(not part for part in parts):
            return False
        return all(_is_read_only_segment(part, allow_piped=True) for part in parts)

    segments = _split_segments(stripped)
    if not segments:
        return False
    return all(_is_read_only_segment(segment, allow_piped=False) for segment in segments)


def _split_segments(command: str) -> list[str]:
    tokens = command.split()
    current: list[str] = []
    segments: list[str] = []
    for token in tokens:
        if token in _CONTROL_OPERATORS:
            if token == "|":
                return []
            if current:
                segments.append(" ".join(current))
                current = []
            continue
        current.append(token)
    if current:
        segments.append(" ".join(current))
    return segments


def _normalize_flag(token: str) -> str:
    if "=" in token:
        return token.split("=", 1)[0]
    return token


def _is_read_only_segment(segment: str, *, allow_piped: bool) -> bool:
    tokens = segment.split()
    if not tokens:
        return False
    cmd = tokens[0].lower()
    args = tokens[1:]
    if cmd not in _READ_ONLY_COMMANDS:
        return False
    if any(arg.startswith((">", ">>", "<")) for arg in args):
        return False
    if any(any(ch in arg for ch in ("$", "`")) for arg in args):
        return False

    if cmd == "git":
        return _is_read_only_git(args)
    if cmd in {"python", "python3", "py"}:
        return _is_read_only_python(args)
    if allow_piped and cmd not in _READ_ONLY_PIPED_COMMANDS:
        return False
    return _validate_simple_read_only_args(cmd, args)


def _validate_simple_read_only_args(cmd: str, args: list[str]) -> bool:
    allowed_flags = _READ_ONLY_SIMPLE_FLAGS.get(cmd, set())
    expects_value_for_previous = False
    for arg in args:
        if expects_value_for_previous:
            expects_value_for_previous = False
            continue
        if arg.startswith("-") or (cmd == "dir" and arg.startswith("/")):
            flag = _normalize_flag(arg)
            if flag not in allowed_flags:
                return False
            if flag in {"-n", "-e", "-g", "-t"}:
                expects_value_for_previous = "=" not in arg
            continue
        # positional args are fine for read/search commands
    return not expects_value_for_previous


def _is_read_only_git(args: list[str]) -> bool:
    if not args:
        return False

    idx = 0
    while idx < len(args) and args[idx].startswith("-"):
        flag = _normalize_flag(args[idx].lower())
        if flag not in _READ_ONLY_GIT_GLOBAL_FLAGS:
            return False
        idx += 1

    if idx >= len(args):
        return False

    subcommand = args[idx].lower()
    if subcommand not in _READ_ONLY_GIT_SUBCOMMANDS:
        return False

    allowed_flags = _READ_ONLY_GIT_ARG_FLAGS.get(subcommand, set())
    expects_value_for_previous = False
    for arg in args[idx + 1:]:
        if expects_value_for_previous:
            expects_value_for_previous = False
            continue
        if arg.startswith("-"):
            flag = _normalize_flag(arg.lower())
            if flag not in allowed_flags:
                return False
            if flag == "-n":
                expects_value_for_previous = "=" not in arg
            continue
    return not expects_value_for_previous


def _is_read_only_python(args: list[str]) -> bool:
    if not args:
        return False
    return all(arg in {"--version", "-V", "-h", "--help"} for arg in args)
