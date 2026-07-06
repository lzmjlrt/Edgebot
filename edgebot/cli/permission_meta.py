"""
edgebot/cli/permission_meta.py - Permission-request classification helpers.

Pure request-dict → string formatting used by the interactive permission
prompts in repl.py. No UI dependencies.
"""

import re
import shlex
from pathlib import Path


def _permission_title(request: dict) -> str:
    tool = str(request.get("tool", "")).strip()
    if tool == "bash":
        return "Bash command"
    if tool == "write_file":
        return "Write file"
    if tool == "edit_file":
        return "Edit file"
    if tool == "background_run":
        return "Background task"
    if tool == "web_fetch":
        return "Web fetch"
    if tool == "web_search":
        return "Web search"
    if tool == "task":
        return "Subagent"
    return f"{tool or 'Tool'} request"


def _permission_subject(request: dict) -> str:
    tool = str(request.get("tool", "")).strip()
    if tool == "bash":
        return str(request.get("raw_command") or request.get("scope_value") or "").strip()
    message = str(request.get("message", "")).strip()
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if len(lines) >= 2:
        return lines[-1]
    return str(request.get("scope_value") or tool or "this operation").strip()


def _permission_description(request: dict) -> str:
    description = str(request.get("description") or "").strip()
    if description:
        return description
    tool = str(request.get("tool", "")).strip()
    if tool == "bash":
        command = _permission_subject(request)
        if "git reset --hard" in command.lower():
            return "Reset git history or working tree state (destructive operation)"
        if re.search(r">\s*\S+", command):
            return "Write command output to a file"
        if request.get("requires_confirmation"):
            return "Sensitive shell operation"
        return "Shell command requested by Edgebot"
    if tool in {"write_file", "edit_file"}:
        return "Modify file contents"
    if tool == "background_run":
        return "Start a background command"
    if tool in {"web_fetch", "web_search"}:
        return "Network access requested by Edgebot"
    if tool == "task":
        return "Start a delegated agent task"
    return "Sensitive tool operation"


def _permission_scope_label(request: dict) -> str:
    tool = str(request.get("tool", "")).strip()
    scope = str(request.get("scope_hint") or "allow_tool")
    subject = _permission_subject(request)
    if tool == "bash":
        if scope == "allow_program":
            program = str(request.get("scope_value") or "").strip()
            return program or subject or "this command"
        pattern = _bash_permission_pattern(subject)
        return pattern or subject or "this command"
    if tool in {"write_file", "edit_file"}:
        try:
            path = Path(subject)
            if not path.is_absolute():
                first = path.parts[0] if path.parts else subject
                return f"{first}{path.anchor or ''} from this project"
        except (OSError, ValueError):
            pass
    return subject or str(request.get("scope_value") or tool or "this tool")


def _bash_permission_pattern(command: str) -> str:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    head = tokens[0]
    if head.lower() == "git" and len(tokens) >= 2:
        return f"git {tokens[1]} *"
    return f"{head} *"
