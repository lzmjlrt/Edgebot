"""
edgebot/cli/repl.py - Interactive REPL (async) with Rich UI.

REPL commands: /new /sessions /resume /compact /memory /tasks /team /inbox /status /help
"""

import json
import time

from rich.console import Console
from rich.markdown import Markdown

from edgebot.agent.compression import auto_compact
from edgebot.agent.context import build_system_prompt, seed_workspace_templates
from edgebot.agent.loop import agent_loop
from edgebot.agent.memory import consolidate_memory
from edgebot.config import MCP_CONFIG_PATH, MODEL, SESSION_DIR
from edgebot.mcp.loader import load_mcp
from edgebot.session.store import SessionStore
from edgebot.tools.registry import BG, BUS, SKILLS, TASK_MGR, TEAM, TODO, TOOL_HANDLERS, TOOLS

console = Console()

_HELP_TEXT = """\
[bold]Edgebot commands:[/bold]
  /new            Start a new conversation
  /sessions       List saved sessions
  /resume <#|key> Resume a previous session
  /compact        Compress conversation context
  /memory         Run memory consolidation now
  /tasks          Show task board
  /team           List teammates
  /inbox          Read inbox
  /status         Show current session info
  /help           Show this help
  exit            Quit"""


def _time_ago(dt) -> str:
    delta = time.time() - dt.timestamp()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


async def main():
    # --- Seed workspace templates (first run) ---
    seed_workspace_templates()

    # --- Start fresh session (no picker) ---
    store = SessionStore(SESSION_DIR)
    session_key = f"session_{int(time.time())}"
    history: list[dict] = []

    # --- MCP initialization (optional) ---
    mcp_client = await load_mcp(MCP_CONFIG_PATH)
    all_tools = list(TOOLS)
    all_handlers = dict(TOOL_HANDLERS)
    if mcp_client:
        all_tools.extend(mcp_client.tool_schemas)
        all_handlers.update(mcp_client.tool_handlers)
        console.print(f"[dim][mcp] {len(mcp_client.tool_schemas)} tools loaded.[/dim]")

    system = build_system_prompt(SKILLS.descriptions())

    # --- Welcome banner ---
    console.print(
        f"\n[bold]Edgebot[/bold] [dim]({MODEL})[/dim] "
        f"— type [bold]/help[/bold] for commands, [bold]exit[/bold] to quit\n"
    )

    try:
        while True:
            try:
                query = input("\033[1;34mYou:\033[0m ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not query:
                continue
            if query.lower() in ("exit", "quit", "/exit", "/quit"):
                break

            # ---- REPL commands ----
            if query == "/help":
                console.print(_HELP_TEXT)
                continue

            if query == "/status":
                console.print(f"[dim]  Session : {session_key}[/dim]")
                console.print(f"[dim]  Messages: {len(history)}[/dim]")
                console.print(f"[dim]  Model   : {MODEL}[/dim]")
                continue

            if query == "/new":
                session_key = f"session_{int(time.time())}"
                history.clear()
                console.print("[dim]  New session started.[/dim]")
                continue

            if query == "/sessions":
                sessions = store.list_sessions()
                if not sessions:
                    console.print("[dim]  No saved sessions.[/dim]")
                else:
                    for i, s in enumerate(sessions[:15], 1):
                        ago = _time_ago(s["updated_at"])
                        cur = " [bold cyan]<-[/bold cyan]" if s["key"] == session_key else ""
                        console.print(
                            f"  [dim]{i:>2}.[/dim] {s['key']}  "
                            f"[dim]({s['message_count']} msgs, {ago})[/dim]{cur}"
                        )
                continue

            if query.startswith("/resume"):
                arg = query[len("/resume"):].strip()
                if not arg:
                    console.print("[dim]  Usage: /resume <#> or /resume <session_key>[/dim]")
                    continue
                sessions = store.list_sessions()
                target = None
                # Try numeric index
                try:
                    idx = int(arg) - 1
                    if 0 <= idx < len(sessions):
                        target = sessions[idx]
                except ValueError:
                    # Try key match
                    for s in sessions:
                        if s["key"] == arg:
                            target = s
                            break
                if target:
                    session_key = target["key"]
                    history[:] = store.load(session_key)
                    console.print(
                        f"[dim]  Resumed '{session_key}' ({len(history)} messages).[/dim]"
                    )
                else:
                    console.print("[dim]  Session not found.[/dim]")
                continue

            if query == "/compact":
                if history:
                    console.print("[dim]  Compressing...[/dim]")
                    history[:] = await auto_compact(history)
                    store.save_all(session_key, history)
                    console.print("[dim]  Done.[/dim]")
                continue

            if query == "/memory":
                console.print("[dim]  Running memory consolidation...[/dim]")
                await consolidate_memory(history)
                continue

            if query == "/tasks":
                console.print(TASK_MGR.list_all())
                continue

            if query == "/team":
                console.print(TEAM.list_all())
                continue

            if query == "/inbox":
                console.print(json.dumps(BUS.read_inbox("lead"), indent=2))
                continue

            if query.startswith("/"):
                console.print(f"[dim]  Unknown command: {query}. Type /help.[/dim]")
                continue

            # ---- Normal message ----
            user_msg = {"role": "user", "content": query}
            history.append(user_msg)
            store.append(session_key, user_msg)

            await agent_loop(
                messages=history,
                system=system,
                tools=all_tools,
                tool_handlers=all_handlers,
                todo_mgr=TODO,
                bg_mgr=BG,
                bus=BUS,
                session_store=store,
                session_key=session_key,
            )
            print()

    finally:
        if len(history) >= 4:
            console.print("[dim]  Consolidating memory...[/dim]")
            try:
                await consolidate_memory(history)
            except Exception:
                pass
        if mcp_client:
            await mcp_client.close()
        console.print("\n[dim]Goodbye![/dim]")
