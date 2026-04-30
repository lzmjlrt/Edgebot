"""Heartbeat service - periodic wake-up based on HEARTBEAT.md."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from edgebot.config import HEARTBEAT_MD_PATH, MODEL, create_provider
from edgebot.providers.base import ToolCallRequest

_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Decide whether there is work to run from HEARTBEAT.md.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                    },
                    "tasks": {
                        "type": "string",
                        "description": "What Edgebot should do now if action=run.",
                    },
                },
                "required": ["action"],
            },
        },
    }
]

_HEARTBEAT_NOTIFY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat_notify",
            "description": "Decide whether the heartbeat execution result should be surfaced to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "notify"],
                    },
                    "reason": {
                        "type": "string",
                        "description": "Short explanation for the decision.",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


class HeartbeatService:
    def __init__(
        self,
        workspace: Path,
        *,
        on_execute: Callable[[str], Awaitable[str | None]] | None = None,
        on_notify: Callable[[str], Awaitable[None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
    ):
        self.workspace = workspace
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_action: str | None = None
        self._last_reason: str | None = None
        self._last_tick_at: str | None = None

    @property
    def heartbeat_file(self) -> Path:
        return HEARTBEAT_MD_PATH

    def set_handlers(
        self,
        *,
        on_execute: Callable[[str], Awaitable[str | None]] | None = None,
        on_notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        if on_execute is not None:
            self.on_execute = on_execute
        if on_notify is not None:
            self.on_notify = on_notify

    async def start(self) -> None:
        if not self.enabled or self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _read_heartbeat(self) -> str | None:
        if not self.heartbeat_file.exists():
            return None
        content = self.heartbeat_file.read_text(encoding="utf-8").strip()
        return content or None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                continue

    async def _decide(self, content: str) -> tuple[str, str]:
        provider = create_provider()
        response = await provider.chat_with_retry(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a heartbeat agent. Read HEARTBEAT.md and always call the "
                        "`heartbeat` tool with action=skip or action=run."
                    ),
                },
                {
                    "role": "user",
                    "content": content,
                },
            ],
            tools=_HEARTBEAT_TOOL,
            max_tokens=300,
            temperature=0.3,
        )
        if not response.tool_calls:
            return "skip", ""
        try:
            args = response.tool_calls[0].arguments
        except Exception:
            args = {}
        return args.get("action", "skip"), args.get("tasks", "")

    async def _should_notify(self, result: str, tasks: str) -> tuple[bool, str]:
        if not result.strip():
            return False, "empty result"
        try:
            provider = create_provider()
            response = await provider.chat_with_retry(
                model=MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a heartbeat post-run evaluator. Decide whether the user should "
                            "be notified about the result. Notify only when the result contains "
                            "actionable findings, completed work, or something worth surfacing."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Triggered task:\n{tasks}\n\n"
                            f"Execution result:\n{result}"
                        ),
                    },
                ],
                tools=_HEARTBEAT_NOTIFY_TOOL,
                max_tokens=200,
                temperature=0.3,
            )
            if response.tool_calls:
                args = response.tool_calls[0].arguments
                return args.get("action") == "notify", args.get("reason", "")
        except Exception:
            pass

        lowered = result.lower()
        noisy_markers = ["nothing to do", "no action", "no changes", "skipped"]
        if any(marker in lowered for marker in noisy_markers):
            return False, "fallback quiet result"
        return True, "fallback notify"

    async def _tick(self) -> str | None:
        content = self._read_heartbeat()
        if not content:
            self._last_tick_at = datetime.now().isoformat(timespec="seconds")
            self._last_action = "missing"
            self._last_reason = "HEARTBEAT.md missing or empty"
            return None
        self._last_tick_at = datetime.now().isoformat(timespec="seconds")
        action, tasks = await self._decide(content)
        if action != "run" or not tasks.strip() or self.on_execute is None:
            self._last_action = action
            self._last_reason = "no runnable task"
            return None
        result = await self.on_execute(tasks.strip())
        self._last_action = "run"
        should_notify, reason = await self._should_notify(result or "", tasks.strip())
        self._last_reason = reason or ("notify" if should_notify else "skip")
        if result and should_notify and self.on_notify is not None:
            await self.on_notify(result)
        return result

    async def trigger_now(self) -> str | None:
        return await self._tick()

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "interval_s": self.interval_s,
            "heartbeat_file": str(self.heartbeat_file),
            "present": self.heartbeat_file.exists(),
            "last_tick_at": self._last_tick_at,
            "last_action": self._last_action,
            "last_reason": self._last_reason,
        }
