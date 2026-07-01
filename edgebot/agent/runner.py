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

from edgebot.agent.context_governance import (
    DEFAULT_MAX_TOOL_RESULT_TOKENS,
    ContextGovernanceConfig,
    apply_input_token_budget,
    prepare_messages_for_model,
)
from edgebot.agent.tool_results import (
    ToolResultPolicy,
    prepare_tool_result_content,
    safe_session_dir_name,
    safe_tool_result_name,
)
from edgebot.providers.base import LLMProvider, ToolCallRequest
from edgebot.tools.orchestration import execute_tool_batches

_console = Console()

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_MAX_CONTEXT_EMERGENCY_COMPACTS = 1
_DEFAULT_MAX_TOOL_RESULT_TOKENS = DEFAULT_MAX_TOOL_RESULT_TOKENS
_LENGTH_RECOVERY_PROMPT = (
    "Output limit reached. Continue exactly where you left off "
    "— no recap, no apology. Break remaining work into smaller steps if needed."
)
_MALFORMED_TOOL_CALL_REPAIR_PROMPT = (
    "The previous model response contained malformed tool calls with "
    "invalid function names. Retry with valid tool names matching "
    "^[A-Za-z0-9_-]{1,64}$, or answer without tools."
)
_MAX_MALFORMED_TOOL_CALL_REPAIRS = 2


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
        malformed_tool_call_repairs = 0

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

                valid_tool_calls = [
                    tc for tc in response.tool_calls if tc.has_valid_name()
                ]
                if not valid_tool_calls:
                    malformed_tool_call_repairs += 1
                    if malformed_tool_call_repairs > _MAX_MALFORMED_TOOL_CALL_REPAIRS:
                        final_content = (
                            "Model returned malformed tool calls repeatedly; "
                            "stopping before persisting invalid tool calls."
                        )
                        stop_reason = "error"
                        append_message({
                            "role": "assistant",
                            "content": final_content,
                        })
                        await _emit_checkpoint(spec, {
                            "phase": "final_response",
                            "iteration": iteration,
                            "assistant_message": messages[-1] if messages else None,
                            "completed_tool_results": [],
                            "pending_tool_calls": [],
                        })
                        break

                    append_message({
                        "role": "user",
                        "content": _MALFORMED_TOOL_CALL_REPAIR_PROMPT,
                    })
                    empty_content_retries = 0
                    continue

                asst_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": [tc.to_openai_tool_call() for tc in valid_tool_calls],
                }
                asst_msg = append_message(asst_msg)
                tools_used.extend(tc.name for tc in valid_tool_calls)

                await _emit_checkpoint(spec, {
                    "phase": "awaiting_tools",
                    "iteration": iteration,
                    "assistant_message": asst_msg,
                    "completed_tool_results": [],
                    "pending_tool_calls": [tc.to_openai_tool_call() for tc in valid_tool_calls],
                })

                # Show tool hints
                for tc in valid_tool_calls:
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
                    [tc.to_openai_tool_call() for tc in valid_tool_calls],
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
                malformed_tool_call_repairs = 0
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
            malformed_tool_call_repairs = 0

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


def _context_governance_config(spec: AgentRunSpec) -> ContextGovernanceConfig:
    return ContextGovernanceConfig(
        model=spec.model,
        max_tokens=spec.max_tokens,
        max_input_tokens=spec.max_input_tokens,
        max_tool_result_tokens=spec.max_tool_result_tokens,
    )


def _prepare_messages_for_model(
    messages: list[dict[str, Any]],
    spec: AgentRunSpec,
) -> list[dict[str, Any]]:
    return prepare_messages_for_model(messages, _context_governance_config(spec))


def _safe_session_dir_name(session_key: str) -> str:
    return safe_session_dir_name(session_key)


def _safe_tool_result_name(tool_call_id: str) -> str:
    return safe_tool_result_name(tool_call_id)


def _tool_result_root(spec: AgentRunSpec) -> Path:
    policy = _tool_result_policy(spec)
    if policy.root is not None:
        return Path(policy.root)
    from edgebot.config import RUNTIME_DIR

    return RUNTIME_DIR / "tool-results"


def _tool_result_policy(spec: AgentRunSpec) -> ToolResultPolicy:
    return ToolResultPolicy(
        max_chars=spec.max_tool_result_chars,
        session_key=spec.session_key,
        root=spec.tool_result_root,
    )


def _prepare_tool_result_content(
    output: str,
    *,
    tool_name: str,
    tool_call_id: str,
    spec: AgentRunSpec,
) -> str:
    return prepare_tool_result_content(
        output,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        policy=_tool_result_policy(spec),
    )


def _apply_input_token_budget(
    messages: list[dict[str, Any]],
    spec: AgentRunSpec,
) -> list[dict[str, Any]]:
    return apply_input_token_budget(messages, _context_governance_config(spec))
