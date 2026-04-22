"""
edgebot/cli/repl.py - Interactive REPL (async) with Rich UI and prompt_toolkit.

REPL commands: /new /sessions /resume /compact /memory /cron /heartbeat /mcp /tasks /team /inbox /status /help
"""

import json
import shlex
import time
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from edgebot.agent.compression import auto_compact, extract_session_summary
from edgebot.agent.context import build_system_prompt, seed_workspace_templates
from edgebot.agent.loop import agent_loop
from edgebot.agent.memory import MemoryStore, cleanup_memory_files_once, consolidate_memory
from edgebot.cron.service import _get_croniter
from edgebot.cron.types import CronSchedule
from edgebot.config import (
    HEARTBEAT_INTERVAL_SECONDS,
    MCP_CONFIG_PATH,
    MEMORY_CONSOLIDATION_INTERVAL_SECONDS,
    MODEL,
    SESSION_DIR,
    WORKDIR,
)
from edgebot.cron.types import CronJob, CronPayload
from edgebot.heartbeat.service import HeartbeatService
from edgebot.mcp.loader import load_mcp
from edgebot.session.store import SessionStore
from edgebot.tools.registry import BG, BUS, CRON, SKILLS, SUBAGENT, TASK_MGR, TEAM, TODO, TOOL_HANDLERS, TOOLS

console = Console()
_MEMORY = MemoryStore(Path.cwd())

_HISTORY_PATH = Path.home() / ".edgebot" / "cli_history"
_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
_prompt_session: PromptSession = PromptSession(history=FileHistory(str(_HISTORY_PATH)))


async def _ask_user() -> str:
    """Prompt the user for a single line; returns stripped text."""
    with patch_stdout():
        line = await _prompt_session.prompt_async(
            HTML("<b><ansiblue>You:</ansiblue></b> ")
        )
    return (line or "").strip()


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can display it safely."""
    ansi_console = Console(
        force_terminal=True,
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


async def _interactive_print(render_fn) -> None:
    """Print async updates without corrupting the active prompt line."""
    def _write() -> None:
        ansi = _render_interactive_ansi(render_fn)
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _interactive_notice(text: str) -> None:
    await _interactive_print(lambda c: c.print(f"[dim]{escape(text)}[/dim]"))


async def _interactive_response(label: str, body: str) -> None:
    safe_label = escape(label)
    safe_body = escape(body)
    await _interactive_print(
        lambda c: (
            c.print(f"[cyan]{safe_label}[/cyan]"),
            c.print(safe_body, highlight=False, soft_wrap=True),
            c.print(),
        )
    )

_HELP_TEXT = """\
[bold]Edgebot commands:[/bold]
  /new            Start a new conversation
  /sessions       List saved sessions
  /resume <#|key> Resume a previous session
  /compact        Compress conversation context
  /memory         Run memory consolidation now
  /cron           Show cron jobs and service state
  /cron list      List jobs in detail
  /cron status    Show cron service status
  /cron run <id>  Run a job immediately
  /cron rm <id>   Remove a job
  /cron on <id>   Enable a job
  /cron off <id>  Disable a job
  /cron add every <seconds> <message>
  /cron add at <iso-datetime> <message>
  /cron add expr <cron-expr> <message> [tz]
  /heartbeat      Trigger one heartbeat tick now
  /mcp            Show MCP servers and loaded capabilities
  /tasks          Show task board
  /team           List teammates
  /subagents      List one-shot subagents
  /inbox          Read inbox
  /status         Show current session info
  /help           Show this help
  exit            Quit"""


_REPLAY_TAIL = 10  # How many visible turns to replay on /resume
_MEMORY_JOB_ID = "memory_consolidation"


def _render_mcp_startup(mcp_client) -> None:
    """Print a concise MCP startup summary."""
    for line in mcp_client.startup_summary_lines():
        console.print(f"[dim]{line}[/dim]")


def _render_mcp_details(mcp_client) -> None:
    """Print detailed MCP summary including capability names."""
    for line in mcp_client.detailed_summary_lines():
        console.print(f"[dim]{line}[/dim]")


def _format_cron_snapshot() -> list[str]:
    status = CRON.status(include_system=False)
    lines = [
        f"  Cron: {'running' if status['enabled'] else 'stopped'}, {status['jobs']} job(s)"
    ]
    for job in CRON.list_jobs(include_disabled=True, include_system=False):
        state_bits = []
        if job.state.next_run_at_ms:
            state_bits.append(
                "next " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job.state.next_run_at_ms / 1000))
            )
        if job.state.last_status:
            state_bits.append(f"last {job.state.last_status}")
        state = f" [{' | '.join(state_bits)}]" if state_bits else ""
        lines.append(f"  - {job.id} {job.name} ({'on' if job.enabled else 'off'}){state}")
    return lines


def _render_cron_table() -> None:
    jobs = CRON.list_jobs(include_disabled=True, include_system=False)
    status = CRON.status(include_system=False)
    console.print(
        f"[dim]  Cron: {'running' if status['enabled'] else 'stopped'}, "
        f"{status['jobs']} job(s)[/dim]"
    )
    if not jobs:
        console.print("[dim]  No scheduled jobs.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("State", no_wrap=True)
    table.add_column("Message")

    for job in jobs:
        if job.schedule.kind == "every" and job.schedule.every_ms:
            schedule = f"every {job.schedule.every_ms // 1000}s"
        elif job.schedule.kind == "cron":
            tz = f" {job.schedule.tz}" if job.schedule.tz else ""
            schedule = f"{job.schedule.expr}{tz}"
        elif job.schedule.kind == "at" and job.schedule.at_ms:
            schedule = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job.schedule.at_ms / 1000))
        else:
            schedule = job.schedule.kind

        state_parts = ["on" if job.enabled else "off"]
        if job.state.next_run_at_ms:
            state_parts.append(
                "next " + time.strftime("%m-%d %H:%M:%S", time.localtime(job.state.next_run_at_ms / 1000))
            )
        if job.state.last_status:
            state_parts.append(f"last {job.state.last_status}")
        message = " ".join(job.payload.message.split())
        if len(message) > 56:
            message = message[:53] + "..."
        table.add_row(job.id, job.name, schedule, " | ".join(state_parts), message)

    console.print(table)


async def _handle_cron_command(query: str) -> None:
    parts = shlex.split(query)
    if len(parts) == 1:
        _render_cron_table()
        return

    sub = parts[1].lower()
    if sub in {"list", "ls"}:
        _render_cron_table()
        return
    if sub == "status":
        for line in _format_cron_snapshot():
            console.print(f"[dim]{line}[/dim]")
        return
    if sub in {"rm", "remove", "run", "on", "off"}:
        if len(parts) < 3:
            console.print(f"[dim]  Usage: /cron {sub} <job_id>[/dim]")
            return
        job_id = parts[2]
        if sub in {"rm", "remove"}:
            result = CRON.remove_job(job_id)
            if result == "removed":
                msg = "Removed " + job_id
            elif result == "protected":
                msg = "Job is system-managed: " + job_id
            else:
                msg = "Job not found: " + job_id
            console.print(f"[dim]  {msg}[/dim]")
            return
        if sub == "run":
            ran = await CRON.run_job(job_id, force=True)
            console.print(f"[dim]  {'Ran ' + job_id if ran else 'Job not found: ' + job_id}[/dim]")
            return
        if sub == "on":
            job = CRON.enable_job(job_id, True)
            if job == "protected":
                msg = "Job is system-managed: " + job_id
            else:
                msg = "Enabled " + job_id if job else "Job not found: " + job_id
            console.print(f"[dim]  {msg}[/dim]")
            return
        if sub == "off":
            job = CRON.enable_job(job_id, False)
            if job == "protected":
                msg = "Job is system-managed: " + job_id
            else:
                msg = "Disabled " + job_id if job else "Job not found: " + job_id
            console.print(f"[dim]  {msg}[/dim]")
            return

    if sub == "add":
        if len(parts) < 5:
            console.print("[dim]  Usage: /cron add every <seconds> <message>[/dim]")
            console.print("[dim]         /cron add at <iso-datetime> <message>[/dim]")
            console.print("[dim]         /cron add expr <cron-expr> <message> [tz][/dim]")
            return
        mode = parts[2].lower()
        if mode == "every":
            try:
                seconds = int(parts[3])
            except ValueError:
                console.print("[dim]  Invalid seconds value.[/dim]")
                return
            message = " ".join(parts[4:]).strip()
            job = CRON.add_job(
                name=message[:40],
                schedule=CronSchedule(kind="every", every_ms=seconds * 1000),
                message=message,
                deliver=True,
                channel="cli",
                to="direct",
                session_key="manual_cron",
            )
            console.print(f"[dim]  Created job {job.id} ({job.name}).[/dim]")
            return
        if mode == "at":
            try:
                at_ms = int(datetime.fromisoformat(parts[3]).timestamp() * 1000)
            except ValueError:
                console.print("[dim]  Invalid ISO datetime.[/dim]")
                return
            message = " ".join(parts[4:]).strip()
            job = CRON.add_job(
                name=message[:40],
                schedule=CronSchedule(kind="at", at_ms=at_ms),
                message=message,
                deliver=True,
                channel="cli",
                to="direct",
                session_key="manual_cron",
                delete_after_run=True,
            )
            console.print(f"[dim]  Created job {job.id} ({job.name}).[/dim]")
            return
        if mode == "expr":
            if len(parts) < 5:
                console.print("[dim]  Usage: /cron add expr <cron-expr> <message> [tz][/dim]")
                return
            if _get_croniter() is None:
                console.print("[dim]  croniter is not installed, cron expressions are unavailable.[/dim]")
                return
            expr = parts[3]
            message = parts[4]
            tz = parts[5] if len(parts) >= 6 else None
            job = CRON.add_job(
                name=message[:40],
                schedule=CronSchedule(kind="cron", expr=expr, tz=tz),
                message=message,
                deliver=True,
                channel="cli",
                to="direct",
                session_key="manual_cron",
            )
            console.print(f"[dim]  Created job {job.id} ({job.name}).[/dim]")
            return

    console.print("[dim]  Unknown /cron usage. Type /help.[/dim]")


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
            "<inbox>",
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
            f"[dim italic]  \u2026 {omitted} earlier message(s) hidden \u2026[/dim italic]\n"
        )

    for msg in shown:
        body = msg["content"]
        if len(body) > 2000:
            body = body[:2000] + f"\n[\u2026 {len(body) - 2000} chars truncated \u2026]"
        # Escape Rich markup so user content like "[bold]" isn't interpreted.
        from rich.markup import escape
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


def _resolve_session_summary(state: dict) -> str | None:
    """Prefer explicit session metadata, then fall back to compacted history."""
    metadata = state.get("metadata", {})
    summary = metadata.get("session_summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return extract_session_summary(state.get("messages", []))


async def main():
    # --- Seed workspace templates (first run) ---
    seed_workspace_templates()
    # Skill loader is instantiated at import time; refresh after first-run seeding.
    SKILLS.reload()

    # --- One-shot dedup cleanup of accumulated memory files ---
    cleanup_memory_files_once()

    # --- Start fresh session (no picker) ---
    store = SessionStore(SESSION_DIR)
    session_key = f"session_{int(time.time())}"
    history: list[dict] = []
    session_summary: str | None = None

    # --- MCP initialization (optional) ---
    mcp_client = await load_mcp(MCP_CONFIG_PATH)
    all_tools = list(TOOLS)
    all_handlers = dict(TOOL_HANDLERS)
    if mcp_client:
        all_tools.extend(mcp_client.tool_schemas)
        all_handlers.update(mcp_client.tool_handlers)
        _render_mcp_startup(mcp_client)

    async def _run_background_turn(
        prompt: str,
        *,
        run_channel: str,
        run_chat_id: str,
        run_session_key: str,
        emit_output: bool,
        assistant_label: str,
    ) -> str | None:
        state = store.load_state(run_session_key)
        run_history = list(state["messages"])
        run_summary = _resolve_session_summary(state)
        user_msg = {"role": "user", "content": prompt}
        store.update_metadata(run_session_key, pending_user_turn=True)
        run_history.append(user_msg)
        store.append(run_session_key, user_msg)
        return await agent_loop(
            messages=run_history,
            system=build_system_prompt(),
            tools=all_tools,
            tool_handlers=all_handlers,
            todo_mgr=TODO,
            bg_mgr=BG,
            bus=BUS,
            session_store=store,
            session_key=run_session_key,
            channel=run_channel,
            chat_id=run_chat_id,
            session_summary=run_summary,
            emit_output=emit_output,
            assistant_label=assistant_label,
        )

    async def _handle_cron_job(job) -> str | None:
        if job.payload.kind == "system_event" and job.id == _MEMORY_JOB_ID:
            await consolidate_memory([], store=_MEMORY, emit_output=False)
            return None
        await _interactive_notice(f"[cron] running {job.name} ({job.id})")
        response = await _run_background_turn(
            job.payload.message,
            run_channel=job.payload.channel or "cron",
            run_chat_id=job.payload.to or "cron",
            run_session_key=job.payload.session_key or f"cron_{job.id}",
            emit_output=False,
            assistant_label=f"cron:{job.name}",
        )
        if job.payload.deliver and response and not response.isspace():
            await _interactive_notice(f"[cron] completed {job.name}")
            await _interactive_response(f"cron:{job.name}", response)
        return response

    async def _heartbeat_execute(tasks: str) -> str | None:
        await _interactive_notice("[heartbeat] active task detected")
        return await _run_background_turn(
            tasks,
            run_channel="heartbeat",
            run_chat_id="heartbeat",
            run_session_key="heartbeat",
            emit_output=False,
            assistant_label="heartbeat",
        )

    async def _heartbeat_notify(response: str) -> None:
        await _interactive_notice("[heartbeat] result")
        await _interactive_response("heartbeat", response)

    heartbeat = HeartbeatService(
        WORKDIR,
        on_execute=_heartbeat_execute,
        on_notify=_heartbeat_notify,
        interval_s=HEARTBEAT_INTERVAL_SECONDS,
    )
    CRON.set_handler(_handle_cron_job)
    CRON.register_system_job(CronJob(
        id=_MEMORY_JOB_ID,
        name="memory_consolidation",
        schedule=CronSchedule(kind="every", every_ms=MEMORY_CONSOLIDATION_INTERVAL_SECONDS * 1000),
        payload=CronPayload(kind="system_event"),
    ))
    await CRON.start()
    await heartbeat.start()

    # --- Welcome banner ---
    _LOGO = r"""
[bold cyan]    ,──────.    [/bold cyan]
[bold cyan]   / ,────. \   [/bold cyan]    [bold white]E D G E B O T[/bold white]
[bold blue]  / / ,──┐ \ \  [/bold blue]    [dim]Autonomous Workspace Agent[/dim]
[bold blue]  \ \ └──/ / /  [/bold blue]
[bold magenta]   \ ─────/ /   [/bold magenta]    [dim]Model:[/dim] [cyan]{model}[/cyan]
[bold purple]    `──────'    [/bold purple]
"""
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

    try:
        while True:
            try:
                query = await _ask_user()
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
                console.print(f"[dim]  Summary : {'yes' if session_summary else 'none'}[/dim]")
                for line in _format_cron_snapshot():
                    console.print(f"[dim]{line}[/dim]")
                hb = heartbeat.status()
                console.print(
                    f"[dim]  Heartbeat: {'running' if hb['running'] else 'stopped'}, "
                    f"every {hb['interval_s']}s, file={'yes' if hb['present'] else 'no'}[/dim]"
                )
                if hb.get("last_action"):
                    console.print(
                        f"[dim]  Heartbeat last: {hb['last_action']} "
                        f"({hb.get('last_reason') or 'n/a'})[/dim]"
                    )
                if mcp_client and mcp_client.connected_servers:
                    console.print(
                        f"[dim]  MCP     : {len(mcp_client.connected_servers)} server(s), "
                        f"{len(mcp_client.tool_schemas)} capabilities[/dim]"
                    )
                    for line in mcp_client.startup_summary_lines():
                        console.print(f"[dim]  {line}[/dim]")
                else:
                    console.print("[dim]  MCP     : none[/dim]")
                continue

            if query == "/mcp":
                if mcp_client and mcp_client.connected_servers:
                    _render_mcp_details(mcp_client)
                else:
                    console.print("[dim]  No MCP servers connected.[/dim]")
                continue

            if query.startswith("/cron"):
                await _handle_cron_command(query)
                continue

            if query == "/heartbeat":
                console.print("[dim]  Triggering heartbeat...[/dim]")
                result = await heartbeat.trigger_now()
                if result is None:
                    console.print("[dim]  Heartbeat skipped.[/dim]")
                continue

            if query == "/new":
                session_key = f"session_{int(time.time())}"
                history.clear()
                session_summary = None
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
                try:
                    idx = int(arg) - 1
                    if 0 <= idx < len(sessions):
                        target = sessions[idx]
                except ValueError:
                    for s in sessions:
                        if s["key"] == arg:
                            target = s
                            break
                if target:
                    session_key = target["key"]
                    state = store.load_state(session_key)
                    history[:] = state["messages"]
                    session_summary = _resolve_session_summary(state)

                    idle_minutes = (time.time() - target["updated_at"].timestamp()) / 60
                    if history and idle_minutes > 60 and len(history) > 10:
                        console.print(
                            f"[dim]  Idle {int(idle_minutes)}m — compressing older history...[/dim]"
                        )
                        history[:] = await auto_compact(
                            history,
                            is_idle=True,
                            idle_minutes=idle_minutes,
                            memory_store=_MEMORY,
                        )
                        store.save_all(session_key, history)
                        session_summary = extract_session_summary(history)
                        store.update_metadata(session_key, session_summary=session_summary or "")

                    if history:
                        _render_history(history)
                    if session_summary:
                        console.print("[dim]  Resume summary loaded into runtime context.[/dim]")
                    console.print(
                        f"[dim]  Resumed '{session_key}' ({len(history)} messages).[/dim]"
                    )
                else:
                    console.print("[dim]  Session not found.[/dim]")
                continue

            if query == "/compact":
                if history:
                    console.print("[dim]  Compressing...[/dim]")
                    history[:] = await auto_compact(history, is_idle=False, memory_store=_MEMORY)
                    store.save_all(session_key, history)
                    session_summary = extract_session_summary(history)
                    store.update_metadata(session_key, session_summary=session_summary or "")
                    console.print("[dim]  Done.[/dim]")
                continue

            if query == "/memory":
                console.print("[dim]  Running memory consolidation...[/dim]")
                await consolidate_memory(history, store=_MEMORY)
                continue

            if query == "/tasks":
                console.print(TASK_MGR.list_all())
                continue

            if query == "/team":
                console.print(TEAM.list_all())
                continue

            if query == "/subagents":
                sa = SUBAGENT.list_all()
                if not sa:
                    console.print("[dim]  No subagents.[/dim]")
                else:
                    console.print(json.dumps(sa, indent=2))
                continue

            if query == "/inbox":
                console.print(json.dumps(BUS.read_inbox("lead"), indent=2))
                continue

            if query.startswith("/"):
                console.print(f"[dim]  Unknown command: {query}. Type /help.[/dim]")
                continue

            # ---- Normal message ----
            user_msg = {"role": "user", "content": query}
            store.update_metadata(session_key, pending_user_turn=True)
            history.append(user_msg)
            store.append(session_key, user_msg)
            system = build_system_prompt()

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
                channel="cli",
                chat_id="direct",
                session_summary=session_summary,
            )
            print()

    finally:
        await heartbeat.stop()
        await CRON.stop()
        if mcp_client:
            await mcp_client.close()
        console.print("\n[dim]Goodbye![/dim]")
