"""
edgebot/tools/builtin/ask.py - Interactive ask_user tool with arrow-key picker.

When the agent needs the user's decision, it calls ask_user. The tool blocks
until the user picks an option or types free text, then returns the answer
as a normal tool result — no interrupt/resume needed.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from edgebot.tools.base import BaseTool

AskHandler = Callable[[str, list[str] | None], Awaitable[str]]

_handler: AskHandler | None = None


def set_ask_handler(handler: AskHandler | None) -> None:
    global _handler
    _handler = handler


class AskUserTool(BaseTool):
    """Ask the user a question and wait for their response."""

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return (
            "Ask the user a question and wait for their response. "
            "Use this ONLY when you genuinely need the user's input before proceeding "
            "(e.g., choosing between approaches, confirming a destructive action, clarifying intent). "
            "Provide 'options' as a list of suggested answers; the user can also type free text. "
            "Do NOT use for status updates or rhetorical questions."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Suggested answers the user can pick from.",
                },
            },
            "required": ["question"],
        }

    def is_read_only(self, params: dict[str, Any] | None = None) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        question = kwargs.get("question", "")
        options = kwargs.get("options")
        if _handler is not None:
            return await _handler(question, options)
        return question


def pending_ask_user_id(messages: list[dict]) -> str | None:
    """Find a pending ask_user tool call without a result."""
    pending: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and isinstance(tc.get("id"), str):
                    fn = tc.get("function") or {}
                    name = fn.get("name") if isinstance(fn, dict) else tc.get("name")
                    if isinstance(name, str):
                        pending[tc["id"]] = name
        elif msg.get("role") == "tool":
            tid = msg.get("tool_call_id")
            if isinstance(tid, str):
                pending.pop(tid, None)
    for tid, name in reversed(pending.items()):
        if name == "ask_user":
            return tid
    return None
