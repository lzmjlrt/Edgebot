"""
edgebot/cli/cron_commands.py - /cron command handler and cron rendering.
"""

import shlex
import time
from datetime import datetime

from rich.table import Table

from edgebot.cli.ui_state import console
from edgebot.cron.service import _get_croniter
from edgebot.cron.types import CronSchedule
from edgebot.tools.registry import CRON


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

    console.print("[dim]  Unknown /cron usage. Subcommands: list, run, rm, on, off, add[/dim]")
