"""
edgebot/tools/orchestration.py - Tool execution orchestration.

Groups tool calls into conservative batches:
 - concurrency-safe tools can run in parallel
 - all other tools run serially

Results are always returned in the original tool-call order so the caller can
append tool messages and checkpoints deterministically.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from edgebot.tools.registry import PERMISSIONS, execute_registered_tool, get_tool_instance


def _parse_tool_args(raw_arguments: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_concurrency_safe(tool_name: str, args: dict[str, Any]) -> bool:
    tool = get_tool_instance(tool_name)
    if tool is None:
        return False

    try:
        cast_args = tool.cast_params(args)
        errors = tool.validate_params(cast_args)
    except Exception:
        return False
    if errors:
        return False

    safe = getattr(tool, "concurrency_safe", False)
    if callable(safe):
        try:
            return bool(safe(cast_args))
        except Exception:
            return False
    return bool(safe)


def _get_max_tool_concurrency() -> int:
    raw = os.getenv("EDGEBOT_MAX_TOOL_CONCURRENCY", "").strip()
    try:
        value = int(raw) if raw else 4
    except ValueError:
        return 4
    return max(1, value)


def partition_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Partition calls into batches of parallel-safe or serial work."""
    batches: list[dict[str, Any]] = []
    for tc in tool_calls:
        fn = tc.get("function", {}) or {}
        name = fn.get("name", "")
        args = _parse_tool_args(fn.get("arguments"))
        tool = get_tool_instance(name)
        cast_args = tool.cast_params(args) if tool is not None else args
        call = {
            "tool_call": tc,
            "name": name,
            "args": cast_args,
            "is_concurrency_safe": _is_concurrency_safe(name, cast_args),
        }
        if call["is_concurrency_safe"] and batches and batches[-1]["parallel"]:
            batches[-1]["calls"].append(call)
        else:
            batches.append({
                "parallel": call["is_concurrency_safe"],
                "calls": [call],
            })
    return batches


async def _run_single_tool(name: str, args: dict[str, Any]) -> Any:
    tool = get_tool_instance(name)
    if tool is not None:
        decision = await PERMISSIONS.authorize(name, args, tool)
        if decision.behavior != "allow":
            return f"Error: {decision.message}"
        return await execute_registered_tool(name, decision.updated_params or args)
    return f"Unknown tool: {name}"


async def _run_parallel_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(_get_max_tool_concurrency())

    async def _guarded(call: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                output = await _run_single_tool(call["name"], call["args"])
            except Exception as exc:
                output = f"Error: {exc}"
            return {
                "tool_call": call["tool_call"],
                "name": call["name"],
                "args": call["args"],
                "output": output,
            }

    return await asyncio.gather(*[_guarded(call) for call in calls])


async def execute_tool_batches(
    tool_calls: list[dict[str, Any]],
    *,
    tool_handlers: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Execute tool calls with conservative orchestration.

    Returns a list of execution records in the same order as the input:
    [{tool_call, name, args, output}, ...]
    """
    results: list[dict[str, Any]] = []
    for batch in partition_tool_calls(tool_calls):
        calls = batch["calls"]
        if batch["parallel"]:
            results.extend(await _run_parallel_calls(calls))
            continue

        for call in calls:
            try:
                tool = get_tool_instance(call["name"])
                if tool is not None:
                    output = await _run_single_tool(call["name"], call["args"])
                elif tool_handlers and call["name"] in tool_handlers:
                    result = tool_handlers[call["name"]](**call["args"])
                    output = await result if hasattr(result, "__await__") else result
                else:
                    output = f"Unknown tool: {call['name']}"
            except Exception as exc:
                output = f"Error: {exc}"
            results.append({
                "tool_call": call["tool_call"],
                "name": call["name"],
                "args": call["args"],
                "output": output,
            })
    return results
