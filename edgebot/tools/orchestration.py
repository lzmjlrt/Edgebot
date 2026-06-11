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
from typing import Any, Awaitable, Callable

from edgebot.permissions.hooks import run_permission_hooks
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


ToolExecutionCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def _denied_output(name: str, message: str | None = None) -> str:
    detail = f": {message}" if message else ""
    return f"Error: Permission denied for tool '{name}'{detail}"


def _hook_payload(name: str, args: dict[str, Any], output: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": name,
        "params": args,
    }
    if output is not None:
        payload["output"] = str(output)
    return payload


async def _execute_authorized_tool(
    name: str,
    args: dict[str, Any],
    *,
    on_execution_start: ToolExecutionCallback | None = None,
    on_execution_end: ToolExecutionCallback | None = None,
) -> Any:
    pre_errors = run_permission_hooks("PreToolUse", _hook_payload(name, args))
    if pre_errors:
        return "Error: " + "; ".join(pre_errors)

    started = False
    if on_execution_start is not None:
        await on_execution_start(name, args)
        started = True
    try:
        output = await execute_registered_tool(name, args)
    finally:
        if started and on_execution_end is not None:
            await on_execution_end(name, args)

    post_errors = run_permission_hooks("PostToolUse", _hook_payload(name, args, output))
    if post_errors:
        return f"{output}\n\nPostToolUse hook errors: " + "; ".join(post_errors)
    return output


async def _run_single_tool(
    name: str,
    args: dict[str, Any],
    *,
    on_execution_start: ToolExecutionCallback | None = None,
    on_execution_end: ToolExecutionCallback | None = None,
) -> Any:
    tool = get_tool_instance(name)
    if tool is not None:
        decision = await PERMISSIONS.authorize(name, args, tool)
        if decision.behavior != "allow":
            return f"Error: {decision.message}"
        updated_args = decision.updated_params or args
        return await _execute_authorized_tool(
            name,
            updated_args,
            on_execution_start=on_execution_start,
            on_execution_end=on_execution_end,
        )
    return f"Unknown tool: {name}"


def _call_key(call: dict[str, Any]) -> str:
    return str(call.get("tool_call", {}).get("id") or id(call))


async def _preapprove_sensitive_calls(calls: list[dict[str, Any]]) -> dict[str, str] | None:
    if len(calls) < 2 or not PERMISSIONS.can_batch_prompt():
        return None

    sensitive: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, call in enumerate(calls):
        tool = get_tool_instance(call["name"])
        if tool is None:
            continue
        if not PERMISSIONS.requires_prompt(call["name"], call["args"], tool):
            continue
        request = PERMISSIONS.build_request(call["name"], call["args"])
        if request.get("requires_confirmation"):
            return None
        sensitive.append((index, call, request))

    if len(sensitive) < 2:
        return None

    response = await PERMISSIONS.prompt_batch([item[2] for item in sensitive])
    if not isinstance(response, dict):
        return {_call_key(call): _denied_output(call["name"]) for _index, call, _request in sensitive}

    action = str(response.get("action", "deny_all"))
    if action == "review_one_by_one":
        return None
    if action == "allow_all":
        return {_call_key(call): "allow" for _index, call, _request in sensitive}
    message = str(response.get("message") or response.get("feedback") or "").strip()
    return {
        _call_key(call): _denied_output(call["name"], message)
        for _index, call, _request in sensitive
    }


async def _run_parallel_calls(
    calls: list[dict[str, Any]],
    *,
    on_execution_start: ToolExecutionCallback | None = None,
    on_execution_end: ToolExecutionCallback | None = None,
    preapprovals: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(_get_max_tool_concurrency())

    async def _guarded(index: int, call: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                approval = (preapprovals or {}).get(_call_key(call))
                if approval == "allow":
                    output = await _execute_authorized_tool(
                        call["name"],
                        call["args"],
                        on_execution_start=on_execution_start,
                        on_execution_end=on_execution_end,
                    )
                elif approval is not None:
                    output = approval
                else:
                    output = await _run_single_tool(
                        call["name"],
                        call["args"],
                        on_execution_start=on_execution_start,
                        on_execution_end=on_execution_end,
                    )
            except Exception as exc:
                output = f"Error: {exc}"
            return {
                "tool_call": call["tool_call"],
                "name": call["name"],
                "args": call["args"],
                "output": output,
            }

    return await asyncio.gather(*[_guarded(index, call) for index, call in enumerate(calls)])


async def execute_tool_batches(
    tool_calls: list[dict[str, Any]],
    *,
    tool_handlers: dict[str, Any] | None = None,
    on_execution_start: ToolExecutionCallback | None = None,
    on_execution_end: ToolExecutionCallback | None = None,
) -> list[dict[str, Any]]:
    """
    Execute tool calls with conservative orchestration.

    Returns a list of execution records in the same order as the input:
    [{tool_call, name, args, output}, ...]
    """
    results: list[dict[str, Any]] = []
    batches = partition_tool_calls(tool_calls)
    all_calls = [call for batch in batches for call in batch["calls"]]
    preapprovals = await _preapprove_sensitive_calls(all_calls)
    for batch in batches:
        calls = batch["calls"]
        if batch["parallel"]:
            results.extend(await _run_parallel_calls(
                calls,
                on_execution_start=on_execution_start,
                on_execution_end=on_execution_end,
                preapprovals=preapprovals,
            ))
            continue

        for call in calls:
            try:
                approval = (preapprovals or {}).get(_call_key(call))
                if approval == "allow":
                    output = await _execute_authorized_tool(
                        call["name"],
                        call["args"],
                        on_execution_start=on_execution_start,
                        on_execution_end=on_execution_end,
                    )
                elif approval is not None:
                    output = approval
                else:
                    tool = get_tool_instance(call["name"])
                    if tool is not None:
                        output = await _run_single_tool(
                            call["name"],
                            call["args"],
                            on_execution_start=on_execution_start,
                            on_execution_end=on_execution_end,
                        )
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
