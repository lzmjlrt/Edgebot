"""
tests/test_multimodal_content.py - Tests for edgebot/multimodal.py helpers.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from edgebot.multimodal import (
    ContentPart,
    as_content_list,
    count_image_parts,
    estimate_content_tokens,
    extract_text_from_content,
    image_file_to_content_part,
    is_content_list,
    merge_content_parts,
    replace_images_with_placeholder,
)


def test_is_content_list_with_str():
    assert not is_content_list("hello")


def test_is_content_list_with_list():
    assert is_content_list([{"type": "text", "text": "hello"}])


def test_is_content_list_with_empty_list():
    assert not is_content_list([])


def test_as_content_list_str():
    assert as_content_list("hello") == [{"type": "text", "text": "hello"}]


def test_as_content_list_empty_str():
    assert as_content_list("") == []


def test_as_content_list_list():
    parts = [{"type": "text", "text": "hi"}]
    assert as_content_list(parts) is parts


def test_as_content_list_none():
    assert as_content_list(None) == []


def test_extract_text_from_str():
    assert extract_text_from_content("hello") == "hello"


def test_extract_text_from_list():
    content: list[ContentPart] = [
        {"type": "text", "text": "first"},
        {"type": "image_url", "image_url": {"url": "data:..."}},
        {"type": "text", "text": "second"},
    ]
    assert extract_text_from_content(content) == "first\nsecond"


def test_count_image_parts():
    content: list[ContentPart] = [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:1"}},
        {"type": "image_url", "image_url": {"url": "data:2"}},
    ]
    assert count_image_parts(content) == 2
    assert count_image_parts("text only") == 0


def test_merge_str_and_str():
    assert merge_content_parts("a", "b") == "a\n\nb"


def test_merge_str_and_list():
    result = merge_content_parts("hello", [{"type": "image_url", "image_url": {"url": "data:..."}}])
    assert result == [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:..."}},
    ]


def test_merge_list_and_str():
    result = merge_content_parts([{"type": "image_url", "image_url": {"url": "data:..."}}], "hello")
    assert result == [
        {"type": "image_url", "image_url": {"url": "data:..."}},
        {"type": "text", "text": "hello"},
    ]


def test_merge_list_and_list():
    a: list[ContentPart] = [{"type": "text", "text": "a"}]
    b: list[ContentPart] = [{"type": "text", "text": "b"}]
    assert merge_content_parts(a, b) == [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]


def test_merge_empty_str_with_list():
    assert merge_content_parts("", [{"type": "text", "text": "b"}]) == [{"type": "text", "text": "b"}]


def test_replace_images_with_placeholder():
    content: list[ContentPart] = [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]
    result = replace_images_with_placeholder(content)
    assert result == [
        {"type": "text", "text": "describe this"},
        {"type": "text", "text": "[Image omitted: current model does not support vision]"},
    ]


def test_replace_images_with_placeholder_str_is_unchanged():
    assert replace_images_with_placeholder("plain text") == "plain text"


def test_estimate_content_tokens_text():
    assert estimate_content_tokens("abcd") == 1


def test_estimate_content_tokens_empty():
    assert estimate_content_tokens("") == 0


def test_estimate_content_tokens_image_low():
    content: list[ContentPart] = [
        {"type": "image_url", "image_url": {"url": "data:...", "detail": "low"}},
    ]
    assert estimate_content_tokens(content) == 85


def test_estimate_content_tokens_image_high():
    content: list[ContentPart] = [
        {"type": "image_url", "image_url": {"url": "data:...", "detail": "high"}},
    ]
    assert estimate_content_tokens(content) == 1024


def test_estimate_content_tokens_image_auto():
    content: list[ContentPart] = [
        {"type": "image_url", "image_url": {"url": "data:...", "detail": "auto"}},
    ]
    assert estimate_content_tokens(content) == 1024


def test_image_file_to_content_part(tmp_path: Path):
    image_path = tmp_path / "pixel.png"
    # Minimal valid 1x1 PNG
    image_path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452"
            "000000010000000108060000001f15c4"
            "890000000d4944415408d7c530050000"
            "00000200443a3a980000000049454e44"
            "ae426082"
        )
    )
    part = image_file_to_content_part(image_path)
    assert part is not None
    assert part["type"] == "image_url"
    url = part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    decoded = base64.b64decode(url.split(",")[-1])
    assert decoded == image_path.read_bytes()


def test_image_file_to_content_part_missing():
    assert image_file_to_content_part("/nonexistent/image.png") is None


def test_image_file_to_content_part_non_image():
    assert image_file_to_content_part("/etc/passwd") is None


def test_image_file_to_content_part_directory(tmp_path: Path):
    assert image_file_to_content_part(tmp_path) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
