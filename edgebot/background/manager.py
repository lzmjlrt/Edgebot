"""
edgebot/background/manager.py - Background task execution with persisted output.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from queue import Queue
from typing import Any

from edgebot.config import BACKGROUND_DIR
from edgebot.security.workspace_policy import workspace_root

_OUTPUT_PREVIEW_CHARS = 4000
_NOTIFICATION_PREVIEW_CHARS = 500
_ADOPT_DRAIN_INTERVAL = 0.2
_ADOPT_DEFAULT_TIMEOUT = 600


class BackgroundManager:
    def __init__(self, output_dir: Path | None = None):
        self.output_dir = Path(output_dir or BACKGROUND_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tasks: dict[str, dict[str, Any]] = {}
        self.notifications = Queue()
        self._lock = threading.Lock()

    def run(self, command: str, timeout: int = 120) -> dict[str, Any]:
        from edgebot.tools.shell import _guard_command

        guard_error = _guard_command(command, str(workspace_root()))
        if guard_error:
            return {
                "task_type": "local_bash",
                "status": "error",
                "description": command[:120],
                "command": command,
                "started_at": time.time(),
                "finished_at": time.time(),
                "timeout_seconds": timeout,
                "exit_code": None,
                "error": guard_error,
                "output_file": None,
                "output_preview": guard_error,
            }
        tid = str(uuid.uuid4())[:8]
        output_path = self.output_dir / f"{tid}.log"
        task = {
            "task_id": tid,
            "type": "local_bash",
            "status": "running",
            "command": command,
            "description": command[:120],
            "started_at": time.time(),
            "finished_at": None,
            "timeout_seconds": timeout,
            "exit_code": None,
            "error": None,
            "output_file": str(output_path),
            "output_preview": "",
            "event": threading.Event(),
        }
        with self._lock:
            self.tasks[tid] = task
        threading.Thread(
            target=self._exec,
            args=(tid, command, timeout, output_path),
            daemon=True,
        ).start()
        return self._public_task(task, include_output=False)

    def adopt(
        self,
        process: subprocess.Popen,
        command: str,
        captured: str,
        *,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Take ownership of an already-running Popen and surface it as a bg task.

        Used when the foreground bash tool hits a hard timeout but the process
        still has work to do. The captured-so-far text is kept on the task
        record as the initial preview; the watcher thread waits for the process
        to exit and then writes the full output to the log file via a single
        final communicate() call.
        """
        tid = str(uuid.uuid4())[:8]
        output_path = self.output_dir / f"{tid}.log"
        task = {
            "task_id": tid,
            "type": "adopted_bash",
            "status": "running",
            "command": command,
            "description": command[:120],
            "started_at": time.time(),
            "finished_at": None,
            "timeout_seconds": timeout if timeout is not None else _ADOPT_DEFAULT_TIMEOUT,
            "exit_code": None,
            "error": None,
            "output_file": str(output_path),
            "output_preview": (captured or "")[:_OUTPUT_PREVIEW_CHARS],
            "event": threading.Event(),
        }
        with self._lock:
            self.tasks[tid] = task
        threading.Thread(
            target=self._watch_adopted,
            args=(tid, process, output_path, task["timeout_seconds"]),
            daemon=True,
        ).start()
        return self._public_task(task, include_output=False)

    def _watch_adopted(
        self,
        tid: str,
        process: subprocess.Popen,
        output_path: Path,
        timeout: int,
    ) -> None:
        """Wait for an adopted process to exit, then write its full output.

        The foreground caller already consumed partial output via
        `communicate(timeout=)`. We avoid touching stdout/stderr until the
        process has actually exited — at that point a final communicate() will
        join the still-running reader threads and return the complete buffered
        output (which includes the bytes already shown to the agent). We write
        that complete output to the log so `task_output` returns the full,
        canonical result.
        """
        deadline = time.monotonic() + timeout if timeout > 0 else None
        status = "completed"
        error: str | None = None
        final_out = ""
        final_err = ""
        try:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    process.kill()
                    status = "timeout"
                    error = f"Adopted task exceeded {timeout}s; killed."
                    break
                if process.poll() is not None:
                    break
                time.sleep(_ADOPT_DRAIN_INTERVAL)
            try:
                final_out, final_err = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                final_out, final_err = process.communicate()
            if isinstance(final_out, bytes):
                final_out = final_out.decode("utf-8", errors="replace")
            if isinstance(final_err, bytes):
                final_err = final_err.decode("utf-8", errors="replace")
        except Exception as exc:
            status = "error"
            error = str(exc)

        parts: list[str] = []
        if final_out:
            parts.append(final_out)
        if final_err and final_err.strip():
            parts.append(f"STDERR:\n{final_err}")
        if process.returncode is not None:
            parts.append(f"\nExit code: {process.returncode}")
        payload = "\n".join(parts) if parts else "(no output)"

        try:
            output_path.write_text(payload, encoding="utf-8")
        except OSError:
            pass
        preview = payload[:_OUTPUT_PREVIEW_CHARS]
        with self._lock:
            task = self.tasks.get(tid)
            if task is None:
                return
            task.update({
                "status": status,
                "finished_at": time.time(),
                "exit_code": process.returncode,
                "error": error,
                "output_preview": preview,
            })
            task["event"].set()

        self.notifications.put({
            "task_id": tid,
            "status": status,
            "output_file": str(output_path),
            "result": preview[:_NOTIFICATION_PREVIEW_CHARS],
        })

    def _exec(self, tid: str, command: str, timeout: int, output_path: Path) -> None:
        status = "completed"
        exit_code: int | None = None
        error: str | None = None
        output = ""
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=workspace_root(),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            exit_code = result.returncode
            output = ((result.stdout or "") + (result.stderr or "")).strip()
        except subprocess.TimeoutExpired as exc:
            status = "timeout"
            output = ((exc.stdout or "") + (exc.stderr or "")).strip()
            error = f"Command timed out after {timeout} seconds"
        except Exception as exc:
            status = "error"
            error = str(exc)
            output = error

        payload = output or "(no output)"
        output_path.write_text(payload, encoding="utf-8")
        preview = payload[:_OUTPUT_PREVIEW_CHARS]
        with self._lock:
            task = self.tasks[tid]
            task.update(
                {
                    "status": status,
                    "finished_at": time.time(),
                    "exit_code": exit_code,
                    "error": error,
                    "output_preview": preview,
                }
            )
            task["event"].set()

        self.notifications.put(
            {
                "task_id": tid,
                "status": status,
                "output_file": str(output_path),
                "result": preview[:_NOTIFICATION_PREVIEW_CHARS],
            }
        )

    def _public_task(self, task: dict[str, Any], *, include_output: bool) -> dict[str, Any]:
        data = {
            "task_id": task["task_id"],
            "task_type": task.get("type", "local_bash"),
            "status": task["status"],
            "description": task.get("description") or task["command"][:120],
            "command": task["command"],
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "timeout_seconds": task.get("timeout_seconds"),
            "exit_code": task.get("exit_code"),
            "error": task.get("error"),
            "output_file": task.get("output_file"),
        }
        if include_output:
            data["output"] = self._read_output(task)
        else:
            data["output_preview"] = task.get("output_preview", "")
        return data

    def _read_output(self, task: dict[str, Any]) -> str:
        output_file = task.get("output_file")
        if not output_file:
            return ""
        path = Path(output_file)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"(failed to read output: {exc})"

    def get_task(self, tid: str) -> dict[str, Any] | None:
        with self._lock:
            task = self.tasks.get(tid)
            if task is None:
                return None
            return self._public_task(task, include_output=False)

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            tasks = [self._public_task(task, include_output=False) for task in self.tasks.values()]
        return sorted(tasks, key=lambda item: item.get("started_at") or 0, reverse=True)

    def check(self, tid: str | None = None) -> str:
        if tid:
            task = self.get_task(tid)
            if task is None:
                return f"Unknown background task: {tid}"
            return json.dumps(task, indent=2, ensure_ascii=False)
        tasks = self.list_tasks()
        if not tasks:
            return "No bg tasks."
        return json.dumps(tasks, indent=2, ensure_ascii=False)

    def task_output(self, tid: str, block: bool = True, timeout_ms: int = 30000) -> dict[str, Any]:
        with self._lock:
            task = self.tasks.get(tid)
            if task is None:
                return {
                    "retrieval_status": "not_found",
                    "task": None,
                }
            event = task["event"]

        if block and task["status"] == "running":
            completed = event.wait(timeout=max(timeout_ms, 0) / 1000.0)
            if not completed:
                with self._lock:
                    refreshed = self.tasks.get(tid)
                    if refreshed is None:
                        return {"retrieval_status": "not_found", "task": None}
                    return {
                        "retrieval_status": "timeout",
                        "task": self._public_task(refreshed, include_output=True),
                    }

        with self._lock:
            refreshed = self.tasks.get(tid)
            if refreshed is None:
                return {"retrieval_status": "not_found", "task": None}
            if refreshed["status"] == "running" and not block:
                return {
                    "retrieval_status": "not_ready",
                    "task": self._public_task(refreshed, include_output=True),
                }
            return {
                "retrieval_status": "success",
                "task": self._public_task(refreshed, include_output=True),
            }

    def drain(self) -> list:
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs
