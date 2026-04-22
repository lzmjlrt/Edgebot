"""Built-in cron management tool."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from edgebot.cron.service import _get_croniter
from edgebot.cron.types import CronJob, CronJobState, CronSchedule
from edgebot.tools.base import BaseTool


class CronTool(BaseTool):
    def __init__(self, cron_service):
        self._cron = cron_service
        self._channel = "cli"
        self._chat_id = "direct"
        self._session_key: str | None = None

    def set_runtime_context(
        self,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        session_key: str | None = None,
    ) -> None:
        self._channel = channel
        self._chat_id = chat_id
        self._session_key = session_key

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule and manage recurring agent tasks. "
            "Supports add, list, remove, enable, disable, run, and status."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove", "enable", "disable", "run", "status"],
                },
                "name": {"type": "string"},
                "message": {"type": "string"},
                "every_seconds": {"type": "integer", "minimum": 1},
                "cron_expr": {"type": "string"},
                "tz": {"type": "string"},
                "at": {
                    "type": "string",
                    "description": "ISO datetime, e.g. 2026-04-22T18:30:00",
                },
                "deliver": {"type": "boolean", "default": True},
                "job_id": {"type": "string"},
            },
            "required": ["action"],
        }

    def execute(self, **kwargs: Any) -> Any:
        action = kwargs["action"]
        if action == "add":
            return self._add_job(**kwargs)
        if action == "list":
            return self._list_jobs()
        if action == "remove":
            return self._remove_job(kwargs.get("job_id"))
        if action == "enable":
            return self._toggle_job(kwargs.get("job_id"), True)
        if action == "disable":
            return self._toggle_job(kwargs.get("job_id"), False)
        if action == "run":
            return self._run_job(kwargs.get("job_id"))
        if action == "status":
            return self._format_status()
        return f"Unknown action: {action}"

    def _add_job(
        self,
        *,
        name: str | None = None,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        deliver: bool = True,
        **_: Any,
    ) -> str:
        if not message.strip():
            return "Error: message is required for add"
        if sum(bool(x) for x in (every_seconds, cron_expr, at)) != 1:
            return "Error: provide exactly one of every_seconds, cron_expr, or at"

        delete_after_run = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            if _get_croniter() is None:
                return "Error: cron expression support requires the optional dependency 'croniter'"
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
        else:
            try:
                dt = datetime.fromisoformat(at or "")
            except ValueError:
                return "Error: invalid ISO datetime for 'at'"
            at_ms = int(dt.timestamp() * 1000)
            if at_ms <= int(datetime.now().timestamp() * 1000):
                return "Error: 'at' must be in the future"
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after_run = True

        job = self._cron.add_job(
            name=(name or message[:40]).strip(),
            schedule=schedule,
            message=message.strip(),
            deliver=deliver,
            channel=self._channel,
            to=self._chat_id,
            session_key=self._session_key,
            delete_after_run=delete_after_run,
        )
        return f"Created job '{job.name}' (id: {job.id})"

    def _format_schedule(self, schedule: CronSchedule) -> str:
        if schedule.kind == "every" and schedule.every_ms:
            return f"every {schedule.every_ms // 1000}s"
        if schedule.kind == "cron":
            tz = f" ({schedule.tz})" if schedule.tz else ""
            return f"cron {schedule.expr}{tz}"
        if schedule.kind == "at" and schedule.at_ms:
            return datetime.fromtimestamp(schedule.at_ms / 1000).isoformat(sep=" ", timespec="seconds")
        return schedule.kind

    def _format_state(self, state: CronJobState) -> str:
        bits: list[str] = []
        if state.next_run_at_ms:
            bits.append(
                "next="
                + datetime.fromtimestamp(state.next_run_at_ms / 1000).isoformat(sep=" ", timespec="seconds")
            )
        if state.last_run_at_ms:
            bits.append(
                "last="
                + datetime.fromtimestamp(state.last_run_at_ms / 1000).isoformat(sep=" ", timespec="seconds")
            )
        if state.last_status:
            bits.append(f"status={state.last_status}")
        if state.last_error:
            bits.append(f"error={state.last_error}")
        return ", ".join(bits)

    def _message_preview(self, message: str, limit: int = 80) -> str:
        compact = " ".join(message.split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    def _render_job(self, job: CronJob) -> str:
        enabled = "enabled" if job.enabled else "disabled"
        state = self._format_state(job.state)
        state_line = f" [{state}]" if state else ""
        deliver = "deliver" if job.payload.deliver else "silent"
        managed = ", system" if job.system_managed else ""
        return (
            f"- {job.name} ({job.id}, {enabled}, {deliver}{managed}, {self._format_schedule(job.schedule)}){state_line}\n"
            f"  message: {self._message_preview(job.payload.message)}"
        )

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs(include_disabled=True, include_system=False)
        if not jobs:
            return "No scheduled jobs."
        return "Scheduled jobs:\n" + "\n".join(self._render_job(job) for job in jobs)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required"
        result = self._cron.remove_job(job_id)
        if result == "removed":
            return f"Removed job {job_id}"
        if result == "protected":
            return f"Job {job_id} is system-managed and cannot be removed"
        return f"Job {job_id} not found"

    def _toggle_job(self, job_id: str | None, enabled: bool) -> str:
        if not job_id:
            return "Error: job_id is required"
        job = self._cron.enable_job(job_id, enabled=enabled)
        if job == "protected":
            return f"Job {job_id} is system-managed and cannot be {'enabled' if enabled else 'disabled'}"
        if job is None:
            return f"Job {job_id} not found"
        return f"{'Enabled' if enabled else 'Disabled'} job {job_id}"

    async def _run_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required"
        ran = await self._cron.run_job(job_id, force=True)
        if not ran:
            return f"Job {job_id} not found"
        return f"Ran job {job_id}"

    def _format_status(self) -> str:
        status = self._cron.status(include_system=False)
        next_wake = status.get("next_wake_at_ms")
        next_text = (
            datetime.fromtimestamp(next_wake / 1000).isoformat(sep=" ", timespec="seconds")
            if isinstance(next_wake, int)
            else "none"
        )
        return f"Cron service: enabled={status['enabled']}, jobs={status['jobs']}, next_wake={next_text}"
