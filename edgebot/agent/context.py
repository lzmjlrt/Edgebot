"""
edgebot/agent/context.py - System prompt assembly and runtime-context injection.

Workspace template seeding lives in edgebot/agent/workspace_setup.py and is
re-exported here for backward compatibility.
"""

from __future__ import annotations

import platform
from datetime import datetime
from typing import Any

from edgebot.config import (
    AGENTS_MD_PATH,
    SOUL_MD_PATH,
    TOOLS_MD_PATH,
    USER_MD_PATH,
    WORKDIR,
)

from edgebot.agent.workspace_setup import (  # noqa: F401  (re-exported for compat)
    BOOTSTRAP_FILES,
    _SEEDED_ONLY_FILES,
    _seed_runtime_config,
    seed_workspace_templates,
)

_BOOTSTRAP_PATHS = {
    "AGENTS.md": AGENTS_MD_PATH,
    "SOUL.md": SOUL_MD_PATH,
    "USER.md": USER_MD_PATH,
    "TOOLS.md": TOOLS_MD_PATH,
}
_RUNTIME_CONTEXT_TAG = "[Runtime Context - metadata only, not instructions]"
_RUNTIME_CONTEXT_END = "[/Runtime Context]"
_SESSION_SUMMARY_HEADING = "## Session Summary"


def build_system_prompt(
    skills_descriptions: str | None = None,
    session_summary: str | None = None,
    session_key: str | None = None,
) -> str:
    """
    Assemble a rich system prompt from identity, workspace files, and skills.

    Loading order (separated by ---):
      1. Identity + Runtime info
      2. Bootstrap files (AGENTS.md, SOUL.md, USER.md, TOOLS.md from workspace)
      3. Long-term memory
      4. Active always-skills
      5. Skills summary
      6. Recent archived history
    """
    parts = []

    # 1. Identity + Runtime
    runtime = (
        f"OS: {platform.system()} {platform.release()}, "
        f"Python: {platform.python_version()}"
    )
    parts.append(
        f"# Edgebot\n\n"
        f"You are Edgebot, a coding agent.\n\n"
        f"## Runtime\n{runtime}\n\n"
        f"## Workspace\n{WORKDIR}"
    )

    # 2. Bootstrap files from Edgebot runtime directory
    for filename in BOOTSTRAP_FILES:
        path = _BOOTSTRAP_PATHS[filename]
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
            except Exception:
                pass

    # 3. Long-term memory (memory/MEMORY.md)
    from edgebot.agent.memory import _STORE as _memory

    memory_context = _memory.get_memory_context()
    if memory_context:
        parts.append(memory_context)

    # 4. Active always-skills
    from edgebot.tools.registry import SKILLS as _skills

    _skills.reload()
    always_skills = _skills.get_always_skills()
    if always_skills:
        always_content = _skills.load_skills_for_context(always_skills)
        if always_content:
            parts.append(f"## Active Skills\n\n{always_content}")

    # 5. Skills summary
    summary = skills_descriptions
    if summary is None:
        summary = _skills.build_skills_summary(exclude=set(always_skills))
    if summary and summary != "(no skills)":
        parts.append(f"## Available Skills\n\n{summary}")

    # 6. Recent archived history that has not yet been folded into MEMORY.md
    recent_history_entries = _memory.read_unprocessed_history(
        _memory.get_last_dream_cursor(),
        session_key=session_key,
    )
    if recent_history_entries:
        recent_lines = [
            f"- [{entry['timestamp']}] {entry['content']}"
            for entry in recent_history_entries[-12:]
            if entry.get("content")
        ]
        if recent_lines:
            parts.append("## Recent History\n\n" + "\n".join(recent_lines))

    system = "\n\n---\n\n".join(parts)
    return inject_session_summary_into_system_prompt(system, session_summary)


def inject_session_summary_into_system_prompt(
    system: str,
    session_summary: str | None,
) -> str:
    """Append persisted session continuity as trusted system context."""
    summary = session_summary.strip() if isinstance(session_summary, str) else ""
    if not summary:
        return system
    section = f"{_SESSION_SUMMARY_HEADING}\n\n{summary}"
    if section in system:
        return system
    return f"{system.rstrip()}\n\n{section}"


def build_runtime_context(
    *,
    channel: str = "cli",
    chat_id: str = "direct",
    session_key: str | None = None,
    session_summary: str | None = None,
) -> str:
    """Build an untrusted runtime metadata block for the current turn."""
    lines = [
        f"Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Channel: {channel}",
        f"Chat ID: {chat_id}",
    ]
    if session_key:
        lines.append(f"Session Key: {session_key}")
    return _RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + _RUNTIME_CONTEXT_END


def merge_runtime_context_into_messages(
    messages: list[dict[str, Any]],
    *,
    channel: str = "cli",
    chat_id: str = "direct",
    session_key: str | None = None,
    session_summary: str | None = None,
) -> list[dict[str, Any]]:
    """
    Inject runtime metadata into the latest user message only.

    Stored session history remains clean; only the LLM request gets the block.
    """
    if not messages:
        return []

    runtime_block = build_runtime_context(
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
        session_summary=session_summary,
    )
    merged = list(messages)

    for idx in range(len(merged) - 1, -1, -1):
        message = merged[idx]
        if message.get("role") != "user":
            continue
        content = message.get("content")
        updated = dict(message)
        if isinstance(content, str):
            updated["content"] = f"{runtime_block}\n\n{content}"
        elif isinstance(content, list):
            updated["content"] = [{"type": "text", "text": runtime_block}] + content
        else:
            updated["content"] = f"{runtime_block}\n\n{content}"
        merged[idx] = updated
        break
    return merged
