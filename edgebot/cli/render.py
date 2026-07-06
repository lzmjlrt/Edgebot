"""
edgebot/cli/render.py - Read-only Rich renderers for the REPL.

History replay, subagent tables, MCP summaries, the startup banner, and
the /help text. No interactive input.
"""

import time
from datetime import datetime

from rich.markup import escape
from rich.table import Table

from edgebot.cli.cron_commands import _format_cron_snapshot
from edgebot.cli.ui_state import console
from edgebot.config import MODEL
from edgebot.tools.registry import SUBAGENT

_HELP_TEXT = """[bold]Edgebot commands:[/bold]
  /new                Start a new conversation
  /sessions           List saved sessions
  /resume             Interactively pick a session to resume
  /resume <#|key>     Resume a specific session
  /compact            Compress conversation context
  /memory             Run memory consolidation now
  /dream-log          Show the latest Dream memory change
  /dream-log <sha>    Show a specific Dream memory change
  /dream-restore      List recent Dream memory versions
  /dream-restore <sha> Restore a Dream memory version
  /cron               Show cron jobs and service state
  /cron run <id>      Run a job immediately
  /cron rm <id>       Remove a job
  /cron on/off <id>   Enable or disable a job
  /cron add every <seconds> <message>
  /cron add at <iso-datetime> <message>
  /cron add expr <cron-expr> <message> [tz]
  /heartbeat          Trigger one heartbeat tick now
  /mcp                Show MCP servers and loaded capabilities
  /tasks              Show task board
  /bg                 Show all background tasks
  /bg <id>            Show one background task
  /bg output <id>     Show task output
  /subagents          List subagents
  /subagents <id>     Show subagent details
  /subagents output <id>
  /subagents transcript <id>
  /subagents fg <id>  Move subagent to foreground
  /subagents bg <id>  Move subagent to background
  /subagents stop <id> [reason]
  /permissions        Show permission rules (persisted + session)
  /status             Show current session info
  /help               Show this help
  /exit                Quit"""


_REPLAY_TAIL = 10  # How many visible turns to replay on /resume
_SESSION_PICK_LIMIT = 20


def _render_mcp_startup(mcp_client) -> None:
    """Print a concise MCP startup summary."""
    for line in mcp_client.startup_summary_lines():
        console.print(f"[dim]{line}[/dim]")


def _render_mcp_details(mcp_client) -> None:
    """Print detailed MCP summary including capability names."""
    for line in mcp_client.detailed_summary_lines():
        console.print(f"[dim]{line}[/dim]")


def _render_history(history: list[dict]) -> None:
    """Pretty-print previous conversation after /resume, dimmed with a divider."""
    def _is_visible(m: dict) -> bool:
        if m.get("role") not in ("user", "assistant"):
            return False
        c = m.get("content")
        if not isinstance(c, str) or not c.strip():
            return False
        skip_prefixes = (
            "<background-results>",
            "<reminder>",
            "[System: Context auto-compressed",
            "[System: User was idle",
        )
        return not c.startswith(skip_prefixes)

    visible = [m for m in history if _is_visible(m)]
    if not visible:
        return

    shown = visible[-_REPLAY_TAIL:]
    omitted = len(visible) - len(shown)

    console.rule("[dim]session history[/dim]", style="dim")
    if omitted > 0:
        console.print(
            f"[dim italic]  … {omitted} earlier message(s) hidden …[/dim italic]\n"
        )

    for msg in shown:
        body = msg["content"]
        if len(body) > 2000:
            body = body[:2000] + f"\n[… {len(body) - 2000} chars truncated …]"
        # Escape Rich markup so user content like "[bold]" isn't interpreted.
        body_esc = escape(body)
        if msg["role"] == "user":
            console.print(f"[dim bold]You:[/dim bold] [dim]{body_esc}[/dim]")
        else:
            console.print(f"[dim bold cyan]Edgebot:[/dim bold cyan] [dim]{body_esc}[/dim]")
        console.print()

    console.rule(style="dim")
    console.print()


def _time_ago(dt) -> str:
    delta = time.time() - dt.timestamp()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _format_timestamp(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _render_subagent_list() -> None:
    tasks = SUBAGENT.list_all()
    if not tasks:
        console.print("[dim]  No subagents.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Mode", no_wrap=True)
    table.add_column("Capability", no_wrap=True)
    table.add_column("Tools", no_wrap=True)
    table.add_column("Started", no_wrap=True)
    table.add_column("Description")

    for task in tasks:
        mode = "bg" if task.get("is_backgrounded", True) else "fg"
        table.add_row(
            task["task_id"],
            task["status"],
            mode,
            task.get("capability", "-"),
            str(task.get("tool_uses", 0)),
            _format_timestamp(task.get("started_at")),
            task.get("description", ""),
        )
    console.print(table)


def _print_subagent_blob(title: str, body: str) -> None:
    console.rule(f"[dim]{title}[/dim]", style="dim")
    console.print(escape(body) if body else "[dim](empty)[/dim]", highlight=False, soft_wrap=True)


def _render_subagent_detail(task: dict[str, object]) -> None:
    if task.get("error") and "task_id" not in task:
        console.print(f"[dim]  {task['error']}[/dim]")
        return

    status = str(task.get("status", "-"))
    mode = "background" if task.get("is_backgrounded", True) else "foreground"
    console.print(f"[cyan]{escape(str(task.get('task_id', '-')))}[/cyan] [dim]({escape(status)} / {mode})[/dim]")
    console.print(f"[dim]  Capability : {escape(str(task.get('capability', '-')))}[/dim]")
    console.print(f"[dim]  Description: {escape(str(task.get('description', '')))}[/dim]")
    console.print(f"[dim]  Started    : {_format_timestamp(task.get('started_at'))}[/dim]")
    console.print(f"[dim]  Finished   : {_format_timestamp(task.get('finished_at'))}[/dim]")
    console.print(f"[dim]  Tool uses  : {task.get('tool_uses', 0)}[/dim]")
    console.print(f"[dim]  Output     : {escape(str(task.get('output_file', '-')))}[/dim]")
    console.print(f"[dim]  Transcript : {escape(str(task.get('transcript_file', '-')))}[/dim]")
    if task.get("stop_requested"):
        console.print(f"[dim]  Stop req   : {escape(str(task.get('stop_reason') or 'requested'))}[/dim]")
    if task.get("error") and status not in {"running"}:
        console.print(f"[dim]  Error      : {escape(str(task.get('error')))}[/dim]")

    output_preview = str(task.get("output_preview", "") or "")
    transcript_preview = str(task.get("transcript_preview", "") or "")
    if output_preview:
        _print_subagent_blob("output preview", output_preview)
    if transcript_preview:
        _print_subagent_blob("transcript preview", transcript_preview)
    console.print()


_LOGO = """
[bold cyan]    ,------.    [/bold cyan]
[bold cyan]   / ,----. \\   [/bold cyan]    [bold white]E D G E B O T[/bold white]
[bold blue]  / / ,--| \\ \\  [/bold blue]    [dim]Autonomous Workspace Agent[/dim]
[bold blue]  \\ \\ `--/ / /  [/bold blue]
[bold magenta]   \\ -----/ /   [/bold magenta]    [dim]Model:[/dim] [cyan]{model}[/cyan]
[bold purple]    `------'    [/bold purple]
"""


def _print_banner(heartbeat) -> None:
    console.print(_LOGO.format(model=MODEL))
    for line in _format_cron_snapshot():
        console.print(f"[dim]{line}[/dim]")
    hb = heartbeat.status()
    console.print(
        f"[dim]  Heartbeat: {'running' if hb['running'] else 'stopped'}, "
        f"every {hb['interval_s']}s, file={'yes' if hb['present'] else 'no'}[/dim]"
    )
    if hb.get("last_action"):
        console.print(f"[dim]  Heartbeat last: {hb['last_action']} ({hb.get('last_reason') or 'n/a'})[/dim]")
    console.print("  [dim]Type [bold]/help[/bold] for commands, [bold]exit[/bold] to quit[/dim]\n")
