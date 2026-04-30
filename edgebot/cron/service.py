"""Cron service for scheduled agent turns."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from edgebot.cron.types import CronJob, CronJobState, CronPayload, CronRunRecord, CronSchedule, CronStore


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_croniter():
    try:
        from croniter import croniter
    except Exception:
        return None
    return croniter


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        croniter = _get_croniter()
        if croniter is None:
            return None
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
            base_dt = datetime.fromtimestamp(now_ms / 1000, tz=tz)
            return int(croniter(schedule.expr, base_dt).get_next(datetime).timestamp() * 1000)
        except Exception:
            return None

    return None


def _validate_schedule_for_add(schedule: CronSchedule) -> None:
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")

    if schedule.kind == "every" and (not schedule.every_ms or schedule.every_ms <= 0):
        raise ValueError("every_ms must be > 0")

    if schedule.kind == "at" and not schedule.at_ms:
        raise ValueError("at_ms is required for 'at' schedules")

    if schedule.kind == "cron":
        if not schedule.expr:
            raise ValueError("expr is required for 'cron' schedules")
        if _get_croniter() is None:
            raise ValueError("cron schedules require the optional dependency 'croniter'")
        if schedule.tz:
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(schedule.tz)
            except Exception as exc:
                raise ValueError(f"unknown timezone '{schedule.tz}'") from exc


class CronService:
    """Persistent cron runner for background agent jobs."""

    _MAX_RUN_HISTORY = 20

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Awaitable[str | None]] | None = None,
        max_sleep_ms: int = 300_000,
    ):
        self.store_path = store_path
        self.on_job = on_job
        self.max_sleep_ms = max_sleep_ms
        self._store = CronStore()
        self._running = False
        self._timer_task: asyncio.Task | None = None

    def set_handler(self, on_job: Callable[[CronJob], Awaitable[str | None]] | None) -> None:
        self.on_job = on_job

    def _load_store(self) -> CronStore:
        if not self.store_path.exists():
            self._store = CronStore()
            return self._store
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            jobs = [CronJob.from_dict(job) for job in data.get("jobs", [])]
            self._store = CronStore(version=data.get("version", 1), jobs=jobs)
        except Exception:
            self._store = CronStore()
        return self._store

    def _save_store(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(self._store.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _recompute_next_runs(self) -> None:
        now = _now_ms()
        for job in self._store.jobs:
            if job.enabled and job.state.next_run_at_ms is None:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)

    def _get_next_wake_ms(self, *, include_system: bool = True) -> int | None:
        enabled_jobs = [
            job.state.next_run_at_ms
            for job in self._store.jobs
            if job.enabled
            and job.state.next_run_at_ms
            and (include_system or not job.system_managed)
        ]
        return min(enabled_jobs) if enabled_jobs else None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()

    async def stop(self) -> None:
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass
            self._timer_task = None

    def _arm_timer(self) -> None:
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
        if not self._running:
            return

        next_wake = self._get_next_wake_ms()
        delay_ms = self.max_sleep_ms if next_wake is None else min(self.max_sleep_ms, max(0, next_wake - _now_ms()))

        async def tick() -> None:
            await asyncio.sleep(delay_ms / 1000)
            if self._running:
                await self._on_timer()

        self._timer_task = asyncio.create_task(tick())

    async def _on_timer(self) -> None:
        self._load_store()
        due_jobs = [
            job for job in self._store.jobs
            if job.enabled and job.state.next_run_at_ms and _now_ms() >= job.state.next_run_at_ms
        ]
        for job in due_jobs:
            await self._execute_job(job)
        self._save_store()
        self._arm_timer()

    async def _execute_job(self, job: CronJob) -> str | None:
        start_ms = _now_ms()
        result_text = None
        try:
            if self.on_job is None:
                raise RuntimeError("cron handler is not configured")
            result_text = await self.on_job(job)
            job.state.last_status = "ok"
            job.state.last_error = None
        except Exception as exc:
            job.state.last_status = "error"
            job.state.last_error = str(exc)
        end_ms = _now_ms()
        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = end_ms
        job.state.run_history.append(
            CronRunRecord(
                run_at_ms=start_ms,
                status=job.state.last_status or "error",
                duration_ms=end_ms - start_ms,
                error=job.state.last_error,
            )
        )
        job.state.run_history = job.state.run_history[-self._MAX_RUN_HISTORY:]

        if job.schedule.kind == "at":
            if job.delete_after_run:
                self._store.jobs = [existing for existing in self._store.jobs if existing.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
        else:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
        return result_text

    def add_job(
        self,
        *,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        session_key: str | None = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        _validate_schedule_for_add(schedule)
        now = _now_ms()
        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            schedule=schedule,
            payload=CronPayload(
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
                session_key=session_key,
            ),
            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
        )
        self._load_store()
        self._store.jobs.append(job)
        self._save_store()
        if self._running:
            self._arm_timer()
        return job

    def register_system_job(self, job: CronJob) -> CronJob:
        """Create or update a protected system-managed job by id."""
        self._load_store()
        now = _now_ms()
        for existing in self._store.jobs:
            if existing.id != job.id:
                continue
            existing.name = job.name
            existing.system_managed = True
            existing.enabled = job.enabled
            existing.schedule = job.schedule
            existing.payload = job.payload
            existing.delete_after_run = job.delete_after_run
            existing.updated_at_ms = now
            if existing.enabled:
                existing.state.next_run_at_ms = _compute_next_run(existing.schedule, now)
            else:
                existing.state.next_run_at_ms = None
            self._save_store()
            if self._running:
                self._arm_timer()
            return existing

        job.system_managed = True
        if not job.created_at_ms:
            job.created_at_ms = now
        job.updated_at_ms = now
        if job.enabled and job.state.next_run_at_ms is None:
            job.state.next_run_at_ms = _compute_next_run(job.schedule, now)
        self._store.jobs.append(job)
        self._save_store()
        if self._running:
            self._arm_timer()
        return job

    def list_jobs(
        self,
        include_disabled: bool = True,
        *,
        include_system: bool = True,
    ) -> list[CronJob]:
        jobs = self._load_store().jobs
        if not include_disabled:
            jobs = [job for job in jobs if job.enabled]
        if not include_system:
            jobs = [job for job in jobs if not job.system_managed]
        return sorted(jobs, key=lambda job: job.state.next_run_at_ms or float("inf"))

    def get_job(self, job_id: str) -> CronJob | None:
        return next((job for job in self._load_store().jobs if job.id == job_id), None)

    def remove_job(self, job_id: str) -> Literal["removed", "not_found", "protected"]:
        self._load_store()
        target = next((job for job in self._store.jobs if job.id == job_id), None)
        if target and target.system_managed:
            return "protected"
        before = len(self._store.jobs)
        self._store.jobs = [job for job in self._store.jobs if job.id != job_id]
        if len(self._store.jobs) == before:
            return "not_found"
        self._save_store()
        if self._running:
            self._arm_timer()
        return "removed"

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | Literal["protected"] | None:
        self._load_store()
        for job in self._store.jobs:
            if job.id != job_id:
                continue
            if job.system_managed:
                return "protected"
            job.enabled = enabled
            job.updated_at_ms = _now_ms()
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms()) if enabled else None
            self._save_store()
            if self._running:
                self._arm_timer()
            return job
        return None

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        self._load_store()
        for job in self._store.jobs:
            if job.id != job_id:
                continue
            if not force and not job.enabled:
                return False
            await self._execute_job(job)
            self._save_store()
            if self._running:
                self._arm_timer()
            return True
        return False

    def status(self, *, include_system: bool = True) -> dict[str, Any]:
        self._load_store()
        visible_jobs = [
            job for job in self._store.jobs
            if include_system or not job.system_managed
        ]
        return {
            "enabled": self._running,
            "jobs": len(visible_jobs),
            "next_wake_at_ms": self._get_next_wake_ms(include_system=include_system),
        }
