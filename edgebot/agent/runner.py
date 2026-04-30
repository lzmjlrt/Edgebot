"""
edgebot/agent/runner.py - Shared execution loop for tool-using agents.

Decoupled from UI rendering and session management. The caller provides
an LLMProvider and tool definitions; the runner handles the iterative
LLM-call -> tool-execution loop and returns the result.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from rich.console import Console

from edgebot.providers.base import LLMProvider, ToolCallRequest
from edgebot.tools.orchestration import execute_tool_batches

_console = Console()

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_MAX_EMPTY_RETRIES = 2
_MICROCOMPACT_KEEP_RECENT = 10
_MICROCOMPACT_MIN_CHARS = 500
_COMPACTABLE_TOOLS = frozenset({
    "read_file", "bash", "grep", "glob",
    "web_search", "web_fetch", "list_dir",
})
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"


@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]]
    provider: LLMProvider
    tools: list[dict[str, Any]]
    tool_handlers: dict[str, Any]
    model: str
    max_iterations: int = 200
    max_tokens: int = 8192
    temperature: float = 0.7
    retry_mode: str = "standard"
    max_tool_result_chars: int = 16_000
    emit_output: bool = True
    assistant_label: str = "Edgebot"
    on_progress: Callable[..., Awaitable[None]] | None = None
    on_stream: Callable[[str], Awaitable[None]] | None = None
    on_stream_end: Callable[..., Awaitable[None]] | None = None
    on_retry_wait: Callable[[str], Awaitable[None]] | None = None
    checkpoint_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    injection_callback: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of an agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tool_names_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        messages = list(spec.initial_messages)
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        stop_reason = "completed"
        empty_content_retries = 0

        for iteration in range(spec.max_iterations):
            # Context governance: clean up any inconsistencies from
            # checkpoint recovery or earlier microcompact passes.
            messages = _drop_orphan_tool_results(messages)
            messages = _backfill_missing_tool_results(messages)
            messages = _microcompact(messages)

            # Streaming LLM call via provider
            first_delta = True
            status = None

            if spec.emit_output:
                status = _console.status(
                    "[dim]Edgebot is thinking...[/dim]", spinner="dots"
                )
                status.start()

            try:
                if spec.on_stream is not None:
                    async def _stream_cb(delta: str, *, _first=[True]) -> None:
                        if spec.emit_output and _first[0]:
                            if status:
                                status.stop()
                            _console.print()
                            _console.print(
                                f"[cyan]{spec.assistant_label}:[/cyan]"
                            )
                            _first[0] = False
                        await spec.on_stream(delta)

                    response = await spec.provider.chat_stream_with_retry(
                        messages=messages,
                        tools=spec.tools,
                        model=spec.model,
                        max_tokens=spec.max_tokens,
                        temperature=spec.temperature,
                        retry_mode=spec.retry_mode,
                        on_content_delta=_stream_cb,
                        on_retry_wait=spec.on_retry_wait,
                    )
                else:
                    response = await spec.provider.chat_with_retry(
                        messages=messages,
                        tools=spec.tools,
                        model=spec.model,
                        max_tokens=spec.max_tokens,
                        temperature=spec.temperature,
                        retry_mode=spec.retry_mode,
                        on_retry_wait=spec.on_retry_wait,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if status:
                    status.stop()
                _console.print(f"[red]  Error: {exc}[/red]")
                return AgentRunResult(
                    final_content=str(exc),
                    messages=messages,
                    tool_names_used=tools_used,
                    usage=usage,
                    stop_reason="error",
                )
            finally:
                if status:
                    try:
                        status.stop()
                    except Exception:
                        pass

            # Emit newline after streaming text finishes
            if spec.emit_output and response.content:
                _console.print()

            # Accumulate usage
            for key, value in response.usage.items():
                try:
                    usage[key] = usage.get(key, 0) + int(value or 0)
                except (TypeError, ValueError):
                    pass

            # ---- Handle tool calls ----
            if response.should_execute_tools:
                if spec.on_stream_end is not None:
                    await spec.on_stream_end(resuming=True)

                asst_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                }
                messages.append(asst_msg)
                tools_used.extend(tc.name for tc in response.tool_calls)

                await _emit_checkpoint(spec, {
                    "phase": "awaiting_tools",
                    "iteration": iteration,
                    "assistant_message": asst_msg,
                    "completed_tool_results": [],
                    "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                })

                # Show tool hints
                for tc in response.tool_calls:
                    _print_tool_hint(tc.name, tc.arguments, spec)

                executed_calls = await execute_tool_batches(
                    [tc.to_openai_tool_call() for tc in response.tool_calls],
                    tool_handlers=spec.tool_handlers,
                )

                completed_results: list[dict[str, Any]] = []
                for executed in executed_calls:
                    tc = executed["tool_call"]
                    output = str(executed["output"])
                    if len(output) > spec.max_tool_result_chars:
                        output = output[:spec.max_tool_result_chars] + "\n...[truncated]"
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": output,
                    }
                    messages.append(tool_msg)
                    completed_results.append(tool_msg)

                await _emit_checkpoint(spec, {
                    "phase": "tools_completed",
                    "iteration": iteration,
                    "assistant_message": asst_msg,
                    "completed_tool_results": completed_results,
                    "pending_tool_calls": [],
                })

                # Injection: drain external events (notifications, subagent results)
                if spec.injection_callback is not None:
                    injected = await spec.injection_callback()
                    if injected:
                        messages.extend(injected)

                empty_content_retries = 0
                continue

            # ---- No tool calls: potentially the final response ----
            # Determine final_content BEFORE calling injection_callback.
            clean = response.content
            if response.finish_reason != "error" and not (clean or "").strip():
                empty_content_retries += 1
                if empty_content_retries < _MAX_EMPTY_RETRIES:
                    continue
                clean = None

            if response.finish_reason == "error":
                final_content = clean or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
            elif not (clean or "").strip():
                final_content = _DEFAULT_ERROR_MESSAGE
                stop_reason = "empty_final_response"
            else:
                final_content = clean

            # Check injection_callback — if new messages arrived, keep looping.
            injected_messages: list[dict[str, Any]] = []
            if spec.injection_callback is not None:
                injected_messages = await spec.injection_callback()
            if injected_messages:
                if final_content:
                    messages.append({"role": "assistant", "content": final_content})
                messages.extend(injected_messages)
                if spec.on_stream_end is not None:
                    await spec.on_stream_end(resuming=True)
                empty_content_retries = 0
                continue

            # Truly final — no more work
            if spec.on_stream_end is not None:
                await spec.on_stream_end(resuming=False)

            if final_content:
                messages.append({"role": "assistant", "content": final_content})

            await _emit_checkpoint(spec, {
                "phase": "final_response",
                "iteration": iteration,
                "assistant_message": messages[-1] if messages else None,
                "completed_tool_results": [],
                "pending_tool_calls": [],
            })
            break
        else:
            stop_reason = "max_iterations"
            final_content = (
                f"Max iterations ({spec.max_iterations}) reached."
            )
            messages.append({"role": "assistant", "content": final_content})

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tool_names_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
        )


# ---- helpers ----


def _print_tool_hint(
    name: str, arguments: dict[str, Any], spec: AgentRunSpec
) -> None:
    from edgebot.cli.tool_hints import format_tool_hint
    hint = format_tool_hint(name, arguments)
    if spec.emit_output:
        _console.print(f"  [dim]↳ {hint}[/dim]")


async def _emit_checkpoint(spec: AgentRunSpec, payload: dict[str, Any]) -> None:
    if spec.checkpoint_callback is not None:
        await spec.checkpoint_callback(payload)


# ---- Context governance ----


def _drop_orphan_tool_results(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop tool results whose tool_call_id has no matching assistant tool_call."""
    declared: set[str] = set()
    updated: list[dict[str, Any]] | None = None
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    declared.add(str(tc["id"]))
        if role == "tool":
            tid = msg.get("tool_call_id")
            if tid and str(tid) not in declared:
                if updated is None:
                    updated = [dict(m) for m in messages[:idx]]
                continue
        if updated is not None:
            updated.append(dict(msg))
    return updated if updated is not None else messages


def _backfill_missing_tool_results(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Insert synthetic error results for assistant tool_calls that lack a tool result."""
    declared: list[tuple[int, str, str]] = []
    fulfilled: set[str] = set()
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    name = ""
                    func = tc.get("function")
                    if isinstance(func, dict):
                        name = func.get("name", "")
                    declared.append((idx, str(tc["id"]), name))
        elif role == "tool":
            tid = msg.get("tool_call_id")
            if tid:
                fulfilled.add(str(tid))

    missing = [(ai, cid, n) for ai, cid, n in declared if cid not in fulfilled]
    if not missing:
        return messages

    updated = list(messages)
    offset = 0
    for assistant_idx, call_id, name in missing:
        insert_at = assistant_idx + 1 + offset
        while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
            insert_at += 1
        updated.insert(insert_at, {
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": _BACKFILL_CONTENT,
        })
        offset += 1
    return updated


def _microcompact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace old compactable tool results with one-line summaries."""
    compactable_indices: list[int] = []
    for idx, msg in enumerate(messages):
        name = msg.get("name")
        if msg.get("role") == "tool" and name in _COMPACTABLE_TOOLS:
            compactable_indices.append(idx)

    if len(compactable_indices) <= _MICROCOMPACT_KEEP_RECENT:
        return messages

    stale = compactable_indices[:len(compactable_indices) - _MICROCOMPACT_KEEP_RECENT]
    updated: list[dict[str, Any]] | None = None
    for idx in stale:
        content = messages[idx].get("content")
        if not isinstance(content, str) or len(content) < _MICROCOMPACT_MIN_CHARS:
            continue
        name = messages[idx].get("name", "tool")
        summary = f"[{name} result omitted from context]"
        if updated is None:
            updated = [dict(m) for m in messages]
        updated[idx]["content"] = summary

    return updated if updated is not None else messages
