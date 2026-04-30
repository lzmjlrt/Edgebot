"""
edgebot/agent/loop.py - Main agent loop with Rich streaming UI.

Orchestrates compression, notification draining, inbox checking, and
delegates the LLM-call / tool-execution inner loop to AgentRunner.
"""

import asyncio
import json
import sys

from rich.console import Console

from edgebot.agent.compression import auto_compact, estimate_tokens, extract_session_summary, microcompact
from edgebot.agent.context import merge_runtime_context_into_messages
from edgebot.agent.memory import MemoryStore, consolidate_memory
from edgebot.agent.runner import AgentRunSpec, AgentRunner
from edgebot.config import IDLE_COMPACT_MINUTES, MODEL, TOKEN_THRESHOLD, WORKDIR, create_provider
from edgebot.tools.registry import SUBAGENT, set_tool_runtime_context

_console = Console()
_CONSOLIDATION_INTERVAL = 15
_MIN_HISTORY_FOR_MEMORY = 10
_turn_counter = 0
_MEMORY = MemoryStore(WORKDIR)
_AUTOCOMPACT = None


def get_autocompact(session_store) -> object | None:
    """Return (and lazily create) the shared AutoCompact instance."""
    global _AUTOCOMPACT
    if _AUTOCOMPACT is not None:
        return _AUTOCOMPACT
    if IDLE_COMPACT_MINUTES <= 0 or session_store is None:
        return None
    from edgebot.agent.autocompact import AutoCompact
    _AUTOCOMPACT = AutoCompact(
        session_store=session_store,
        provider=create_provider(),
        model=MODEL,
        ttl_minutes=IDLE_COMPACT_MINUTES,
    )
    return _AUTOCOMPACT


def _flush_stdin() -> None:
    """Drop any keystrokes typed while the model was generating."""
    try:
        if sys.platform == "win32":
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getwch()
        else:
            import termios
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def _archive_turn_summary(messages: list[dict], final_response: str) -> None:
    """Write a compact summary of this turn to history.jsonl for Dream."""
    # Find the last user message
    user_msg = None
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            content = m["content"]
            # Skip system-injected messages
            if not content.startswith(("<background-results>", "<inbox>", "<reminder>")):
                user_msg = content
                break
    if not user_msg:
        return
    user_preview = user_msg[:200].replace("\n", " ")
    reply_preview = (final_response or "")[:300].replace("\n", " ")
    _MEMORY.append_history(f"User: {user_preview}\nEdgebot: {reply_preview}")


async def agent_loop(
    messages: list,
    system: str,
    tools: list,
    tool_handlers: dict,
    todo_mgr,
    bg_mgr,
    bus,
    session_store=None,
    session_key: str = "default",
    channel: str = "cli",
    chat_id: str = "direct",
    session_summary: str | None = None,
    emit_output: bool = True,
    assistant_label: str = "Edgebot",
):
    provider = create_provider()
    runner = AgentRunner(provider)
    final_response = ""

    # Reset per-session turn counter for Dream gating
    global _turn_counter
    _turn_counter = 0

    # AutoCompact: check if this session was idle and auto-compressed
    autocompact = get_autocompact(session_store)
    if autocompact is not None and session_summary is None:
        _, ac_summary = autocompact.prepare_session(session_key)
        if ac_summary:
            session_summary = ac_summary

    # ---- Pre-processing (once) ----
    set_tool_runtime_context(
        channel=channel, chat_id=chat_id, session_key=session_key,
    )

    microcompact(messages)
    if estimate_tokens(messages) > TOKEN_THRESHOLD:
        if emit_output:
            _console.print("[dim]  Auto-compressing context...[/dim]")
        messages[:] = await auto_compact(
            messages, is_idle=False, memory_store=_MEMORY,
        )
        if session_store:
            session_store.save_all(session_key, messages)
            session_store.update_metadata(
                session_key,
                session_summary=extract_session_summary(messages) or "",
            )

    # ---- Injection callback: drains mid-turn notifications into the runner ----
    async def _injection_callback() -> list[dict]:
        injected: list[dict] = []

        notifs = bg_mgr.drain()
        if notifs:
            txt = "\n".join(
                (
                    f"[bg:{n['task_id']}] {n['status']} "
                    f"(output: {n.get('output_file', 'n/a')}): {n['result']}"
                )
                for n in notifs
            )
            injected.append({
                "role": "user",
                "content": f"<background-results>\n{txt}\n</background-results>",
            })

        agent_notifs = await SUBAGENT.drain()
        if agent_notifs:
            txt = "\n".join(
                (
                    f"[agent:{n['task_id']}] {n['status']} "
                    f"(output: {n.get('output_file', 'n/a')}): {n['result']}"
                )
                for n in agent_notifs
            )
            injected.append({
                "role": "user",
                "content": f"<background-results>\n{txt}\n</background-results>",
            })

        inbox = bus.read_inbox("lead")
        if inbox:
            injected.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })

        return injected

    # Drain any notifications that arrived before this turn.
    pre_notifs = await _injection_callback()
    if pre_notifs:
        messages.extend(pre_notifs)

    # ---- Build the full prompt for the runner ----
    call_messages = [{"role": "system", "content": system}] + merge_runtime_context_into_messages(
        messages,
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
        session_summary=session_summary,
    )

    # ---- Session checkpoint callback ----
    async def _checkpoint(payload: dict) -> None:
        if session_store is None:
            return
        session_store.update_metadata(session_key, runtime_checkpoint=payload)

    # ---- Streaming callbacks for Rich UI ----

    async def _on_stream(delta: str) -> None:
        if emit_output:
            _console.print(delta, end="", highlight=False, soft_wrap=True)

    async def _on_stream_end(*, resuming: bool) -> None:
        if emit_output:
            _console.print()

    async def _on_retry_wait(msg: str) -> None:
        if emit_output:
            _console.print(f"[dim yellow]  {msg}[/dim yellow]")

    # ---- Run the agent (single call, runner handles tool loop internally) ----
    result = await runner.run(AgentRunSpec(
        initial_messages=call_messages,
        provider=provider,
        tools=tools,
        tool_handlers=tool_handlers,
        model=MODEL,
        max_iterations=200,
        max_tokens=8000,
        retry_mode="standard",
        emit_output=emit_output,
        assistant_label=assistant_label,
        on_stream=_on_stream,
        on_stream_end=_on_stream_end,
        on_retry_wait=_on_retry_wait,
        checkpoint_callback=_checkpoint,
        injection_callback=_injection_callback,
    ))

    final_response = result.final_content or ""
    _flush_stdin()

    if result.stop_reason in ("error", "empty_final_response") and emit_output:
        _console.print(
            f"[red]  Agent stopped ({result.stop_reason}): "
            f"{final_response[:200]}[/red]"
        )
        if result.stop_reason == "empty_final_response":
            _console.print(
                "[dim yellow]  Tip: use /compact to shrink context or /new to start fresh.[/dim yellow]"
            )

    # ---- Extract new messages from the runner result ----
    original_count = len(call_messages)
    new_msgs = result.messages[original_count:]

    # Update the caller's messages list
    messages.extend(new_msgs)

    # Save to session
    if session_store:
        session_store.clear_metadata_keys(
            session_key, "runtime_checkpoint",
        )
        session_store.batch_append(session_key, new_msgs)

    # ---- Post-processing (once) ----

    # Handle manual compress
    if "compress" in result.tool_names_used:
        if emit_output:
            _console.print("[dim]  Compressing...[/dim]")
        messages[:] = await auto_compact(
            messages, is_idle=False, memory_store=_MEMORY,
        )
        if session_store:
            session_store.save_all(session_key, messages)
            session_store.update_metadata(
                session_key,
                session_summary=extract_session_summary(messages) or "",
            )

    # Archive turn summary to history.jsonl for Dream
    if final_response:
        _archive_turn_summary(messages, final_response)

    # Memory consolidation (gated by interval + minimum history)
    _turn_counter += 1
    if (
        _turn_counter >= _CONSOLIDATION_INTERVAL
        and len(messages) >= _MIN_HISTORY_FOR_MEMORY
    ):
        _turn_counter = 0
        try:
            await consolidate_memory(messages, store=_MEMORY)
        except Exception as e:
            if emit_output:
                _console.print(f"[dim red]  [memory] error: {e}[/dim red]")

    return final_response
