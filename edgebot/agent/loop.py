"""
edgebot/agent/loop.py - Main agent loop with Rich streaming UI.
"""

import asyncio
import json
import sys

import litellm
from rich.console import Console

from edgebot.agent.compression import auto_compact, estimate_tokens, extract_session_summary, microcompact
from edgebot.agent.context import merge_runtime_context_into_messages
from edgebot.agent.memory import MemoryStore, consolidate_memory
from edgebot.cli.tool_hints import format_tool_hint
from edgebot.config import API_BASE, API_KEY, MODEL, TOKEN_THRESHOLD, WORKDIR
from edgebot.tools.registry import execute_registered_tool, get_tool_instance, set_tool_runtime_context

_console = Console()
_CONSOLIDATION_INTERVAL = 15
_MIN_HISTORY_FOR_MEMORY = 10
_turn_counter = 0
_MEMORY = MemoryStore(WORKDIR)


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


async def _stream_completion(call_messages, tools, *, emit_output: bool = True, assistant_label: str = "Edgebot"):
    """
    Stream the LLM response. Prints text deltas as they arrive.
    Returns (content_text, tool_calls_list, finish_reason).
    """
    content_parts: list[str] = []
    tool_calls_buf: dict[int, dict] = {}
    finish_reason = "stop"
    first_delta = True
    status = None
    if emit_output:
        status = _console.status("[dim]Edgebot is thinking...[/dim]", spinner="dots")
        status.start()

    try:
        stream = await litellm.acompletion(
            model=MODEL,
            messages=call_messages,
            tools=tools,
            max_tokens=8000,
            api_key=API_KEY,
            api_base=API_BASE,
            stream=True,
        )

        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta

            # Text content streams to stdout token-by-token
            text = getattr(delta, "content", None)
            if text:
                if emit_output and first_delta:
                    status.stop()
                    _console.print()
                    _console.print(f"[cyan]{assistant_label}:[/cyan]")
                    first_delta = False
                if emit_output:
                    _console.print(text, end="", highlight=False, soft_wrap=True)
                content_parts.append(text)

            # Tool call deltas accumulate
            tc_delta = getattr(delta, "tool_calls", None) or []
            for tc in tc_delta:
                idx = getattr(tc, "index", 0) or 0
                buf = tool_calls_buf.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""}
                )
                if getattr(tc, "id", None):
                    buf["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        buf["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        buf["arguments"] += fn.arguments
    finally:
        try:
            if status:
                status.stop()
        except Exception:
            pass
        if emit_output and content_parts:
            _console.print()

    tool_calls = [
        {
            "id": b["id"],
            "type": "function",
            "function": {"name": b["name"], "arguments": b["arguments"] or "{}"},
        }
        for _, b in sorted(tool_calls_buf.items())
        if b["name"]
    ]
    return "".join(content_parts), tool_calls, finish_reason


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
    rounds_without_todo = 0
    final_response = ""

    while True:
        set_tool_runtime_context(channel=channel, chat_id=chat_id, session_key=session_key)
        # s06: compression pipeline
        microcompact(messages)
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            if emit_output:
                _console.print("[dim]  Auto-compressing context...[/dim]")
            messages[:] = await auto_compact(messages, is_idle=False, memory_store=_MEMORY)
            if session_store:
                session_store.save_all(session_key, messages)
                session_store.update_metadata(
                    session_key,
                    session_summary=extract_session_summary(messages) or "",
                )

        # s08: drain background notifications
        notifs = bg_mgr.drain()
        if notifs:
            txt = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            messages.append({
                "role": "user",
                "content": f"<background-results>\n{txt}\n</background-results>",
            })
            messages.append({"role": "assistant", "content": "Noted background results."})

        # s09/s10: check lead inbox
        inbox = bus.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
            messages.append({"role": "assistant", "content": "Noted inbox messages."})

        # ---- Streaming LLM call ----
        call_messages = [{"role": "system", "content": system}] + merge_runtime_context_into_messages(
            messages,
            channel=channel,
            chat_id=chat_id,
            session_key=session_key,
            session_summary=session_summary,
        )
        content_text, tool_calls, finish_reason = await _stream_completion(
            call_messages, tools, emit_output=emit_output, assistant_label=assistant_label
        )
        final_response = content_text or ""

        # Drop any keystrokes the user mashed during generation
        _flush_stdin()

        asst_msg: dict = {"role": "assistant", "content": content_text or None}
        if tool_calls:
            asst_msg["tool_calls"] = tool_calls
        messages.append(asst_msg)
        if session_store:
            session_store.append(session_key, asst_msg)
            if tool_calls:
                session_store.update_metadata(
                    session_key,
                    runtime_checkpoint={
                        "assistant_message": asst_msg,
                        "completed_tool_results": [],
                        "pending_tool_calls": tool_calls,
                    },
                )

        if finish_reason != "tool_calls" or not tool_calls:
            if session_store:
                session_store.clear_metadata_keys(
                    session_key,
                    "pending_user_turn",
                    "runtime_checkpoint",
                )
            return final_response

        # ---- Tool execution ----
        used_todo = False
        manual_compress = False

        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "compress":
                manual_compress = True

            # Show a short hint line for this tool call
            tool_hint = format_tool_hint(name, args)
            tool_status = None
            if emit_output:
                _console.print(f"  [dim]\u21b3 {tool_hint}[/dim]")
                tool_status = _console.status(f"[dim]working {tool_hint}[/dim]", spinner="dots")
                tool_status.start()

            handler = tool_handlers.get(name)
            try:
                tool_instance = get_tool_instance(name)
                if tool_instance is not None:
                    output = await execute_registered_tool(name, args)
                elif handler is None:
                    output = f"Unknown tool: {name}"
                else:
                    result = handler(**args)
                    if asyncio.iscoroutine(result):
                        output = await result
                    else:
                        output = result
            except Exception as e:
                output = f"Error: {e}"
            finally:
                try:
                    if tool_status:
                        tool_status.stop()
                except Exception:
                    pass

            tool_msg = {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": str(output),
            }
            messages.append(tool_msg)
            if session_store:
                session_store.append(session_key, tool_msg)
                checkpoint_state = session_store.load_state(session_key)
                checkpoint = checkpoint_state["metadata"].get("runtime_checkpoint", {})
                completed = list(checkpoint.get("completed_tool_results", []))
                completed.append(tool_msg)
                remaining = [
                    pending
                    for pending in checkpoint.get("pending_tool_calls", [])
                    if pending.get("id") != tc["id"]
                ]
                session_store.update_metadata(
                    session_key,
                    runtime_checkpoint={
                        "assistant_message": checkpoint.get("assistant_message", asst_msg),
                        "completed_tool_results": completed,
                        "pending_tool_calls": remaining,
                    },
                )
            if name == "TodoWrite":
                used_todo = True

        # s03: nag reminder
        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if todo_mgr.has_open_items() and rounds_without_todo >= 3:
            messages.append({
                "role": "user",
                "content": "<reminder>Update your todos.</reminder>",
            })

        # s06: manual compress
        if manual_compress:
            if emit_output:
                _console.print("[dim]  Compressing...[/dim]")
            messages[:] = await auto_compact(messages, is_idle=False, memory_store=_MEMORY)
            if session_store:
                session_store.save_all(session_key, messages)
                session_store.update_metadata(
                    session_key,
                    session_summary=extract_session_summary(messages) or "",
                )

        if session_store:
            session_store.clear_metadata_keys(session_key, "runtime_checkpoint")

        # Memory consolidation (gated by interval + minimum history)
        global _turn_counter
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
