"""
tests/test_multimodal_enforce_role_alternation.py - Tests for merging list content.
"""

from __future__ import annotations

from edgebot.multimodal import ContentPart
from edgebot.providers.base import LLMProvider


def _msg(role, content):
    return {"role": role, "content": content}


def test_enforce_role_alternation_drops_trailing_bare_assistant():
    messages = [
        _msg("user", "hello"),
        _msg("assistant", "hi there"),
    ]
    result = LLMProvider.enforce_role_alternation(messages)
    # Trailing assistant messages without tool_calls are dropped by design.
    assert result == [_msg("user", "hello")]


def test_enforce_role_alternation_merge_two_text_user_messages():
    messages = [
        _msg("user", "first"),
        _msg("user", "second"),
    ]
    result = LLMProvider.enforce_role_alternation(messages)
    assert len(result) == 1
    assert result[0]["content"] == "first\n\nsecond"


def test_enforce_role_alternation_merge_two_list_user_messages():
    image: ContentPart = {"type": "image_url", "image_url": {"url": "data:1"}}
    text: ContentPart = {"type": "text", "text": "describe"}
    messages = [
        _msg("user", [image]),
        _msg("user", [text]),
    ]
    result = LLMProvider.enforce_role_alternation(messages)
    assert len(result) == 1
    assert result[0]["content"] == [image, text]


def test_enforce_role_alternation_merge_text_then_list_user_messages():
    image: ContentPart = {"type": "image_url", "image_url": {"url": "data:1"}}
    messages = [
        _msg("user", "look at this"),
        _msg("user", [image]),
    ]
    result = LLMProvider.enforce_role_alternation(messages)
    assert len(result) == 1
    assert result[0]["content"] == [
        {"type": "text", "text": "look at this"},
        image,
    ]


def test_enforce_role_alternation_merge_list_then_text_user_messages():
    image: ContentPart = {"type": "image_url", "image_url": {"url": "data:1"}}
    messages = [
        _msg("user", [image]),
        _msg("user", "what is it"),
    ]
    result = LLMProvider.enforce_role_alternation(messages)
    assert len(result) == 1
    assert result[0]["content"] == [
        image,
        {"type": "text", "text": "what is it"},
    ]


def test_enforce_role_alternation_different_roles_not_merged():
    image: ContentPart = {"type": "image_url", "image_url": {"url": "data:1"}}
    messages = [
        _msg("user", [image]),
        _msg("assistant", "I see."),
        _msg("user", "more"),
    ]
    result = LLMProvider.enforce_role_alternation(messages)
    assert len(result) == 3


def test_enforce_role_alternation_preserves_assistant_with_tool_calls():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
    ]
    result = LLMProvider.enforce_role_alternation(messages)
    assert len(result) == 2


def test_enforce_role_alternation_list_content_not_overwritten():
    """Regression: previously non-str content replaced the previous message."""
    image: ContentPart = {"type": "image_url", "image_url": {"url": "data:1"}}
    text: ContentPart = {"type": "text", "text": "text"}
    messages = [
        _msg("user", [image]),
        _msg("user", [text]),
    ]
    result = LLMProvider.enforce_role_alternation(messages)
    assert len(result) == 1
    assert result[0]["content"] == [image, text]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
