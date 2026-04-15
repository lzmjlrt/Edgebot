"""
edgebot/agent/loop.py - Main agent loop and system prompt.
"""

import json

import litellm

from edgebot.agent.compression import auto_compact, estimate_tokens, microcompact
from edgebot.config import API_BASE, API_KEY, MODEL, TOKEN_THRESHOLD, WORKDIR


def build_system_prompt(skills_descriptions: str) -> str:
    return (
        f"You are a coding agent at {WORKDIR}. Use tools to solve tasks.\n"
        "Prefer task_create/task_update/task_list for multi-step work. "
        "Use TodoWrite for short checklists.\n"
        "Use task for subagent delegation. Use load_skill for specialized knowledge.\n"
        f"Skills: {skills_descriptions}"
    )


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


def agent_loop(
    messages: list,
    system: str,
    tools: list,
    tool_handlers: dict,
    todo_mgr,
    bg_mgr,
    bus,
):
    """
    Main agentic loop.

    Args:
        messages:      Conversation history (mutated in place).
        system:        System prompt string.
        tools:         OpenAI function-calling tool schema list.
        tool_handlers: {tool_name: callable(**kwargs) -> str}
        todo_mgr:      TodoManager instance (for nag reminder).
        bg_mgr:        BackgroundManager instance (drain notifications).
        bus:           MessageBus instance (read lead inbox).
    """
    rounds_without_todo = 0

    while True:
        # s06: compression pipeline
        microcompact(messages)
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            print("[auto-compact triggered]")
            messages[:] = auto_compact(messages)

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

        # LLM call (system prompt as first message)
        call_messages = [{"role": "system", "content": system}] + messages
        response = litellm.completion(
            model=MODEL, messages=call_messages,
            tools=tools, max_tokens=8000,
            api_key=API_KEY, api_base=API_BASE,
        )
        choice = response.choices[0]
        messages.append(_serialize_assistant(choice.message))

        if choice.finish_reason != "tool_calls":
            if choice.message.content:
                print(choice.message.content)
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
                output = handler(**args) if handler else f"Unknown tool: {name}"
            except Exception as e:
                output = f"Error: {e}"
            print(f"> {name}: {str(output)[:200]}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(output),
            })
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
            print("[manual compact]")
            messages[:] = auto_compact(messages)
