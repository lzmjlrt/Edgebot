"""
edgebot/subagent/runner.py - Isolated subagent task runner.

Each subagent has:
 - isolated conversation state
 - an explicit allowed tool pool (capability-defined)
 - a local_agent task entry with transcript + output files

Execution delegates to AgentRunner so the LLM-call / tool-execution loop is
shared with the main agent (and inherits PERMISSIONS.authorize gating via
tools.orchestration.execute_tool_batches).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from edgebot.config import MODEL, SUBAGENT_DIR, create_provider
from edgebot.subagent.capabilities import CAPABILITIES

_MAX_TURNS = 20
_WALLCLOCK_SECONDS = 300
_RESULT_PREVIEW_CHARS = 4000
_TERMINAL_STATUSES = {"completed", "failed", "stopped"}


class SubagentRunner:
    """Manage isolated subagent runs as explicit local_agent tasks."""

    def __init__(self, root_dir: Path | None = None):
        self.root_dir = Path(root_dir or SUBAGENT_DIR)
        self.transcript_dir = self.root_dir / "transcripts"
        self.output_dir = self.root_dir / "outputs"
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, dict[str, Any]] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def _tool_schema(self, tool_name: str) -> dict[str, Any] | None:
        from edgebot.tools.registry import get_tool_instance

        tool = get_tool_instance(tool_name)
        if tool is None:
            return None
        return tool.to_openai()

    @staticmethod
    def _preview_text(text: str, limit: int = _RESULT_PREVIEW_CHARS) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"

    def _task_view(
        self,
        rec: dict[str, Any],
        *,
        include_output: bool = False,
        preview_chars: int = _RESULT_PREVIEW_CHARS,
    ) -> dict[str, Any]:
        output_text = self._read_text(Path(rec["output_file"]))
        transcript_text = self._read_text(Path(rec["transcript_file"]))
        data = {
            "task_id": rec["task_id"],
            "task_type": "local_agent",
            "status": rec["status"],
            "capability": rec["capability"],
            "description": rec["description"],
            "prompt": rec["prompt"],
            "result": rec.get("result"),
            "partial_result": rec.get("partial_result"),
            "error": rec.get("error"),
            "started_at": rec["started_at"],
            "finished_at": rec.get("finished_at"),
            "tool_uses": rec.get("tool_uses", 0),
            "output_file": rec["output_file"],
            "transcript_file": rec["transcript_file"],
            "allowed_tools": list(rec["allowed_tools"]),
            "is_backgrounded": rec.get("is_backgrounded", True),
            "stop_requested": rec.get("stop_requested", False),
            "stop_reason": rec.get("stop_reason"),
        }
        if include_output:
            data["output"] = output_text
            data["transcript"] = transcript_text
        else:
            data["output_preview"] = self._preview_text(output_text, preview_chars)
            data["transcript_preview"] = self._preview_text(transcript_text, preview_chars)
        return data

    @staticmethod
    def _read_text(path: Path) -> str:
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"(failed to read {path}: {exc})"

    @staticmethod
    def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _enqueue_notification(self, rec: dict[str, Any]) -> None:
        preview = (rec.get("result") or rec.get("partial_result") or rec.get("error") or "")[:500]
        asyncio.create_task(
            self._notifications.put(
                {
                    "task_id": rec["task_id"],
                    "status": rec["status"],
                    "output_file": rec["output_file"],
                    "result": preview,
                }
            )
        )

    def _finalize_record(self, rec: dict[str, Any]) -> None:
        if rec["event"].is_set():
            return
        rec["finished_at"] = time.time()
        rec["runner_task"] = None
        rec["event"].set()
        self._enqueue_notification(rec)

    def _on_runner_done(self, task_id: str, runner_task: asyncio.Task[Any]) -> None:
        rec = self._tasks.get(task_id)
        if rec is None or rec["event"].is_set():
            return

        if runner_task.cancelled():
            rec["status"] = "stopped"
            rec["error"] = rec.get("stop_reason") or "stopped"
            rec["result"] = rec["partial_result"] or ""
            output_path = Path(rec["output_file"])
            if not self._read_text(output_path):
                output_path.write_text(rec["result"] or rec["error"], encoding="utf-8")
        else:
            exc = runner_task.exception()
            if exc is not None:
                rec["status"] = "failed"
                rec["error"] = str(exc)
                rec["result"] = rec["partial_result"] or ""
        self._finalize_record(rec)

    def spawn(
        self,
        capability: str,
        prompt: str,
        *,
        name: str = "",
        description: str = "",
        backgrounded: bool = True,
    ) -> dict[str, Any]:
        if capability not in CAPABILITIES:
            return {
                "error": f"unknown capability '{capability}'",
                "options": list(CAPABILITIES),
            }

        task_id = name.strip() if name else f"sa_{uuid.uuid4().hex[:8]}"
        existing = self._tasks.get(task_id)
        if existing and existing["status"] not in _TERMINAL_STATUSES:
            return {"error": f"task_id '{task_id}' already running"}

        transcript_file = self.transcript_dir / f"{task_id}.jsonl"
        output_file = self.output_dir / f"{task_id}.txt"
        capability_spec = CAPABILITIES[capability]
        rec = {
            "task_id": task_id,
            "type": "local_agent",
            "capability": capability,
            "description": description.strip() or prompt[:80],
            "prompt": prompt,
            "status": "running",
            "result": None,
            "partial_result": "",
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
            "tool_uses": 0,
            "allowed_tools": tuple(capability_spec["allowed_tools"]),
            "transcript_file": str(transcript_file),
            "output_file": str(output_file),
            "is_backgrounded": backgrounded,
            "stop_requested": False,
            "stop_reason": None,
            "event": asyncio.Event(),
            "runner_task": None,
        }
        self._tasks[task_id] = rec
        transcript_file.write_text("", encoding="utf-8")
        output_file.write_text("", encoding="utf-8")
        rec["runner_task"] = asyncio.create_task(self._run(task_id))
        rec["runner_task"].add_done_callback(lambda task: self._on_runner_done(task_id, task))
        return self._task_view(rec, include_output=False)

    def status(self, task_id: str) -> dict[str, Any]:
        rec = self._tasks.get(task_id)
        if not rec:
            return {"error": f"unknown task_id '{task_id}'"}
        return self._task_view(rec, include_output=False)

    def detail(self, task_id: str) -> dict[str, Any]:
        rec = self._tasks.get(task_id)
        if not rec:
            return {"error": f"unknown task_id '{task_id}'"}
        return self._task_view(rec, include_output=True)

    def list_all(self) -> list[dict[str, Any]]:
        return [
            self._task_view(rec, include_output=False)
            for _, rec in sorted(
                self._tasks.items(),
                key=lambda item: item[1]["started_at"],
                reverse=True,
            )
        ]

    async def drain(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while not self._notifications.empty():
            items.append(await self._notifications.get())
        return items

    def set_backgrounded(self, task_id: str, backgrounded: bool) -> dict[str, Any]:
        rec = self._tasks.get(task_id)
        if rec is None:
            return {"error": f"unknown task_id '{task_id}'"}
        rec["is_backgrounded"] = backgrounded
        return self._task_view(rec, include_output=False)

    def stop(self, task_id: str, *, reason: str = "stopped by user") -> dict[str, Any]:
        rec = self._tasks.get(task_id)
        if rec is None:
            return {"error": f"unknown task_id '{task_id}'"}
        if rec["status"] in _TERMINAL_STATUSES:
            return self._task_view(rec, include_output=False)

        rec["stop_requested"] = True
        rec["stop_reason"] = reason
        rec["status"] = "stopping"
        runner_task = rec.get("runner_task")
        if runner_task is not None and not runner_task.done():
            runner_task.cancel()
        return self._task_view(rec, include_output=False)

    async def wait(
        self,
        task_id: str,
        *,
        timeout_ms: int | None = None,
        foreground: bool = False,
        include_output: bool = True,
    ) -> dict[str, Any]:
        rec = self._tasks.get(task_id)
        if rec is None:
            return {"retrieval_status": "not_found", "task": None}

        if foreground:
            rec["is_backgrounded"] = False

        if rec["status"] not in _TERMINAL_STATUSES:
            try:
                if timeout_ms is None:
                    await rec["event"].wait()
                else:
                    await asyncio.wait_for(rec["event"].wait(), timeout=max(timeout_ms, 0) / 1000)
            except asyncio.TimeoutError:
                return {
                    "retrieval_status": "timeout",
                    "task": self._task_view(rec, include_output=include_output),
                }

        return {
            "retrieval_status": "success",
            "task": self._task_view(rec, include_output=include_output),
        }

    async def task_output(
        self,
        task_id: str,
        *,
        block: bool = True,
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        rec = self._tasks.get(task_id)
        if rec is None:
            return {"retrieval_status": "not_found", "task": None}

        if block:
            return await self.wait(task_id, timeout_ms=timeout_ms, include_output=True)

        if rec["status"] not in _TERMINAL_STATUSES:
            return {
                "retrieval_status": "not_ready",
                "task": self._task_view(rec, include_output=True),
            }
        return {
            "retrieval_status": "success",
            "task": self._task_view(rec, include_output=True),
        }

    async def run_and_wait(
        self,
        capability: str,
        prompt: str,
        *,
        name: str = "",
        description: str = "",
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        task = self.spawn(
            capability,
            prompt,
            name=name,
            description=description,
            backgrounded=False,
        )
        if "task_id" not in task:
            return task
        return await self.wait(
            task["task_id"],
            timeout_ms=timeout_ms,
            foreground=True,
            include_output=True,
        )

    async def _run(self, task_id: str) -> None:
        """Delegate to AgentRunner; persist transcript + output via checkpoints."""
        from edgebot.agent.runner import AgentRunner, AgentRunSpec
        from edgebot.tools.registry import TOOL_HANDLERS

        rec = self._tasks[task_id]
        cap = CAPABILITIES[rec["capability"]]
        transcript_path = Path(rec["transcript_file"])
        output_path = Path(rec["output_file"])

        initial_messages: list[dict[str, Any]] = [
            {"role": "system", "content": cap["system"]},
            {"role": "user", "content": rec["prompt"]},
        ]
        # Persist the initial user prompt to the transcript.
        self._append_jsonl(transcript_path, initial_messages[1])

        tool_schemas = [
            schema for schema in (self._tool_schema(n) for n in cap["allowed_tools"]) if schema
        ]

        # AgentRunner uses execute_tool_batches which resolves tools via the
        # registry directly; passing TOOL_HANDLERS here is for API parity.
        tool_handlers = dict(TOOL_HANDLERS)

        provider = create_provider()
        runner = AgentRunner(provider)

        transcript_seen = 0  # how many messages of `result.messages` we've persisted

        async def _checkpoint(payload: dict[str, Any]) -> None:
            nonlocal transcript_seen
            # Persist assistant + tool messages emitted during this iteration.
            assistant_msg = payload.get("assistant_message")
            if assistant_msg and isinstance(assistant_msg, dict):
                self._append_jsonl(transcript_path, assistant_msg)
                content = (assistant_msg.get("content") or "").strip() if isinstance(assistant_msg.get("content"), str) else ""
                if content:
                    rec["partial_result"] = content[:_RESULT_PREVIEW_CHARS]
                    output_path.write_text(content, encoding="utf-8")
            for tool_msg in payload.get("completed_tool_results") or []:
                self._append_jsonl(transcript_path, tool_msg)
                rec["tool_uses"] += 1
            transcript_seen += 1

        async def _injection() -> list[dict[str, Any]]:
            # Honor explicit stop requests from the parent.
            if rec["stop_requested"]:
                raise asyncio.CancelledError()
            return []

        spec = AgentRunSpec(
            initial_messages=initial_messages,
            provider=provider,
            tools=tool_schemas,
            tool_handlers=tool_handlers,
            model=MODEL,
            max_iterations=_MAX_TURNS,
            max_tokens=8000,
            retry_mode="standard",
            emit_output=False,
            assistant_label=f"subagent[{rec['capability']}]",
            on_stream=None,
            on_stream_end=None,
            on_retry_wait=None,
            checkpoint_callback=_checkpoint,
            injection_callback=_injection,
        )

        try:
            result = await asyncio.wait_for(runner.run(spec), timeout=_WALLCLOCK_SECONDS)
        except asyncio.CancelledError:
            rec["status"] = "stopped"
            rec["error"] = rec.get("stop_reason") or "stopped"
            rec["result"] = rec["partial_result"] or ""
            if not output_path.read_text(encoding="utf-8"):
                output_path.write_text(rec["result"] or rec["error"], encoding="utf-8")
            return
        except asyncio.TimeoutError:
            rec["status"] = "failed"
            rec["error"] = f"subagent timeout after {_WALLCLOCK_SECONDS} seconds"
            rec["result"] = rec["partial_result"] or ""
            if not output_path.read_text(encoding="utf-8"):
                output_path.write_text(rec["error"], encoding="utf-8")
            return
        except Exception as exc:
            rec["status"] = "failed"
            rec["error"] = str(exc)
            rec["result"] = rec["partial_result"] or ""
            if not output_path.read_text(encoding="utf-8"):
                output_path.write_text(rec["error"], encoding="utf-8")
            return

        final = result.final_content or rec["partial_result"] or ""
        if result.stop_reason == "completed":
            rec["status"] = "completed"
            rec["result"] = final
        elif result.stop_reason == "max_iterations":
            rec["status"] = "completed"
            rec["result"] = final
            rec["error"] = "max_iterations"
        else:
            rec["status"] = "failed"
            rec["error"] = result.stop_reason
            rec["result"] = final

        if final:
            output_path.write_text(final, encoding="utf-8")
