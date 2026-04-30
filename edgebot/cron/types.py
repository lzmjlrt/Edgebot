"""Cron types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class CronSchedule:
    kind: Literal["at", "every", "cron"]
    at_ms: int | None = None
    every_ms: int | None = None
    expr: str | None = None
    tz: str | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "at_ms": self.at_ms,
            "every_ms": self.every_ms,
            "expr": self.expr,
            "tz": self.tz,
        }


@dataclass
class CronPayload:
    kind: Literal["agent_turn", "system_event"] = "agent_turn"
    message: str = ""
    deliver: bool = False
    channel: str | None = None
    to: str | None = None
    session_key: str | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "deliver": self.deliver,
            "channel": self.channel,
            "to": self.to,
            "session_key": self.session_key,
        }


@dataclass
class CronRunRecord:
    run_at_ms: int
    status: Literal["ok", "error", "skipped"]
    duration_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "run_at_ms": self.run_at_ms,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass
class CronJobState:
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None
    run_history: list[CronRunRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "next_run_at_ms": self.next_run_at_ms,
            "last_run_at_ms": self.last_run_at_ms,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "run_history": [record.to_dict() for record in self.run_history],
        }


@dataclass
class CronJob:
    id: str
    name: str
    system_managed: bool = False
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "system_managed": self.system_managed,
            "enabled": self.enabled,
            "schedule": self.schedule.to_dict(),
            "payload": self.payload.to_dict(),
            "state": self.state.to_dict(),
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "delete_after_run": self.delete_after_run,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CronJob":
        state_data = dict(data.get("state", {}))
        state_data["run_history"] = [
            record if isinstance(record, CronRunRecord) else CronRunRecord(**record)
            for record in state_data.get("run_history", [])
        ]
        return cls(
            id=data["id"],
            name=data["name"],
            system_managed=data.get("system_managed", False),
            enabled=data.get("enabled", True),
            schedule=CronSchedule(**data.get("schedule", {"kind": "every"})),
            payload=CronPayload(**data.get("payload", {})),
            state=CronJobState(**state_data),
            created_at_ms=data.get("created_at_ms", 0),
            updated_at_ms=data.get("updated_at_ms", 0),
            delete_after_run=data.get("delete_after_run", False),
        )


@dataclass
class CronStore:
    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "jobs": [job.to_dict() for job in self.jobs],
        }
