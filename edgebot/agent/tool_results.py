"""Tool result normalization and storage helpers for agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

TOOL_RESULT_PREVIEW_CHARS = 1200


@dataclass(slots=True)
class ToolResultPolicy:
    """Controls how raw tool output becomes model-facing tool content."""

    max_chars: int = 16_000
    session_key: str = "default"
    root: Path | None = None


def prepare_tool_result_content(
    output: str,
    *,
    tool_name: str,
    tool_call_id: str,
    policy: ToolResultPolicy,
) -> str:
    """Offload large non-read_file tool outputs and return context content."""
    if tool_name != "read_file" and len(output) > policy.max_chars:
        path = (
            _tool_result_root(policy)
            / safe_session_dir_name(policy.session_key)
            / safe_tool_result_name(tool_call_id)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        preview = output[:TOOL_RESULT_PREVIEW_CHARS]
        return (
            "[Tool result offloaded]\n"
            f"Path: {path}\n"
            f"Original size: {len(output)} chars\n"
            f"Preview:\n{preview}"
        )

    if len(output) > policy.max_chars:
        return output[:policy.max_chars] + "\n...[truncated]"
    return output


def safe_session_dir_name(session_key: str) -> str:
    safe = "".join(
        ch if ch.isalnum() or ch in ("-", "_", ".") else "_"
        for ch in session_key
    )
    return safe.strip("._") or "default"


def safe_tool_result_name(tool_call_id: str) -> str:
    safe = "".join(
        ch if ch.isalnum() or ch in ("-", "_", ".") else "_"
        for ch in tool_call_id
    )
    return (safe.strip("._") or "tool_call") + ".txt"


def _tool_result_root(policy: ToolResultPolicy) -> Path:
    if policy.root is not None:
        return Path(policy.root)
    from edgebot.config import RUNTIME_DIR

    return RUNTIME_DIR / "tool-results"
