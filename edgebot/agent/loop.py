"""
edgebot/agent/loop.py - Main agent loop with Rich UI.
"""

import asyncio
import json

import litellm
from rich.console import Console
from rich.markdown import Markdown

from edgebot.agent.compression import auto_compact, estimate_tokens, microcompact
from edgebot.agent.memory import consolidate_memory
from edgebot.config import API_BASE, API_KEY, MODEL, TOKEN_THRESHOLD

_console = Console()
_CONSOLIDATION_INTERVAL = 5
_turn_counter = 0


def _serialize_assistant(message) -> dict:
    """Convert litellm response message to a storable dict."""
    msg = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in message.tool_calls
        ]
    return msg


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
):
    rounds_without_todo = 0

    while True:
        # s06: compression pipeline
        microcompact(messages)
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            _console.print("[dim]  Auto-compressing context...[/dim]")
            messages[:] = await auto_compact(messages)
            if session_store:
                session_store.save_all(session_key, messages)

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

        # LLM call with thinking spinner
        call_messages = [{"role": "system", "content": system}] + messages
        with _console.status("[dim]Edgebot is thinking...[/dim]", spinner="dots"):
            response = await litellm.acompletion(
                model=MODEL, messages=call_messages,
                tools=tools, max_tokens=8000,
                api_key=API_KEY, api_base=API_BASE,
            )
        choice = response.choices[0]
        asst_msg = _serialize_assistant(choice.message)
        messages.append(asst_msg)
        if session_store:
            session_store.append(session_key, asst_msg)

        if choice.finish_reason != "tool_calls":
            if choice.message.content:
                _console.print()
                _console.print(Markdown(choice.message.content))
            return

        # Tool execution
        used_todo = False
        manual_compress = False

        for tc in choice.message.tool_calls or []:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            if name == "compress":
                manual_compress = True
            handler = tool_handlers.get(name)
            try:
                if handler is None:
                    output = f"Unknown tool: {name}"
                else:
                    result = handler(**args)
                    if asyncio.iscoroutine(result):
                        output = await result
                    else:
                        output = result
            except Exception as e:
                output = f"Error: {e}"
            _console.print(f"  [dim]\u21b3 {name}: {str(output)[:200]}[/dim]")
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(output),
            }
            messages.append(tool_msg)
            if session_store:
                session_store.append(session_key, tool_msg)
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
            _console.print("[dim]  Compressing...[/dim]")
            messages[:] = await auto_compact(messages)
            if session_store:
                session_store.save_all(session_key, messages)

        # Memory consolidation every N turns
        global _turn_counter
        _turn_counter += 1
        if _turn_counter >= _CONSOLIDATION_INTERVAL:
            _turn_counter = 0
            try:
                await consolidate_memory(messages)
            except Exception as e:
                _console.print(f"[dim]  Memory error: {e}[/dim]")
