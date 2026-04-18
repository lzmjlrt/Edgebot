"""
edgebot/subagent/capabilities.py - Capability matrix for one-shot subagents.

Each capability = {system, tools, handlers}. Privileges are enforced by
which tools are injected into the LLM's tool list, not by runtime checks.
"""

from edgebot.tools.filesystem import run_edit, run_read, run_write
from edgebot.tools.shell import run_bash


def _tool(name: str, description: str, parameters: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


_READ_SCHEMA = _tool(
    "read_file", "Read a file.",
    {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)
_BASH_SCHEMA = _tool(
    "bash", "Run a shell command (prefer read-only: ls/cat/grep/rg/find).",
    {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
)
_WRITE_SCHEMA = _tool(
    "write_file", "Write a file.",
    {"type": "object",
     "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
     "required": ["path", "content"]},
)
_EDIT_SCHEMA = _tool(
    "edit_file", "Edit a file by exact string replace.",
    {"type": "object",
     "properties": {"path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"}},
     "required": ["path", "old_text", "new_text"]},
)

_READ_TOOLS = [_READ_SCHEMA, _BASH_SCHEMA]
_READ_HANDLERS = {
    "read_file": lambda **kw: run_read(kw["path"]),
    "bash":      lambda **kw: run_bash(kw["command"]),
}

_FULL_TOOLS = _READ_TOOLS + [_WRITE_SCHEMA, _EDIT_SCHEMA]
_FULL_HANDLERS = {
    **_READ_HANDLERS,
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

CAPABILITIES = {
    "explore": {
        "system": (
            "You are a read-only exploration subagent. Investigate the task and report findings "
            "concisely. Never write files; never modify state. When done, reply with a final "
            "summary (no tool call)."
        ),
        "tools": _READ_TOOLS,
        "handlers": _READ_HANDLERS,
    },
    "builder": {
        "system": (
            "You are a builder subagent. Implement the requested change, then return a short "
            "summary of exactly what you changed. Keep the diff minimal and focused."
        ),
        "tools": _FULL_TOOLS,
        "handlers": _FULL_HANDLERS,
    },
    "reviewer": {
        "system": (
            "You are a code-review subagent. Read only. Output a structured review with these "
            "sections: **Issues**, **Suggestions**, **Verdict** (approve|reject)."
        ),
        "tools": _READ_TOOLS,
        "handlers": _READ_HANDLERS,
    },
}
