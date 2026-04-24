"""
edgebot/providers/base.py - Base LLM provider interface with retry logic.
"""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console

_console = Console()


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_openai_tool_call(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    retry_after: float | None = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def should_execute_tools(self) -> bool:
        """Tools execute only when has_tool_calls AND finish_reason allows it."""
        if not self.has_tool_calls:
            return False
        return self.finish_reason in ("tool_calls", "stop")


@dataclass(frozen=True)
class GenerationSettings:
    temperature: float = 0.7
    max_tokens: int = 8192


class LLMProvider(ABC):
    """Abstract base class for LLM providers with built-in retry logic."""

    _CHAT_RETRY_DELAYS = (1, 2, 4)
    _PERSISTENT_MAX_DELAY = 60
    _PERSISTENT_IDENTICAL_ERROR_LIMIT = 10
    _RETRY_HEARTBEAT_CHUNK = 30
    _TRANSIENT_ERROR_MARKERS = (
        "429", "rate limit", "500", "502", "503", "504",
        "overloaded", "timeout", "timed out", "connection",
        "server error", "temporarily unavailable", "速率限制",
    )

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base
        self.generation = GenerationSettings()

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        pass

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        pass

    # ---- message format repair ----

    _SYNTHETIC_USER_CONTENT = "(conversation continued)"

    @staticmethod
    def enforce_role_alternation(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge consecutive same-role messages and drop trailing assistant messages.

        Some providers reject requests where two consecutive non-system messages
        share the same role, or where the last message is 'assistant'. This method
        normalizes the list so every provider receives valid input.
        """
        if not messages:
            return messages

        merged: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if (
                merged
                and role not in ("system", "tool")
                and merged[-1].get("role") == role
                and role in ("user", "assistant")
            ):
                prev = merged[-1]
                if role == "assistant":
                    prev_has_tools = bool(prev.get("tool_calls"))
                    curr_has_tools = bool(msg.get("tool_calls"))
                    if curr_has_tools:
                        merged[-1] = dict(msg)
                        continue
                    if prev_has_tools:
                        continue
                prev_content = prev.get("content") or ""
                curr_content = msg.get("content") or ""
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    prev["content"] = (prev_content + "\n\n" + curr_content).strip()
                else:
                    merged[-1] = dict(msg)
            else:
                merged.append(dict(msg))

        # Drop trailing assistant messages (some providers reject this).
        # But keep assistant messages with tool_calls — they may be mid-turn.
        last_popped = None
        while (
            merged
            and merged[-1].get("role") == "assistant"
            and not merged[-1].get("tool_calls")
        ):
            last_popped = merged.pop()

        # If we removed everything except system messages, recover the last
        # assistant as a user message so the request is still valid.
        if (
            merged
            and last_popped is not None
            and not any(m.get("role") in ("user", "tool") for m in merged)
        ):
            recovered = dict(last_popped)
            recovered["role"] = "user"
            merged.append(recovered)

        # Ensure the first non-system message is not a bare assistant.
        for i, msg in enumerate(merged):
            if msg.get("role") != "system":
                if msg.get("role") == "assistant" and not msg.get("tool_calls"):
                    merged.insert(i, {
                        "role": "user",
                        "content": LLMProvider._SYNTHETIC_USER_CONTENT,
                    })
                break

        return merged

    # ---- retry infrastructure ----

    @classmethod
    def _is_transient_error(cls, content: str | None) -> bool:
        err = (content or "").lower()
        return any(marker in err for marker in cls._TRANSIENT_ERROR_MARKERS)

    @classmethod
    def _extract_retry_after(cls, content: str | None) -> float | None:
        if not content:
            return None
        text = content.lower()
        patterns = (
            r"retry after\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)?",
            r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)",
        )
        for idx, pattern in enumerate(patterns):
            match = re.search(pattern, text)
            if not match:
                continue
            value = float(match.group(1))
            unit = match.group(2) if idx < len(patterns) else "s"
            return cls._to_retry_seconds(value, unit)
        return None

    @classmethod
    def _to_retry_seconds(cls, value: float, unit: str | None = None) -> float:
        normalized = (unit or "s").lower()
        if normalized in {"ms", "milliseconds"}:
            return max(0.1, value / 1000.0)
        if normalized in {"m", "min", "minutes"}:
            return max(0.1, value * 60.0)
        return max(0.1, value)

    async def _safe_chat(self, **kwargs: Any) -> LLMResponse:
        try:
            return await self.chat(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error")

    async def _safe_chat_stream(self, **kwargs: Any) -> LLMResponse:
        try:
            return await self.chat_stream(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error")

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        retry_mode: str = "standard",
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        if max_tokens is None:
            max_tokens = self.generation.max_tokens
        if temperature is None:
            temperature = self.generation.temperature
        kw: dict[str, Any] = dict(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, temperature=temperature,
        )
        return await self._run_with_retry(
            self._safe_chat, kw,
            retry_mode=retry_mode, on_retry_wait=on_retry_wait,
        )

    async def chat_stream_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        retry_mode: str = "standard",
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        if max_tokens is None:
            max_tokens = self.generation.max_tokens
        if temperature is None:
            temperature = self.generation.temperature
        kw: dict[str, Any] = dict(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, temperature=temperature,
            on_content_delta=on_content_delta,
        )
        return await self._run_with_retry(
            self._safe_chat_stream, kw,
            retry_mode=retry_mode, on_retry_wait=on_retry_wait,
        )

    async def _run_with_retry(
        self,
        call: Callable[..., Awaitable[LLMResponse]],
        kw: dict[str, Any],
        *,
        retry_mode: str,
        on_retry_wait: Callable[[str], Awaitable[None]] | None,
    ) -> LLMResponse:
        attempt = 0
        delays = list(self._CHAT_RETRY_DELAYS)
        persistent = retry_mode == "persistent"
        last_response: LLMResponse | None = None
        last_error_key: str | None = None
        identical_error_count = 0

        while True:
            attempt += 1
            response = await call(**kw)
            if response.finish_reason != "error":
                return response

            last_response = response
            error_key = (response.content or "").strip().lower() or None
            if error_key and error_key == last_error_key:
                identical_error_count += 1
            else:
                last_error_key = error_key
                identical_error_count = 1 if error_key else 0

            if not self._is_transient_error(response.content):
                return response

            if persistent and identical_error_count >= self._PERSISTENT_IDENTICAL_ERROR_LIMIT:
                _console.print(
                    f"[dim yellow]  Stopped persistent retry after "
                    f"{identical_error_count} identical errors[/dim yellow]"
                )
                return response

            if not persistent and attempt > len(delays):
                _console.print(
                    f"[dim yellow]  LLM request failed after {attempt} retries[/dim yellow]"
                )
                break

            base_delay = delays[min(attempt - 1, len(delays) - 1)]
            delay = self._extract_retry_after(response.content) or base_delay
            if persistent:
                delay = min(delay, self._PERSISTENT_MAX_DELAY)

            if on_retry_wait:
                await on_retry_wait(
                    f"Model request failed, retrying in {int(round(delay))}s "
                    f"(attempt {attempt})."
                )

            remaining = max(0.0, delay)
            while remaining > 0:
                chunk = min(remaining, self._RETRY_HEARTBEAT_CHUNK)
                await asyncio.sleep(chunk)
                remaining -= chunk

        return last_response if last_response is not None else await call(**kw)
