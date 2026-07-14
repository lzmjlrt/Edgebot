"""Shell command execution helpers with basic safety guards."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from edgebot.config import WORKDIR
from edgebot.permissions.defaults import DEFAULT_BASH_DENY_PATTERNS
from edgebot.security.workspace_policy import (
    WORKSPACE_BOUNDARY_NOTE,
    is_path_within_workspace,
    require_workspace_path,
    workspace_root,
)

_MAX_OUTPUT = 10_000
_TIMEOUT = 120
_REPEAT_COLLAPSE_THRESHOLD = 5
# Single source of truth — same regex set the PermissionManager uses.
_DENY_PATTERNS = list(DEFAULT_BASH_DENY_PATTERNS)
_READ_ONLY_COMMANDS = {
    "cat", "type", "more", "head", "tail", "pwd", "cd", "ls", "dir",
    "find", "findstr", "grep", "rg", "git", "python", "python3", "py",
    "powershell", "pwsh", "echo",
}
_READ_ONLY_GIT_SUBCOMMANDS = {
    "status", "diff", "show", "log", "branch", "rev-parse", "ls-files",
}
_READ_ONLY_SIMPLE_FLAGS = {
    "cat": set(),
    "type": set(),
    "more": set(),
    "echo": set(),
    "head": {"-n"},
    "tail": {"-n"},
    "pwd": set(),
    "cd": set(),
    "ls": {"-a", "-l", "-la", "-al", "-h", "-r", "-t", "-s"},
    "dir": {"/a", "/b", "/o", "/s"},
    "find": set(),
    "findstr": {"/i", "/n", "/r", "/c", "/l", "/s", "/m", "/v", "/x"},
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
_READ_ONLY_PIPED_COMMANDS = {
    "cat", "type", "more", "head", "tail", "ls", "dir",
    "find", "findstr", "grep", "rg", "git", "echo",
}
_WINDOWS_SLASH_FLAG_COMMANDS = {"dir", "findstr"}
_POWERSHELL_FLAGS_WITH_VALUE = {"-command", "-c", "-encodedcommand", "-ec"}
_POWERSHELL_READ_ONLY_CMDS = {
    "get-content", "gc", "cat", "type",
    "select-object", "select",
    "select-string", "sls",
    "where-object", "where", "?",
    "sort-object", "sort",
    "format-table", "ft",
    "format-list", "fl",
    "write-output", "echo",
}
_POWERSHELL_WRITE_MARKERS = re.compile(
    r"\b("
    r"set-content|add-content|out-file|tee-object|new-item|remove-item|"
    r"move-item|copy-item|rename-item|clear-content|start-process|stop-process|"
    r"invoke-webrequest|invoke-restmethod|curl|wget|rm|del|erase|rmdir"
    r")\b",
    re.IGNORECASE,
)


def _extract_absolute_paths(command: str) -> list[str]:
    win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]*", command)
    posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command)
    home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command)
    return win_paths + posix_paths + home_paths


def _guard_command(command: str, cwd: str) -> str | None:
    stripped = command.strip()
    for pattern in _DENY_PATTERNS:
        if re.search(pattern, stripped, re.IGNORECASE):
            return "Error: Command blocked by safety guard (dangerous pattern detected)"

    from edgebot.security.network import contains_internal_url

    if contains_internal_url(command):
        return "Error: Command blocked by safety guard (internal/private URL detected)"
    if "..\\" in command or "../" in command:
        return (
            "Error: Command blocked by safety guard (path traversal detected)"
            + WORKSPACE_BOUNDARY_NOTE
        )

    try:
        require_workspace_path(cwd, raw_label="working directory")
    except Exception:
        return (
            "Error: Command blocked by safety guard (working directory outside workspace)"
            + WORKSPACE_BOUNDARY_NOTE
        )

    for raw in _extract_absolute_paths(command):
        try:
            p = Path(os.path.expandvars(raw.strip())).expanduser().resolve(strict=False)
        except Exception:
            continue
        if p.is_absolute() and not is_path_within_workspace(p):
            return (
                "Error: Command blocked by safety guard (path outside working dir)"
                + WORKSPACE_BOUNDARY_NOTE
            )
    return None


def _collapse_repeats(text: str, threshold: int = _REPEAT_COLLAPSE_THRESHOLD) -> str:
    """Compress runs of identical lines into one line plus a counter."""
    if not text:
        return text
    lines = text.splitlines(keepends=True)
    if len(lines) < threshold:
        return text
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i + 1
        while j < len(lines) and lines[j] == lines[i]:
            j += 1
        run = j - i
        if run >= threshold:
            out.append(lines[i])
            sep = "" if lines[i].endswith("\n") else "\n"
            out.append(f"{sep}... [previous line repeated {run - 1} more times]\n")
        else:
            out.extend(lines[i:j])
        i = j
    return "".join(out)


def _format_output(
    stdout: str,
    stderr: str,
    exit_code: int | None,
    *,
    partial: bool = False,
) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr and stderr.strip():
        parts.append(f"STDERR:\n{stderr}")
    if exit_code is not None:
        parts.append(f"\nExit code: {exit_code}")
    elif partial:
        parts.append("\n(process still running — exit code not yet available)")
    output = "\n".join(parts) if parts else "(no output)"
    output = _collapse_repeats(output)
    if len(output) > _MAX_OUTPUT:
        half = _MAX_OUTPUT // 2
        output = (
            output[:half]
            + f"\n\n... ({len(output) - _MAX_OUTPUT:,} chars truncated) ...\n\n"
            + output[-half:]
        )
    return output


def run_bash(command: str) -> str:
    cwd = workspace_root()
    guard_error = _guard_command(command, str(cwd))
    if guard_error:
        return guard_error

    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
        )
    except Exception as e:
        return f"Error executing command: {e}"

    try:
        stdout, stderr = process.communicate(timeout=_TIMEOUT)
        return _format_output(stdout or "", stderr or "", process.returncode)
    except subprocess.TimeoutExpired as exc:
        captured_stdout = exc.stdout or ""
        captured_stderr = exc.stderr or ""
        if isinstance(captured_stdout, bytes):
            captured_stdout = captured_stdout.decode("utf-8", errors="replace")
        if isinstance(captured_stderr, bytes):
            captured_stderr = captured_stderr.decode("utf-8", errors="replace")
        captured = _format_output(
            captured_stdout, captured_stderr, exit_code=None, partial=True
        )
        try:
            from edgebot.tools.registry import BG

            bg_task = BG.adopt(process, command, captured)
            tid = bg_task.get("task_id", "?")
            return (
                f"Command exceeded {_TIMEOUT}s and was moved to background "
                f"(task_id={tid}). Use task_output with that id to retrieve the "
                f"final result.\n\n"
                f"--- Partial output (captured before timeout) ---\n{captured}"
            )
        except Exception as adopt_err:
            # Adoption failed for some reason — kill to avoid orphan and
            # surface what we already captured.
            try:
                process.kill()
            except Exception:
                pass
            return (
                f"Error: Command timed out after {_TIMEOUT} seconds (background "
                f"adoption failed: {adopt_err})\n\n"
                f"--- Partial output ---\n{captured}"
            )
    except Exception as e:
        try:
            process.kill()
        except Exception:
            pass
        return f"Error executing command: {e}"


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
    stripped = _strip_safe_null_redirections(stripped)
    if _looks_like_powershell(stripped):
        return _is_read_only_powershell_command(stripped)
    if any(op in stripped for op in _WRITE_OPERATORS):
        return False
    if any(token in stripped for token in ("$(", "`")):
        return False
    if _has_pipeline(stripped):
        parts = [part.strip() for part in re.split(r"(?<!\|)\|(?!\|)", stripped)]
        if not parts or any(not part for part in parts):
            return False
        return all(_is_read_only_segment(part, allow_piped=True) for part in parts)

    segments = _split_segments(stripped)
    if not segments:
        return False
    return all(_is_read_only_segment(segment, allow_piped=False) for segment in segments)


def is_safe_development_command(command: str) -> bool:
    """Return whether *command* is a narrow, local development operation.

    These commands are eligible for Edgebot's explicit ``auto`` permission
    mode. The allowlist intentionally excludes installers, build commands,
    arbitrary scripts, shell operators, and redirections.
    """
    stripped = command.strip()
    if not stripped or any(token in stripped for token in ("&", "|", ";", ">", "<", "$(", "`")):
        return False
    try:
        import shlex

        tokens = shlex.split(stripped, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False

    program = Path(tokens[0]).stem.lower()
    arguments = [token.lower() for token in tokens[1:]]
    if program in {"pytest", "mypy"}:
        return True
    if program in {"python", "python3", "py"}:
        return len(arguments) >= 2 and arguments[:2] == ["-m", "pytest"]
    if program == "uv":
        return len(arguments) >= 2 and arguments[:2] == ["run", "pytest"]
    if program == "ruff":
        return bool(arguments) and (
            arguments[0] == "check"
            or (arguments[0] == "format" and "--check" in arguments)
        )
    if program == "black":
        return "--check" in arguments
    if program in {"npm", "pnpm", "yarn"}:
        return bool(arguments) and (
            arguments[0] == "test"
            or (
                len(arguments) >= 2
                and arguments[:2] in (["run", "lint"], ["run", "test"], ["run", "typecheck"])
            )
        )
    if program == "go":
        return bool(arguments) and arguments[0] == "test"
    if program == "cargo":
        return bool(arguments) and arguments[0] == "test"
    return False


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


def _has_pipeline(command: str) -> bool:
    return re.search(r"(?<!\|)\|(?!\|)", command) is not None


def _normalize_flag(token: str) -> str:
    if "=" in token:
        return token.split("=", 1)[0]
    return token


def _strip_safe_null_redirections(command: str) -> str:
    """Remove stderr-to-null redirections that do not write workspace files."""
    patterns = (
        r"(?i)\s+2>\s*nul\b",
        r"(?i)\s+2>>\s*nul\b",
        r"(?i)\s+2>\s*/dev/null\b",
        r"(?i)\s+2>>\s*/dev/null\b",
        r"(?i)\s+2>\s*\$null\b",
        r"(?i)\s+2>>\s*\$null\b",
    )
    for pattern in patterns:
        command = re.sub(pattern, "", command)
    return command


def _looks_like_powershell(command: str) -> bool:
    first = command.split(maxsplit=1)[0].lower() if command.split() else ""
    return first in {"powershell", "pwsh"}


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
    if cmd in {"powershell", "pwsh"}:
        return _is_read_only_powershell(args)
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
        if arg.startswith("-") or (cmd in _WINDOWS_SLASH_FLAG_COMMANDS and arg.startswith("/")):
            flag = _normalize_flag(arg)
            if cmd in _WINDOWS_SLASH_FLAG_COMMANDS:
                flag = flag.lower()
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


def _is_read_only_powershell(args: list[str]) -> bool:
    if not args:
        return False

    script: str | None = None
    idx = 0
    while idx < len(args):
        token = args[idx].lower()
        if token in {"-noprofile", "-noninteractive", "-executionpolicy"}:
            idx += 2 if token == "-executionpolicy" else 1
            continue
        if token in _POWERSHELL_FLAGS_WITH_VALUE:
            if idx + 1 >= len(args) or token in {"-encodedcommand", "-ec"}:
                return False
            script = args[idx + 1].strip().strip('"').strip("'")
            break
        return False
    if script is None:
        return False
    return _is_read_only_powershell_script(script)


def _is_read_only_powershell_command(command: str) -> bool:
    try:
        import shlex

        args = shlex.split(command, posix=False)
    except ValueError:
        args = command.split()
    if not args:
        return False
    return _is_read_only_powershell(args[1:])


def _is_read_only_powershell_script(script: str) -> bool:
    if not script:
        return False
    if _POWERSHELL_WRITE_MARKERS.search(script):
        return False
    if re.search(r"(?<![<>=])>(?![=>])|>>|<", script):
        return False
    if any(token in script for token in ("$(", "`")):
        return False

    segments = [seg.strip() for seg in script.split("|")]
    if not segments or any(not seg for seg in segments):
        return False
    for segment in segments:
        head = segment.split(maxsplit=1)[0].strip().lower()
        if head not in _POWERSHELL_READ_ONLY_CMDS:
            return False
    return True
