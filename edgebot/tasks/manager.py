"""
edgebot/tasks/manager.py - File-backed persistent task board (TaskManager).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from edgebot.config import TASKS_DIR
from edgebot.tasks.hooks import run_task_hooks

_TASK_STATUSES = {"pending", "in_progress", "completed", "blocked"}


class TaskManager:
    def __init__(self):
        TASKS_DIR.mkdir(exist_ok=True)

    def _next_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in TASKS_DIR.glob("task_*.json")]
        return max(ids, default=0) + 1

    def _load(self, tid: int) -> dict:
        p = TASKS_DIR / f"task_{tid}.json"
        if not p.exists():
            raise ValueError(f"Task {tid} not found")
        task = json.loads(p.read_text(encoding="utf-8"))
        normalized = self._normalize_task(task)
        if normalized != task:
            self._save(normalized)
        return normalized

    def _save(self, task: dict):
        task = self._normalize_task(task)
        (TASKS_DIR / f"task_{task['id']}.json").write_text(
            json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _normalize_task(self, task: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(task)
        normalized["id"] = int(normalized["id"])
        normalized["subject"] = str(normalized.get("subject", "")).strip()
        normalized["description"] = str(normalized.get("description", "") or "")

        status = str(normalized.get("status", "pending") or "pending")
        normalized["status"] = status if status in _TASK_STATUSES else "pending"

        owner = normalized.get("owner")
        normalized["owner"] = str(owner).strip() if owner else None

        active_form = normalized.get("activeForm")
        normalized["activeForm"] = str(active_form).strip() if active_form else None

        metadata = normalized.get("metadata")
        normalized["metadata"] = metadata if isinstance(metadata, dict) else {}

        normalized["blockedBy"] = self._normalize_id_list(normalized.get("blockedBy"))
        normalized["blocks"] = self._normalize_id_list(normalized.get("blocks"))

        now = self._now_iso()
        normalized["createdAt"] = str(normalized.get("createdAt") or now)
        normalized["updatedAt"] = str(normalized.get("updatedAt") or normalized["createdAt"])
        return normalized

    @staticmethod
    def _normalize_id_list(raw: Any) -> list[int]:
        if raw is None:
            return []
        if isinstance(raw, (str, int)):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        values: list[int] = []
        for item in raw:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value not in values:
                values.append(value)
        return values

    @staticmethod
    def _merge_metadata(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(existing)
        for key, value in incoming.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        return merged

    def _unlink_task(self, tid: int) -> str:
        path = TASKS_DIR / f"task_{tid}.json"
        path.unlink(missing_ok=True)
        for f in TASKS_DIR.glob("task_*.json"):
            task = self._normalize_task(json.loads(f.read_text(encoding="utf-8")))
            changed = False
            if tid in task.get("blockedBy", []):
                task["blockedBy"] = [value for value in task["blockedBy"] if value != tid]
                changed = True
            if tid in task.get("blocks", []):
                task["blocks"] = [value for value in task["blocks"] if value != tid]
                changed = True
            if changed:
                task["updatedAt"] = self._now_iso()
                self._save(task)
        return f"Task {tid} deleted"

    @staticmethod
    def _status_marker(status: str) -> str:
        return {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "blocked": "[-]"}.get(status, "[?]")

    def format_task(self, task: dict[str, Any]) -> str:
        task = self._normalize_task(task)
        lines = [
            f"Task #{task['id']}: {task['subject']}",
            f"Status: {task['status']}",
        ]
        if task.get("owner"):
            lines.append(f"Owner: {task['owner']}")
        if task.get("activeForm"):
            lines.append(f"Active: {task['activeForm']}")
        if task.get("description"):
            lines.append(f"Description: {task['description']}")
        if task.get("blockedBy"):
            lines.append(f"Blocked by: {', '.join(f'#{value}' for value in task['blockedBy'])}")
        if task.get("blocks"):
            lines.append(f"Blocks: {', '.join(f'#{value}' for value in task['blocks'])}")
        if task.get("metadata"):
            lines.append("Metadata:")
            for key, value in sorted(task["metadata"].items()):
                lines.append(f"  {key}: {value}")
        lines.append(f"Created: {task['createdAt']}")
        lines.append(f"Updated: {task['updatedAt']}")
        return "\n".join(lines)

    def format_task_list(self, tasks: list[dict[str, Any]]) -> str:
        if not tasks:
            return "No tasks."
        lines = []
        for task in tasks:
            task = self._normalize_task(task)
            owner = f" @{task['owner']}" if task.get("owner") else ""
            blocked = (
                f" (blocked by: {', '.join(f'#{value}' for value in task['blockedBy'])})"
                if task.get("blockedBy")
                else ""
            )
            active = (
                f" <- {task['activeForm']}"
                if task.get("status") == "in_progress" and task.get("activeForm")
                else ""
            )
            lines.append(
                f"{self._status_marker(task['status'])} #{task['id']}: {task['subject']}{owner}{blocked}{active}"
            )
        return "\n".join(lines)

    def list_data(self) -> list[dict[str, Any]]:
        return [
            self._normalize_task(json.loads(f.read_text(encoding="utf-8")))
            for f in sorted(TASKS_DIR.glob("task_*.json"))
        ]

    def create(
        self,
        subject: str,
        description: str = "",
        *,
        active_form: str | None = None,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        cleaned_subject = str(subject).strip()
        if not cleaned_subject:
            raise ValueError("Task subject is required")
        now = self._now_iso()
        task = {
            "id": self._next_id(),
            "subject": cleaned_subject,
            "description": description,
            "status": "pending",
            "owner": owner.strip() if owner else None,
            "activeForm": active_form.strip() if active_form else None,
            "blockedBy": [],
            "blocks": [],
            "metadata": metadata or {},
            "createdAt": now,
            "updatedAt": now,
        }
        self._save(task)
        errors = run_task_hooks("task_created", task)
        if errors:
            self._unlink_task(task["id"])
            raise ValueError("\n".join(errors))
        return self.format_task(task)

    def get(self, tid: int) -> str:
        return self.format_task(self._load(tid))

    def update(
        self,
        tid: int,
        status: str | None = None,
        add_blocked_by: list[int] | None = None,
        add_blocks: list[int] | None = None,
        *,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        task = self._load(tid)
        updated_fields: list[str] = []

        if subject is not None and subject.strip() and subject != task["subject"]:
            task["subject"] = subject.strip()
            updated_fields.append("subject")
        if description is not None and description != task["description"]:
            task["description"] = description
            updated_fields.append("description")
        if active_form is not None:
            new_active_form = active_form.strip() or None
            if new_active_form != task.get("activeForm"):
                task["activeForm"] = new_active_form
                updated_fields.append("activeForm")
        if owner is not None:
            new_owner = owner.strip() or None
            if new_owner != task.get("owner"):
                task["owner"] = new_owner
                updated_fields.append("owner")
        if metadata is not None:
            merged = self._merge_metadata(task.get("metadata", {}), metadata)
            if merged != task.get("metadata", {}):
                task["metadata"] = merged
                updated_fields.append("metadata")

        if status:
            if status == "deleted":
                return self._unlink_task(tid)
            if status not in _TASK_STATUSES:
                raise ValueError(f"Invalid status: {status}")
            if status == "completed" and task["status"] != "completed":
                preview = dict(task)
                preview["status"] = "completed"
                preview["updatedAt"] = self._now_iso()
                errors = run_task_hooks("task_completed", preview)
                if errors:
                    raise ValueError("\n".join(errors))
            task["status"] = status
            updated_fields.append("status")
            if status == "completed":
                for f in TASKS_DIR.glob("task_*.json"):
                    t = self._normalize_task(json.loads(f.read_text(encoding="utf-8")))
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        t["updatedAt"] = self._now_iso()
                        self._save(t)
        if add_blocked_by:
            merged_blocked_by = self._normalize_id_list(task["blockedBy"] + add_blocked_by)
            if merged_blocked_by != task["blockedBy"]:
                task["blockedBy"] = merged_blocked_by
                updated_fields.append("blockedBy")
        if add_blocks:
            merged_blocks = self._normalize_id_list(task["blocks"] + add_blocks)
            if merged_blocks != task["blocks"]:
                task["blocks"] = merged_blocks
                updated_fields.append("blocks")

        if not updated_fields:
            return self.format_task(task)

        task["updatedAt"] = self._now_iso()
        self._save(task)
        prefix = f"Updated task #{task['id']}: {', '.join(updated_fields)}"
        return f"{prefix}\n{self.format_task(task)}"

    def list_all(self) -> str:
        return self.format_task_list(self.list_data())

    def claim(self, tid: int, owner: str) -> str:
        task = self._load(tid)
        task["owner"] = owner
        task["status"] = "in_progress"
        task["updatedAt"] = self._now_iso()
        self._save(task)
        return f"Claimed task #{tid} for {owner}"
