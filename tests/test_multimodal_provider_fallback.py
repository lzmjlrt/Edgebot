"""
tests/test_multimodal_provider_fallback.py - Tests for vision fallback in LiteLLMProvider.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from edgebot.multimodal import ContentPart
from edgebot.providers.base import LLMProvider
from edgebot.providers.litellm_provider import LiteLLMProvider, _downgrade_images_if_needed
from edgebot.providers.base import GenerationSettings


def _image_msg():
    image: ContentPart = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,abc"},
    }
    return {"role": "user", "content": [image]}


def test_downgrade_images_if_needed_for_non_vision_model():
    messages = [_image_msg()]
    result = _downgrade_images_if_needed(messages, "deepseek/deepseek-chat")
    assert result[0]["content"] == [
        {"type": "text", "text": "[Image omitted: current model does not support vision]"},
    ]


def test_downgrade_images_if_needed_for_vision_model():
    messages = [_image_msg()]
    result = _downgrade_images_if_needed(messages, "openai/gpt-4o")
    assert result == messages


def test_downgrade_images_if_needed_unknown_model_defaults_safe():
    """Models we do not recognize as vision-capable should get the placeholder."""
    messages = [_image_msg()]
    result = _downgrade_images_if_needed(messages, "some/random-model")
    assert result[0]["content"] == [
        {"type": "text", "text": "[Image omitted: current model does not support vision]"},
    ]


def test_downgrade_images_if_needed_preserves_non_image_content():
    messages = [{"role": "user", "content": "plain text"}]
    result = _downgrade_images_if_needed(messages, "deepseek/deepseek-chat")
    assert result == messages


async def _noop_on_content_delta(delta: str) -> None:
    pass


def _mock_response():
    class Choice:
        def __init__(self):
            self.message = type("M", (), {"content": "mock reply", "tool_calls": None})()
            self.finish_reason = "stop"
    class Resp:
        def __init__(self):
            self.choices = [Choice()]
            self.usage = None
    return Resp()


def test_chat_downgrades_images_for_non_vision_model():
    provider = LiteLLMProvider(
        api_key="dummy",
        model="deepseek/deepseek-chat",
        generation=GenerationSettings(),
    )
    with patch("edgebot.providers.litellm_provider.litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock:
        asyncio.run(provider.chat(messages=[_image_msg()]))
        call_kwargs = mock.call_args.kwargs
        messages_sent = call_kwargs["messages"]
        assert len(messages_sent) == 1
        assert messages_sent[0]["content"] == [
            {"type": "text", "text": "[Image omitted: current model does not support vision]"},
        ]


def test_chat_keeps_images_for_vision_model():
    provider = LiteLLMProvider(
        api_key="dummy",
        model="openai/gpt-4o",
        generation=GenerationSettings(),
    )
    original = [_image_msg()]
    with patch("edgebot.providers.litellm_provider.litellm.acompletion", new=AsyncMock(return_value=_mock_response())) as mock:
        asyncio.run(provider.chat(messages=original))
        call_kwargs = mock.call_args.kwargs
        messages_sent = call_kwargs["messages"]
        assert len(messages_sent) == 1
        assert messages_sent[0]["content"][0]["type"] == "image_url"


def test_chat_stream_downgrades_images_for_non_vision_model():
    provider = LiteLLMProvider(
        api_key="dummy",
        model="deepseek/deepseek-chat",
        generation=GenerationSettings(),
    )
    with patch("edgebot.providers.litellm_provider.litellm.acompletion", new=AsyncMock(return_value=AsyncMock())) as mock:
        asyncio.run(provider.chat_stream(messages=[_image_msg()], on_content_delta=_noop_on_content_delta))
        call_kwargs = mock.call_args.kwargs
        messages_sent = call_kwargs["messages"]
        assert len(messages_sent) == 1
        assert messages_sent[0]["content"] == [
            {"type": "text", "text": "[Image omitted: current model does not support vision]"},
        ]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
