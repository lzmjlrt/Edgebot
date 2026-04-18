"""
edgebot/subagent/runner.py - One-shot subagent runner.

Unlike teammates, subagents are fire-and-forget async tasks:
 - not persisted to disk
 - not part of the message bus
 - do not claim tasks from the board
 - run to completion then hold their final text in-memory for polling
"""

import asyncio
import json
import time
import uuid

import litellm

from edgebot.config import API_BASE, API_KEY, MODEL
from edgebot.subagent.capabilities import CAPABILITIES


class SubagentRunner:
    """Manages one-shot subagents spawned via asyncio.create_task."""

    def __init__(self):
        self._tasks: dict[str, dict] = {}

    def spawn(self, capability: str, prompt: str, name: str = "") -> dict:
        if capability not in CAPABILITIES:
            return {
                "error": f"unknown capability '{capability}'",
                "options": list(CAPABILITIES),
            }
        task_id = name.strip() if name else f"sa_{uuid.uuid4().hex[:8]}"
        if task_id in self._tasks and self._tasks[task_id]["status"] == "running":
            return {"error": f"task_id '{task_id}' already running"}
        rec = {
            "task_id": task_id,
            "capability": capability,
            "prompt": prompt,
            "status": "running",
            "result": None,
            "started": time.time(),
        }
        self._tasks[task_id] = rec
        try:
            asyncio.create_task(self._run(task_id))
        except RuntimeError as e:
            rec["status"] = "failed"
            rec["result"] = f"[error] no running event loop: {e}"
        return {"task_id": task_id, "status": rec["status"]}

    def status(self, task_id: str) -> dict:
        rec = self._tasks.get(task_id)
        if not rec:
            return {"error": f"unknown task_id '{task_id}'"}
        return {
            "task_id": rec["task_id"],
            "capability": rec["capability"],
            "status": rec["status"],
            "result": rec["result"],
            "elapsed_sec": round(time.time() - rec["started"], 1),
        }

    def list_all(self) -> list[dict]:
        return [
            {"task_id": k, "status": v["status"], "capability": v["capability"],
             "elapsed_sec": round(time.time() - v["started"], 1)}
            for k, v in self._tasks.items()
        ]

    async def _run(self, task_id: str):
        rec = self._tasks[task_id]
        cap = CAPABILITIES[rec["capability"]]
        messages = [{"role": "user", "content": rec["prompt"]}]
        try:
            deadline = time.time() + 300  # 5 min wall-clock
            for _ in range(20):           # max 20 tool-roundtrips
                if time.time() > deadline:
                    rec["result"] = "[subagent timeout — 5min]"
                    break
                resp = await asyncio.wait_for(
                    litellm.acompletion(
                        model=MODEL,
                        messages=[{"role": "system", "content": cap["system"]}] + messages,
                        tools=cap["tools"],
                        max_tokens=8000,
                        api_key=API_KEY,
                        api_base=API_BASE,
                    ),
                    timeout=120,
                )
                choice = resp.choices[0]
                asst = {"role": "assistant", "content": choice.message.content}
                if choice.message.tool_calls:
                    asst["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name,
                                      "arguments": tc.function.arguments}}
                        for tc in choice.message.tool_calls
                    ]
                messages.append(asst)
                if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
                    rec["result"] = choice.message.content or ""
                    break
                for tc in choice.message.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    handler = cap["handlers"].get(tc.function.name)
                    try:
                        out = (handler(**args) if handler
                               else f"Unknown tool '{tc.function.name}' for capability "
                                    f"'{rec['capability']}'")
                    except Exception as e:
                        out = f"Error: {e}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(out),
                    })
            rec["status"] = "completed"
        except Exception as e:
            rec["status"] = "failed"
            rec["result"] = f"[error] {e}"
