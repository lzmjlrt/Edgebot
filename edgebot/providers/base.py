"""
edgebot/providers/base.py - Base LLM provider interface with retry logic.
"""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from rich.console import Console

_console = Console()
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_valid_tool_name(name: Any) -> bool:
    return isinstance(name, str) and _TOOL_NAME_RE.fullmatch(name) is not None


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]

    def has_valid_name(self) -> bool:
        return is_valid_tool_name(self.name)

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
    # Structured error metadata used by retry policy when finish_reason == "error".
    error_status_code: int | None = None
    error_kind: str | None = None  # e.g. "timeout", "connection", "context_length", "rate_limit"
    error_type: str | None = None  # provider-specific, e.g. "insufficient_quota"
    error_code: str | None = None  # provider-specific, e.g. "rate_limit_exceeded"
    error_retry_after_s: float | None = None
    error_should_retry: bool | None = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def should_execute_tools(self) -> bool:
        """Tools execute only when has_tool_calls AND finish_reason allows it.

        finish_reason ``length`` / ``content_filter`` / ``refusal`` / ``error``
        all indicate the tool_calls payload may be incomplete or invalid, so
        we refuse to execute them.
        """
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
        "429", "rate limit", "500", "502", "503", "504", "529",
        "overloaded", "timeout", "timed out", "connection",
        "server error", "temporarily unavailable", "速率限制",
    )
    _RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 529})
    _TRANSIENT_ERROR_KINDS = frozenset({
        "timeout", "connection", "overloaded", "rate_limit", "server_error",
    })
    # Hard non-retryable: caller must repair input or stop. These are surfaced
    # to the runner unchanged so it can take corrective action (e.g. compact
    # context on context_length, or fail fast on auth).
    _NON_RETRYABLE_ERROR_KINDS = frozenset({
        "context_length", "invalid_request", "auth", "authentication",
        "permission", "content_filter", "refusal",
    })
    # 429 sub-classification: quota/billing/balance is non-retryable; rate
    # limiting and overload are retryable with backoff.
    _NON_RETRYABLE_429_TOKENS = frozenset({
        "insufficient_quota", "quota_exceeded", "quota_exhausted",
        "billing_hard_limit_reached", "insufficient_balance",
        "credit_balance_too_low", "billing_not_active", "payment_required",
    })
    _RETRYABLE_429_TOKENS = frozenset({
        "rate_limit_exceeded", "rate_limit_error", "too_many_requests",
        "request_limit_exceeded", "requests_limit_exceeded", "overloaded_error",
    })
    _NON_RETRYABLE_429_TEXT_MARKERS = (
        "insufficient_quota", "insufficient quota",
        "quota exceeded", "quota exhausted",
        "billing hard limit", "billing not active",
        "insufficient balance", "credit balance too low",
        "payment required", "out of credits", "out of quota",
        "exceeded your current quota",
    )
    _RETRYABLE_429_TEXT_MARKERS = (
        "rate limit", "rate_limit", "too many requests",
        "retry after", "try again in", "temporarily unavailable",
        "overloaded", "concurrency limit", "速率限制",
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

    @staticmethod
    def _normalize_error_token(value: Any) -> str | None:
        if value is None:
            return None
        token = str(value).strip().lower()
        return token or None

    @classmethod
    def _extract_error_type_code(cls, payload: Any) -> tuple[str | None, str | None]:
        """Pull `type` / `code` out of an OpenAI-style error JSON payload."""
        data: dict[str, Any] | None = None
        if isinstance(payload, dict):
            data = payload
        elif isinstance(payload, str):
            text = payload.strip()
            if text:
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    data = parsed
        if not isinstance(data, dict):
            return None, None

        error_obj = data.get("error")
        type_value = data.get("type")
        code_value = data.get("code")
        if isinstance(error_obj, dict):
            type_value = error_obj.get("type") or type_value
            code_value = error_obj.get("code") or code_value

        return cls._normalize_error_token(type_value), cls._normalize_error_token(code_value)

    @classmethod
    def _is_retryable_429_response(cls, response: LLMResponse) -> bool:
        """429 sub-classification: quota/billing → no-retry, rate-limit → retry."""
        type_token = cls._normalize_error_token(response.error_type)
        code_token = cls._normalize_error_token(response.error_code)
        semantic_tokens = {t for t in (type_token, code_token) if t is not None}

        if any(t in cls._NON_RETRYABLE_429_TOKENS for t in semantic_tokens):
            return False

        content = (response.content or "").lower()
        if any(m in content for m in cls._NON_RETRYABLE_429_TEXT_MARKERS):
            return False

        if any(t in cls._RETRYABLE_429_TOKENS for t in semantic_tokens):
            return True
        if any(m in content for m in cls._RETRYABLE_429_TEXT_MARKERS):
            return True
        # Unknown 429 defaults to WAIT+retry (rate-limit is the common case).
        return True

    @classmethod
    def _is_transient_response(cls, response: LLMResponse) -> bool:
        """Prefer structured error metadata over text grep when available.

        Returns True if the error should be retried with backoff. Hard errors
        like context_length / auth / content_filter return False so the caller
        can take corrective action.
        """
        if response.error_should_retry is not None:
            return bool(response.error_should_retry)

        kind = (response.error_kind or "").strip().lower()
        if kind in cls._NON_RETRYABLE_ERROR_KINDS:
            return False

        if response.error_status_code is not None:
            status = int(response.error_status_code)
            if status == 429:
                return cls._is_retryable_429_response(response)
            if status == 413:
                # Payload too large → caller should compact, not blind-retry.
                return False
            if status in cls._RETRYABLE_STATUS_CODES or status >= 500:
                return True
            if 400 <= status < 500:
                return False

        if kind in cls._TRANSIENT_ERROR_KINDS:
            return True

        return cls._is_transient_error(response.content)

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

    @classmethod
    def _extract_retry_after_from_headers(cls, headers: Any) -> float | None:
        """Parse Retry-After / retry-after-ms HTTP headers (numeric, ms, or HTTP-date)."""
        if not headers:
            return None

        def _header_value(name: str) -> Any:
            if hasattr(headers, "get"):
                value = headers.get(name) or headers.get(name.title())
                if value is not None:
                    return value
            if isinstance(headers, dict):
                for key, value in headers.items():
                    if isinstance(key, str) and key.lower() == name.lower():
                        return value
            return None

        with suppress(TypeError, ValueError):
            retry_ms = _header_value("retry-after-ms")
            if retry_ms is not None:
                value = float(retry_ms) / 1000.0
                if value > 0:
                    return value

        retry_after = _header_value("retry-after")
        if retry_after is None:
            return None
        text = str(retry_after).strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return cls._to_retry_seconds(float(text), "s")
        try:
            retry_at = parsedate_to_datetime(text)
        except Exception:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        remaining = (retry_at - datetime.now(retry_at.tzinfo)).total_seconds()
        return max(0.1, remaining)

    @classmethod
    def _extract_retry_after_from_response(cls, response: LLMResponse) -> float | None:
        """Pick the strongest retry-after signal: structured field → legacy field → text."""
        if response.error_retry_after_s is not None and response.error_retry_after_s > 0:
            return response.error_retry_after_s
        if response.retry_after is not None and response.retry_after > 0:
            return response.retry_after
        return cls._extract_retry_after(response.content)

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

            if not self._is_transient_response(response):
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
            delay = self._extract_retry_after_from_response(response) or base_delay
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
