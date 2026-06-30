"""
edgebot/agent/runner.py - Shared execution loop for tool-using agents.

Decoupled from UI rendering and session management. The caller provides
an LLMProvider and tool definitions; the runner handles the iterative
LLM-call -> tool-execution loop and returns the result.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from rich.console import Console

from edgebot.agent.compression import estimate_tokens
from edgebot.agent.token_budget import input_token_budget
from edgebot.providers.base import LLMProvider, ToolCallRequest
from edgebot.session.store import find_legal_start
from edgebot.tools.orchestration import execute_tool_batches

_console = Console()

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_MAX_CONTEXT_EMERGENCY_COMPACTS = 1
_MICROCOMPACT_KEEP_RECENT = 10
_MICROCOMPACT_MIN_CHARS = 500
_TOOL_RESULT_PREVIEW_CHARS = 1200
_DEFAULT_MAX_TOOL_RESULT_TOKENS = 24_000
_COMPACTABLE_TOOLS = frozenset({
    "read_file", "bash", "grep", "glob",
    "web_search", "web_fetch", "list_dir",
})
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"
_LENGTH_RECOVERY_PROMPT = (
    "Output limit reached. Continue exactly where you left off "
    "— no recap, no apology. Break remaining work into smaller steps if needed."
)


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
    max_input_tokens: int | None = None
    temperature: float = 0.7
    retry_mode: str = "standard"
    max_tool_result_chars: int = 16_000
    max_tool_result_tokens: int | None = _DEFAULT_MAX_TOOL_RESULT_TOKENS
    session_key: str = "default"
    tool_result_root: Path | None = None
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
    new_messages: list[dict[str, Any]] = field(default_factory=list)


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        messages = [dict(message) for message in spec.initial_messages]
        new_messages: list[dict[str, Any]] = []
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        stop_reason = "completed"
        empty_content_retries = 0
        length_recoveries = 0
        context_emergency_compacts = 0

        def append_message(message: dict[str, Any]) -> dict[str, Any]:
            stored = dict(message)
            messages.append(stored)
            new_messages.append(stored)
            return stored

        for iteration in range(spec.max_iterations):
            # Context governance is model-facing only. Synthetic repair
            # messages must not shift the persisted append boundary.
            call_messages = _prepare_messages_for_model(messages, spec)

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
                        messages=call_messages,
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
                        messages=call_messages,
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
                    new_messages=list(new_messages),
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

            # ---- Context-length emergency: compact then retry ----
            if (
                response.finish_reason == "error"
                and getattr(response, "error_kind", None) == "context_length"
                and context_emergency_compacts < _MAX_CONTEXT_EMERGENCY_COMPACTS
            ):
                context_emergency_compacts += 1
                if spec.emit_output:
                    _console.print(
                        "[dim yellow]  Context length exceeded — "
                        "compacting and retrying...[/dim yellow]"
                    )
                try:
                    from edgebot.agent.compression import auto_compact
                    messages = await auto_compact(messages, is_idle=False)
                except Exception as exc:
                    if spec.emit_output:
                        _console.print(
                            f"[red]  Emergency compact failed: {exc}[/red]"
                        )
                    return AgentRunResult(
                        final_content=str(exc),
                        messages=messages,
                        tool_names_used=tools_used,
                        usage=usage,
                        stop_reason="error",
                        new_messages=list(new_messages),
                    )
                empty_content_retries = 0
                continue

            # ---- Length recovery: output hit max_tokens, ask to continue ----
            if (
                response.finish_reason == "length"
                and length_recoveries < _MAX_LENGTH_RECOVERIES
            ):
                # Tool_calls under finish_reason="length" are likely truncated
                # (incomplete arguments JSON) — never execute them. Preserve
                # any partial assistant text so the model can continue from it.
                length_recoveries += 1
                if spec.emit_output:
                    _console.print(
                        f"[dim yellow]  Output truncated (length), "
                        f"continuing... ({length_recoveries}/"
                        f"{_MAX_LENGTH_RECOVERIES})[/dim yellow]"
                    )
                if response.content and response.content.strip():
                    append_message({
                        "role": "assistant",
                        "content": response.content,
                    })
                append_message({
                    "role": "user",
                    "content": _LENGTH_RECOVERY_PROMPT,
                })
                empty_content_retries = 0
                continue

            # ---- Handle tool calls ----
            if response.should_execute_tools:
                if spec.on_stream_end is not None:
                    await spec.on_stream_end(resuming=True)

                asst_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                }
                asst_msg = append_message(asst_msg)
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

                tool_status = None
                active_tool_executions = 0

                async def _on_tool_execution_start(name: str, args: dict[str, Any]) -> None:
                    nonlocal tool_status, active_tool_executions
                    if not spec.emit_output:
                        return
                    if name == "ask_user":
                        return
                    active_tool_executions += 1
                    if tool_status is None:
                        tool_status = _console.status(
                            "[dim]Edgebot is thinking...[/dim]", spinner="dots"
                        )
                        tool_status.start()

                async def _on_tool_execution_end(name: str, args: dict[str, Any]) -> None:
                    nonlocal tool_status, active_tool_executions
                    if not spec.emit_output:
                        return
                    if name == "ask_user":
                        return
                    active_tool_executions = max(0, active_tool_executions - 1)
                    if active_tool_executions == 0 and tool_status is not None:
                        try:
                            tool_status.stop()
                        finally:
                            tool_status = None

                executed_calls = await execute_tool_batches(
                    [tc.to_openai_tool_call() for tc in response.tool_calls],
                    tool_handlers=spec.tool_handlers,
                    on_execution_start=_on_tool_execution_start,
                    on_execution_end=_on_tool_execution_end,
                )

                completed_results: list[dict[str, Any]] = []
                for executed in executed_calls:
                    tc = executed["tool_call"]
                    tool_name = executed.get("name") or tc.get("function", {}).get("name") or "tool"
                    output = str(executed["output"])
                    output = _prepare_tool_result_content(
                        output,
                        tool_name=tool_name,
                        tool_call_id=str(tc.get("id") or "tool_call"),
                        spec=spec,
                    )
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tool_name,
                        "content": output,
                    }
                    tool_msg = append_message(tool_msg)
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
                        for message in injected:
                            append_message(message)

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
                    append_message({"role": "assistant", "content": final_content})
                for message in injected_messages:
                    append_message(message)
                if spec.on_stream_end is not None:
                    await spec.on_stream_end(resuming=True)
                empty_content_retries = 0
                continue

            # Truly final — no more work
            if spec.on_stream_end is not None:
                await spec.on_stream_end(resuming=False)

            if final_content:
                append_message({"role": "assistant", "content": final_content})

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
            append_message({"role": "assistant", "content": final_content})

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tool_names_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            new_messages=list(new_messages),
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


def _prepare_messages_for_model(
    messages: list[dict[str, Any]],
    spec: AgentRunSpec,
) -> list[dict[str, Any]]:
    """Return a governed request copy without changing the persisted transcript."""
    model_messages = [dict(message) for message in messages]
    model_messages = _drop_orphan_tool_results(model_messages)
    model_messages = _backfill_missing_tool_results(model_messages)
    model_messages = _microcompact(model_messages)
    governed_messages = _apply_tool_result_budget(model_messages, spec)
    return _apply_input_token_budget(governed_messages, spec)


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


def _safe_session_dir_name(session_key: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in session_key)
    return safe.strip("._") or "default"


def _safe_tool_result_name(tool_call_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in tool_call_id)
    return (safe.strip("._") or "tool_call") + ".txt"


def _tool_result_root(spec: AgentRunSpec) -> Path:
    if spec.tool_result_root is not None:
        return Path(spec.tool_result_root)
    from edgebot.config import RUNTIME_DIR
    return RUNTIME_DIR / "tool-results"


def _prepare_tool_result_content(
    output: str,
    *,
    tool_name: str,
    tool_call_id: str,
    spec: AgentRunSpec,
) -> str:
    """Offload large non-read_file tool outputs and return context content."""
    if tool_name != "read_file" and len(output) > spec.max_tool_result_chars:
        root = _tool_result_root(spec)
        path = (
            root
            / _safe_session_dir_name(spec.session_key)
            / _safe_tool_result_name(tool_call_id)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        preview = output[:_TOOL_RESULT_PREVIEW_CHARS]
        return (
            "[Tool result offloaded]\n"
            f"Path: {path}\n"
            f"Original size: {len(output)} chars\n"
            f"Preview:\n{preview}"
        )

    if len(output) > spec.max_tool_result_chars:
        return output[:spec.max_tool_result_chars] + "\n...[truncated]"
    return output


def _apply_tool_result_budget(
    messages: list[dict[str, Any]],
    spec: AgentRunSpec,
) -> list[dict[str, Any]]:
    """Replace old tool results when aggregate tool output exceeds budget."""
    max_tool_result_tokens = spec.max_tool_result_tokens
    if max_tool_result_tokens is None:
        return messages
    max_tool_result_tokens = max(0, int(max_tool_result_tokens))
    tool_indices = [
        idx for idx, msg in enumerate(messages)
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str)
    ]
    if not tool_indices:
        return messages

    tool_messages = [messages[idx] for idx in tool_indices]
    if estimate_tokens(tool_messages) <= max_tool_result_tokens:
        return messages

    keep: set[int] = set()
    kept_messages: list[dict[str, Any]] = []
    for idx in reversed(tool_indices):
        msg = messages[idx]
        candidate = [msg] + kept_messages
        if kept_messages and estimate_tokens(candidate) > max_tool_result_tokens:
            continue
        keep.add(idx)
        kept_messages = candidate
        if estimate_tokens(kept_messages) > max_tool_result_tokens:
            break

    updated = [dict(message) for message in messages]
    for idx in tool_indices:
        if idx in keep:
            continue
        name = updated[idx].get("name") or "tool"
        updated[idx]["content"] = (
            f"[{name} result omitted from context due to tool-result budget]"
        )
    return updated


def _drop_incomplete_tool_call_groups(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove orphan tools and assistant tool_call groups without full results."""
    updated: list[dict[str, Any]] = []
    idx = 0
    changed = False
    while idx < len(messages):
        msg = messages[idx]
        role = msg.get("role")
        if role == "tool":
            changed = True
            idx += 1
            continue
        if role != "assistant" or not msg.get("tool_calls"):
            updated.append(dict(msg))
            idx += 1
            continue

        call_ids = {
            str(tc.get("id"))
            for tc in msg.get("tool_calls") or []
            if isinstance(tc, dict) and tc.get("id")
        }
        group = [dict(msg)]
        found: set[str] = set()
        cursor = idx + 1
        while cursor < len(messages) and messages[cursor].get("role") == "tool":
            tool_msg = messages[cursor]
            group.append(dict(tool_msg))
            tool_call_id = tool_msg.get("tool_call_id")
            if tool_call_id:
                found.add(str(tool_call_id))
            cursor += 1

        if call_ids and call_ids.issubset(found):
            updated.extend(group)
        else:
            changed = True
        idx = cursor

    return updated if changed else messages


def _apply_input_token_budget(
    messages: list[dict[str, Any]],
    spec: AgentRunSpec,
) -> list[dict[str, Any]]:
    """Return a legal suffix that fits the model-aware input budget."""
    max_input_tokens = spec.max_input_tokens
    if max_input_tokens is None:
        max_input_tokens = input_token_budget(
            spec.model,
            max_completion_tokens=spec.max_tokens,
        )
    max_input_tokens = max(0, int(max_input_tokens))
    if estimate_tokens(messages) <= max_input_tokens:
        return _drop_incomplete_tool_call_groups(messages)

    system_prefix: list[dict[str, Any]] = []
    body_start = 0
    for idx, message in enumerate(messages):
        if message.get("role") != "system":
            body_start = idx
            break
        system_prefix.append(dict(message))
    else:
        return [dict(message) for message in messages]

    selected: list[dict[str, Any]] = []
    body = messages[body_start:]
    for message in reversed(body):
        candidate = [message] + selected
        if selected and estimate_tokens(system_prefix + candidate) > max_input_tokens:
            break
        selected = candidate
        if estimate_tokens(system_prefix + selected) > max_input_tokens:
            break

    start = find_legal_start(selected)
    selected = selected[start:]
    for idx, message in enumerate(selected):
        if message.get("role") == "user" or (
            message.get("role") == "assistant" and message.get("tool_calls")
        ):
            selected = selected[idx:]
            break

    repaired = _drop_incomplete_tool_call_groups(selected)
    return system_prefix + [dict(message) for message in repaired]
