"""
tests/test_multimodal_run_read.py - Tests for image handling in run_read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from edgebot import config as _config_module
import edgebot.tools.base as _tools_base_module
from edgebot.tools.filesystem import run_read


def _make_png(tmp_path: Path) -> Path:
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452"
            "000000010000000108060000001f15c4"
            "890000000d4944415408d7c530050000"
            "00000200443a3a980000000049454e44"
            "ae426082"
        )
    )
    return image_path


@pytest.fixture
def in_workspace(tmp_path: Path, monkeypatch):
    """Run run_read with WORKDIR set to tmp_path so relative paths are valid."""
    monkeypatch.setattr(_config_module, "WORKDIR", tmp_path)
    monkeypatch.setattr(_tools_base_module, "WORKDIR", tmp_path)
    yield tmp_path


def test_run_read_image_with_vision_model_returns_content_part(in_workspace):
    image_path = _make_png(in_workspace)
    result = run_read("pixel.png", model="openai/gpt-4o")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["type"] == "image_url"
    assert result[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_run_read_image_without_vision_model_returns_text_prompt(in_workspace):
    image_path = _make_png(in_workspace)
    result = run_read("pixel.png", model="deepseek/deepseek-chat")
    assert isinstance(result, str)
    assert "Image file" in result
    assert "vision-capable" in result


def test_run_read_image_without_model_returns_text_prompt(in_workspace):
    image_path = _make_png(in_workspace)
    result = run_read("pixel.png")
    assert isinstance(result, str)
    assert "vision-capable" in result


def test_run_read_text_file_unchanged(in_workspace):
    text_path = in_workspace / "hello.txt"
    text_path.write_text("hello world", encoding="utf-8")
    result = run_read("hello.txt")
    assert isinstance(result, str)
    assert "hello world" in result


def test_run_read_unknown_image_returns_error(in_workspace):
    result = run_read("nonexistent/image.png", model="openai/gpt-4o")
    assert isinstance(result, str)
    assert "File not found" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
