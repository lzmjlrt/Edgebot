"""
edgebot/providers/litellm_provider.py - LiteLLM-based provider implementation.

Wraps litellm.acompletion (non-streaming and streaming) behind the
LLMProvider interface so the rest of Edgebot never touches litellm directly.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import json_repair
import litellm
from litellm import exceptions as _litellm_exc

litellm.suppress_debug_info = True

from edgebot.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest


class LiteLLMProvider(LLMProvider):
    """LLMProvider backed by LiteLLM (supports any OpenAI-compatible API)."""

    def __init__(
        self,
        api_key: str,
        model: str,
        api_base: str | None = None,
        generation: GenerationSettings | None = None,
    ):
        super().__init__(api_key=api_key, api_base=api_base)
        self._model = model
        if generation is not None:
            self.generation = generation

    def get_default_model(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.7,
    ) -> LLMResponse:
        safe_messages = self.enforce_role_alternation(messages)
        try:
            request_model = model or self._model
            resp = await litellm.acompletion(
                model=request_model,
                messages=safe_messages,
                tools=tools,
                max_tokens=max_tokens,
                temperature=_effective_temperature(request_model, temperature),
                api_key=self.api_key,
                api_base=self.api_base,
            )
        except Exception as exc:
            return self._handle_error(exc)
        return self._parse_sync_response(resp)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.7,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        safe_messages = self.enforce_role_alternation(messages)
        try:
            request_model = model or self._model
            stream = await litellm.acompletion(
                model=request_model,
                messages=safe_messages,
                tools=tools,
                max_tokens=max_tokens,
                temperature=_effective_temperature(request_model, temperature),
                api_key=self.api_key,
                api_base=self.api_base,
                stream=True,
            )
        except Exception as exc:
            return self._handle_error(exc)

        content_parts: list[str] = []
        tool_calls_buf: dict[int, dict] = {}
        # None until provider tells us; we infer at end if still missing.
        finish_reason: str | None = None

        try:
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
        except Exception as exc:
            # Stream-side error mid-flight: surface it as a structured error
            # so retry policy can classify it (502/503/timeout/etc.).
            return self._handle_error(exc)

        # Infer finish_reason if the provider never sent one.
        if finish_reason is None:
            if tool_calls_buf and not _all_tool_args_complete(tool_calls_buf):
                # Incomplete JSON in tool_call.arguments → almost certainly truncated.
                finish_reason = "length"
            elif tool_calls_buf:
                finish_reason = "tool_calls"
            else:
                finish_reason = "stop"

        # If finish_reason is "length" but we have tool_call buffers, the args
        # JSON is likely truncated — json_repair handles partial JSON gracefully.
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

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    @classmethod
    def _handle_error(cls, exc: Exception) -> LLMResponse:
        """Convert a litellm/httpx exception into a structured error LLMResponse."""
        meta = cls._extract_error_metadata(exc)
        body = (
            getattr(exc, "body", None)
            or getattr(exc, "doc", None)
            or getattr(getattr(exc, "response", None), "text", None)
        )
        body_text = body if isinstance(body, str) else (str(body) if body is not None else "")
        if body_text.strip():
            content = f"Error: {body_text.strip()[:500]}"
        else:
            content = f"Error calling LLM: {exc}"

        retry_after = meta.get("error_retry_after_s")
        if retry_after is None:
            retry_after = cls._extract_retry_after(content)

        return LLMResponse(
            content=content,
            finish_reason="error",
            retry_after=retry_after,
            **meta,
        )

    @classmethod
    def _extract_error_metadata(cls, exc: Exception) -> dict[str, Any]:
        """Pull status_code / kind / type / code / retry-after / should-retry from an exception."""
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)

        # Body for type/code extraction (OpenAI-style error JSON).
        payload: Any = (
            getattr(exc, "body", None)
            or getattr(exc, "doc", None)
            or getattr(response, "text", None)
        )
        if payload is None and response is not None:
            response_json = getattr(response, "json", None)
            if callable(response_json):
                try:
                    payload = response_json()
                except Exception:
                    payload = None
        error_type, error_code = LLMProvider._extract_error_type_code(payload)

        status_code = getattr(exc, "status_code", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)

        # Honor x-should-retry hint if the provider sent one.
        should_retry: bool | None = None
        if headers is not None:
            try:
                raw = headers.get("x-should-retry")
            except Exception:
                raw = None
            if isinstance(raw, str):
                lowered = raw.strip().lower()
                if lowered == "true":
                    should_retry = True
                elif lowered == "false":
                    should_retry = False

        # Map litellm exception class → kind (so retry policy can short-circuit).
        kind = cls._classify_litellm_kind(exc)

        return {
            "error_status_code": int(status_code) if status_code is not None else None,
            "error_kind": kind,
            "error_type": error_type,
            "error_code": error_code,
            "error_retry_after_s": cls._extract_retry_after_from_headers(headers),
            "error_should_retry": should_retry,
        }

    @staticmethod
    def _classify_litellm_kind(exc: Exception) -> str | None:
        """Map a litellm exception class to one of our `error_kind` tokens."""
        if isinstance(exc, _litellm_exc.ContextWindowExceededError):
            return "context_length"
        if isinstance(exc, _litellm_exc.Timeout):
            return "timeout"
        if isinstance(exc, _litellm_exc.APIConnectionError):
            return "connection"
        if isinstance(exc, _litellm_exc.RateLimitError):
            return "rate_limit"
        if isinstance(
            exc,
            (
                _litellm_exc.ServiceUnavailableError,
                _litellm_exc.InternalServerError,
                _litellm_exc.BadGatewayError,
            ),
        ):
            return "server_error"
        if isinstance(exc, _litellm_exc.AuthenticationError):
            return "auth"
        if isinstance(exc, _litellm_exc.PermissionDeniedError):
            return "permission"
        if isinstance(exc, _litellm_exc.ContentPolicyViolationError):
            return "content_filter"
        if isinstance(
            exc,
            (
                _litellm_exc.BadRequestError,
                _litellm_exc.InvalidRequestError,
                _litellm_exc.UnprocessableEntityError,
                _litellm_exc.UnsupportedParamsError,
            ),
        ):
            return "invalid_request"
        # Fallback: sniff the class name (covers httpx.TimeoutException etc.).
        name = exc.__class__.__name__.lower()
        if "timeout" in name:
            return "timeout"
        if "connection" in name:
            return "connection"
        return None


def _all_tool_args_complete(buf: dict[int, dict]) -> bool:
    """True iff every accumulated tool_call's argument string is valid JSON."""
    for b in buf.values():
        raw = b.get("arguments") or ""
        if not raw:
            # No args yet — treat as complete (empty dict).
            continue
        try:
            json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return False
    return True


def _parse_json(raw: str) -> dict[str, Any]:
    """Parse tool-call arguments JSON, repairing truncated/malformed payloads."""
    if not raw:
        return {}
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        try:
            result = json_repair.loads(raw)
        except Exception:
            return {}
    return result if isinstance(result, dict) else {}


def _effective_temperature(model: str | None, temperature: float) -> float:
    """Normalize generation params for providers with fixed model constraints."""
    normalized = (model or "").lower()
    if normalized in {"moonshot/kimi-k2.7-code", "kimi-k2.7-code"}:
        return 1.0
    return temperature
