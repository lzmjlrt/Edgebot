"""
edgebot/providers/litellm_provider.py - LiteLLM-based provider implementation.

Wraps litellm.acompletion (non-streaming and streaming) behind the
LLMProvider interface so the rest of Edgebot never touches litellm directly.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import litellm

litellm.suppress_debug_info = True

from edgebot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class LiteLLMProvider(LLMProvider):
    """LLMProvider backed by LiteLLM (supports any OpenAI-compatible API)."""

    def __init__(
        self,
        api_key: str,
        model: str,
        api_base: str | None = None,
    ):
        super().__init__(api_key=api_key, api_base=api_base)
        self._model = model

    def get_default_model(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        safe_messages = self.enforce_role_alternation(messages)
        resp = await litellm.acompletion(
            model=model or self._model,
            messages=safe_messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            api_key=self.api_key,
            api_base=self.api_base,
        )
        return self._parse_sync_response(resp)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        safe_messages = self.enforce_role_alternation(messages)
        stream = await litellm.acompletion(
            model=model or self._model,
            messages=safe_messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            api_key=self.api_key,
            api_base=self.api_base,
            stream=True,
        )

        content_parts: list[str] = []
        tool_calls_buf: dict[int, dict] = {}
        finish_reason = "stop"

        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta

            text = getattr(delta, "content", None)
            if text:
                content_parts.append(text)
                if on_content_delta:
                    await on_content_delta(text)

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

        tool_calls = [
            ToolCallRequest(
                id=b["id"],
                name=b["name"],
                arguments=_parse_json(b["arguments"] or "{}"),
            )
            for _, b in sorted(tool_calls_buf.items())
            if b["name"]
        ]

        return LLMResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _parse_sync_response(resp) -> LLMResponse:
        choice = resp.choices[0]
        content = choice.message.content if choice.message else None
        finish_reason = choice.finish_reason or "stop"

        tool_calls: list[ToolCallRequest] = []
        for tc in getattr(choice.message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            args = _parse_json(getattr(fn, "arguments", "{}") or "{}") if fn else {}
            tool_calls.append(ToolCallRequest(
                id=tc.id,
                name=getattr(fn, "name", "") if fn else "",
                arguments=args,
            ))

        usage: dict[str, int] = {}
        if hasattr(resp, "usage") and resp.usage:
            usage = {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(resp.usage, "total_tokens", 0) or 0,
            }

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )


def _parse_json(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
