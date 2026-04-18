"""
edgebot/cli/tool_hints.py - Short, readable summaries of tool calls.
Used to render progress lines like "  ↳ read config.py" in the REPL.
"""


def _trunc(s: str, n: int = 60) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "\u2026"


def format_tool_hint(name: str, args: dict) -> str:
    """Produce a short, readable description of a tool call."""
    if name == "bash":
        return f"$ {_trunc(args.get('command', ''), 70)}"
    if name == "read_file":
        return f"read {args.get('path', '')}"
    if name == "write_file":
        return f"write {args.get('path', '')}"
    if name == "edit_file":
        return f"edit {args.get('path', '')}"
    if name == "task":
        return f"subagent: {_trunc(args.get('prompt', ''), 50)}"
    if name == "load_skill":
        return f"load skill: {args.get('name', '')}"
    if name == "task_create":
        return f"task+ {_trunc(args.get('subject', ''), 50)}"
    if name == "task_update":
        status = args.get("status", "")
        return f"task~ #{args.get('task_id', '?')} \u2192 {status}" if status else f"task~ #{args.get('task_id', '?')}"
    if name == "task_get":
        return f"task? #{args.get('task_id', '?')}"
    if name == "task_list":
        return "tasks (list)"
    if name == "claim_task":
        return f"claim task #{args.get('task_id', '?')}"
    if name == "TodoWrite":
        n = len(args.get("items", []) or [])
        return f"todos ({n} items)"
    if name == "compress":
        return "compress context"
    if name == "background_run":
        return f"bg$ {_trunc(args.get('command', ''), 60)}"
    if name == "check_background":
        tid = args.get("task_id")
        return f"bg check{(' ' + tid) if tid else ''}"
    if name == "spawn_subagent":
        return f"subagent+ [{args.get('capability','?')}] {_trunc(args.get('prompt',''), 50)}"
    if name == "check_subagent":
        return f"subagent? {args.get('task_id','?')}"
    if name == "list_subagents":
        return "subagents (list)"
    if name == "spawn_teammate":
        return f"spawn {args.get('name', '?')} ({args.get('role', '?')})"
    if name == "list_teammates":
        return "team (list)"
    if name == "send_message":
        return f"msg \u2192 {args.get('to', '?')}"
    if name == "read_inbox":
        return "inbox"
    if name == "broadcast":
        return "broadcast"
    if name == "shutdown_request":
        return f"shutdown \u2192 {args.get('teammate', '?')}"
    if name == "plan_approval":
        approve = args.get("approve")
        verdict = "approve" if approve else "reject"
        return f"plan {verdict}"
    if name == "idle":
        return "idle"
    if name.startswith("mcp_"):
        # Strip the mcp_ prefix for readability
        return f"mcp::{name[4:]}"
    return name
