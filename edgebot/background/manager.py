"""
edgebot/background/manager.py - Background thread task execution.
"""

import subprocess
import threading
import uuid
from queue import Queue

from edgebot.config import WORKDIR


class BackgroundManager:
    def __init__(self):
        self.tasks = {}
        self.notifications = Queue()

    def run(self, command: str, timeout: int = 120) -> str:
        tid = str(uuid.uuid4())[:8]
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        threading.Thread(
            target=self._exec, args=(tid, command, timeout), daemon=True
        ).start()
        return f"Background task {tid} started: {command[:80]}"

    def _exec(self, tid: str, command: str, timeout: int):
        try:
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=timeout,
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            self.tasks[tid].update({"status": "completed", "result": output or "(no output)"})
        except Exception as e:
            self.tasks[tid].update({"status": "error", "result": str(e)})
        self.notifications.put({
            "task_id": tid,
            "status": self.tasks[tid]["status"],
            "result": self.tasks[tid]["result"][:500],
        })

    def check(self, tid: str = None) -> str:
        if tid:
            t = self.tasks.get(tid)
            return (
                f"[{t['status']}] {t.get('result', '(running)')}"
                if t else f"Unknown: {tid}"
            )
        return (
            "\n".join(
                f"{k}: [{v['status']}] {v['command'][:60]}"
                for k, v in self.tasks.items()
            )
            or "No bg tasks."
        )

    def drain(self) -> list:
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs
