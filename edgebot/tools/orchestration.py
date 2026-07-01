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


def _resolve_tool(tool_registry: Any | None, tool_name: str) -> Any | None:
    if tool_registry is None:
        return get_tool_instance(tool_name)
    getter = getattr(tool_registry, "get", None)
    if callable(getter):
        return getter(tool_name)
    return None


def _prepare_call_args(
    tool_registry: Any | None,
    tool_name: str,
    args: dict[str, Any],
) -> tuple[Any | None, dict[str, Any], str | None]:
    if tool_registry is not None:
        prepare = getattr(tool_registry, "prepare_call", None)
        if callable(prepare):
            tool, prepared_args, error = prepare(tool_name, args)
            if isinstance(prepared_args, dict):
                return tool, prepared_args, error
            return tool, args, error

    tool = _resolve_tool(tool_registry, tool_name)
    if tool is None:
        return None, args, f"Unknown tool: {tool_name}"

    try:
        cast_args = tool.cast_params(args)
        errors = tool.validate_params(cast_args)
    except Exception as exc:
        return tool, args, f"Error: {exc}"
    if errors:
        return tool, cast_args, f"Error: Invalid parameters for tool '{tool_name}': {'; '.join(errors)}"
    return tool, cast_args, None


def _is_concurrency_safe(
    tool_name: str,
    args: dict[str, Any],
    *,
    tool_registry: Any | None = None,
) -> bool:
    tool, cast_args, error = _prepare_call_args(tool_registry, tool_name, args)
    if tool is None:
        return False
    if error:
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


def partition_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    tool_registry: Any | None = None,
) -> list[dict[str, Any]]:
    """Partition calls into batches of parallel-safe or serial work."""
    batches: list[dict[str, Any]] = []
    for tc in tool_calls:
        fn = tc.get("function", {}) or {}
        name = fn.get("name", "")
        args = _parse_tool_args(fn.get("arguments"))
        _tool, cast_args, prep_error = _prepare_call_args(tool_registry, name, args)
        call = {
            "tool_call": tc,
            "name": name,
            "args": cast_args,
            "prep_error": prep_error,
            "is_concurrency_safe": _is_concurrency_safe(
                name,
                cast_args,
                tool_registry=tool_registry,
            ),
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
    tool_registry: Any | None = None,
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
        if tool_registry is None:
            output = await execute_registered_tool(name, args)
        else:
            execute = getattr(tool_registry, "execute", None)
            if callable(execute):
                output = await execute(name, args)
            else:
                tool, prepared_args, error = _prepare_call_args(tool_registry, name, args)
                if error:
                    output = error
                elif tool is None:
                    output = f"Unknown tool: {name}"
                else:
                    result = tool.execute(**prepared_args)
                    output = await result if hasattr(result, "__await__") else result
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
    tool_registry: Any | None = None,
    permission_manager: Any | None = None,
    on_execution_start: ToolExecutionCallback | None = None,
    on_execution_end: ToolExecutionCallback | None = None,
) -> Any:
    tool = _resolve_tool(tool_registry, name)
    if tool is not None:
        permissions = permission_manager or PERMISSIONS
        decision = await permissions.authorize(name, args, tool)
        if decision.behavior != "allow":
            return f"Error: {decision.message}"
        updated_args = decision.updated_params or args
        return await _execute_authorized_tool(
            name,
            updated_args,
            tool_registry=tool_registry,
            on_execution_start=on_execution_start,
            on_execution_end=on_execution_end,
        )
    return f"Unknown tool: {name}"


def _call_key(call: dict[str, Any]) -> str:
    return str(call.get("tool_call", {}).get("id") or id(call))


async def _preapprove_sensitive_calls(
    calls: list[dict[str, Any]],
    *,
    tool_registry: Any | None = None,
    permission_manager: Any | None = None,
) -> dict[str, str] | None:
    permissions = permission_manager or PERMISSIONS
    if len(calls) < 2 or not permissions.can_batch_prompt():
        return None

    sensitive: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, call in enumerate(calls):
        tool = _resolve_tool(tool_registry, call["name"])
        if tool is None:
            continue
        if not permissions.requires_prompt(call["name"], call["args"], tool):
            continue
        request = permissions.build_request(call["name"], call["args"])
        if request.get("requires_confirmation"):
            return None
        sensitive.append((index, call, request))

    if len(sensitive) < 2:
        return None

    response = await permissions.prompt_batch([item[2] for item in sensitive])
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
    tool_registry: Any | None = None,
    permission_manager: Any | None = None,
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
                        tool_registry=tool_registry,
                        on_execution_start=on_execution_start,
                        on_execution_end=on_execution_end,
                    )
                elif approval is not None:
                    output = approval
                else:
                    output = await _run_single_tool(
                        call["name"],
                        call["args"],
                        tool_registry=tool_registry,
                        permission_manager=permission_manager,
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
    tool_registry: Any | None = None,
    permission_manager: Any | None = None,
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
    batches = partition_tool_calls(tool_calls, tool_registry=tool_registry)
    all_calls = [call for batch in batches for call in batch["calls"]]
    preapprovals = await _preapprove_sensitive_calls(
        all_calls,
        tool_registry=tool_registry,
        permission_manager=permission_manager,
    )
    for batch in batches:
        calls = batch["calls"]
        if batch["parallel"]:
            results.extend(await _run_parallel_calls(
                calls,
                tool_registry=tool_registry,
                permission_manager=permission_manager,
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
                        tool_registry=tool_registry,
                        on_execution_start=on_execution_start,
                        on_execution_end=on_execution_end,
                    )
                elif approval is not None:
                    output = approval
                else:
                    tool = _resolve_tool(tool_registry, call["name"])
                    if tool is not None:
                        output = await _run_single_tool(
                            call["name"],
                            call["args"],
                            tool_registry=tool_registry,
                            permission_manager=permission_manager,
                            on_execution_start=on_execution_start,
                            on_execution_end=on_execution_end,
                        )
                    elif tool_registry is None and tool_handlers and call["name"] in tool_handlers:
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
