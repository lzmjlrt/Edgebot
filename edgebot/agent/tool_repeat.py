"""
edgebot/agent/tool_repeat.py - Repeated-tool-call guard for the agent loop.

Tracks consecutive identical tool-call batches and provides the repair /
finalization prompts the runner injects when the model loops on the same
call without new information.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from edgebot.providers.base import ToolCallRequest

_REPEATED_TOOL_CALL_REPAIR_THRESHOLD = 3
_REPEATED_TOOL_CALL_STOP_THRESHOLD = 5
_REPEATED_TOOL_CALL_REPAIR_PROMPT = (
    "You have called the same tool with the same arguments repeatedly and "
    "received no new information. Do not call it again. Choose one of: read "
    "a different range, edit a source file, run a targeted test, or submit "
    "the final patch."
)
_REPEATED_READ_FILE_REPAIR_PROMPT = (
    "The same read_file range has already been read and is unchanged. Do not "
    "call read_file with the same path, offset, and limit again. Use a "
    "different range, edit a source file, run a targeted test, or provide "
    "the final answer."
)
_REPEATED_TOOL_CALL_FINALIZATION_PROMPT = (
    "The agent loop stopped because the same tool call was repeated without "
    "new information. Based only on the conversation and tool results above, "
    "provide a concise final response. Do not call tools. Do not claim the "
    "task is complete unless a source edit and verification are visible above."
)
_REPEATED_TOOL_CALL_FALLBACK_FINAL = (
    "The agent loop stopped because the same tool call was repeated without "
    "new information."
)


@dataclass(slots=True)
class ToolRepeatState:
    last_signature: str | None = None
    consecutive_count: int = 0
    max_consecutive_count: int = 0
    repair_injections: int = 0

    def observe(self, signature: str) -> int:
        if signature == self.last_signature:
            self.consecutive_count += 1
        else:
            self.last_signature = signature
            self.consecutive_count = 1
        self.max_consecutive_count = max(
            self.max_consecutive_count,
            self.consecutive_count,
        )
        return self.consecutive_count

    def reset(self) -> None:
        self.last_signature = None
        self.consecutive_count = 0


def _normalize_tool_args(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_tool_args(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, list):
        return [_normalize_tool_args(item) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value


def _tool_call_signature(name: str, arguments: Any) -> str:
    normalized = _normalize_tool_args(arguments)
    try:
        payload = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        payload = repr(normalized)
    return f"{name}:{payload}"


def _tool_call_batch_signature(tool_calls: list[ToolCallRequest]) -> str:
    signatures = [
        _tool_call_signature(tool_call.name, tool_call.arguments)
        for tool_call in tool_calls
    ]
    return json.dumps(signatures, separators=(",", ":"), ensure_ascii=False)


def _repeated_tool_call_repair_prompt(tool_calls: list[ToolCallRequest]) -> str:
    if len(tool_calls) == 1 and tool_calls[0].name == "read_file":
        return _REPEATED_READ_FILE_REPAIR_PROMPT
    return _REPEATED_TOOL_CALL_REPAIR_PROMPT


def _record_tool_call_telemetry(
    telemetry: dict[str, Any],
    tool_calls: list[ToolCallRequest],
) -> None:
    telemetry["tool_call_count"] += len(tool_calls)
    for tool_call in tool_calls:
        key = f"{tool_call.name}_count"
        if key in telemetry:
            telemetry[key] += 1
