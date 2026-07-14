"""
edgebot/agent/context.py - System prompt assembly and runtime-context injection.

Workspace template seeding lives in edgebot/agent/workspace_setup.py and is
re-exported here for backward compatibility.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class SystemPromptSection:
    """One named system-prompt source with an independently enforced cap."""

    source: str
    content: str
    priority: int
    hard_cap_tokens: int
    reducible: bool = True

    @property
    def token_estimate(self) -> int:
        return _estimate_text_tokens(self.content)


_SECTION_TOKEN_CAPS = {
    "bootstrap": 4_000,
    "memory": 4_000,
    "always_skill": 4_000,
    "skills_summary": 2_000,
    "recent_history": 2_000,
    "session_summary": 3_000,
}


def _estimate_text_tokens(content: str) -> int:
    """Conservative, dependency-free estimate for prompt-section capping."""
    return max(1, (len(content) + 2) // 3)


def _bounded_section_content(section: SystemPromptSection) -> str:
    """Return content capped at its declared token budget with a visible marker."""
    content = section.content.strip()
    if not section.reducible or _estimate_text_tokens(content) <= section.hard_cap_tokens:
        return content

    marker = f"[{section.source} truncated to fit the request budget]"
    available_chars = max(0, section.hard_cap_tokens * 3 - len(marker) - 2)
    truncated = content[:available_chars].rstrip()
    return f"{truncated}\n\n{marker}" if truncated else marker


def render_system_prompt_sections(sections: list[SystemPromptSection]) -> str:
    """Render ordered sections after applying their individual hard caps."""
    return "\n\n---\n\n".join(
        content
        for section in sorted(sections, key=lambda item: item.priority)
        if (content := _bounded_section_content(section))
    )


def build_system_prompt_sections(
    skills_descriptions: str | None = None,
    session_summary: str | None = None,
    session_key: str | None = None,
) -> list[SystemPromptSection]:
    """Build ordered, bounded source sections for the system prompt."""
    return _build_system_prompt_sections(
        skills_descriptions=skills_descriptions,
        session_summary=session_summary,
        session_key=session_key,
    )


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
    return render_system_prompt_sections(build_system_prompt_sections(
        skills_descriptions=skills_descriptions,
        session_summary=session_summary,
        session_key=session_key,
    ))


def _build_system_prompt_sections(
    skills_descriptions: str | None = None,
    session_summary: str | None = None,
    session_key: str | None = None,
) -> list[SystemPromptSection]:
    """Implementation kept separate so the public builder remains concise."""
    sections: list[SystemPromptSection] = []

    # 1. Identity + Runtime
    runtime = (
        f"OS: {platform.system()} {platform.release()}, "
        f"Python: {platform.python_version()}"
    )
    sections.append(SystemPromptSection(
        source="Runtime identity",
        priority=0,
        hard_cap_tokens=2_000,
        reducible=False,
        content=(
        f"# Edgebot\n\n"
        f"You are Edgebot, a coding agent.\n\n"
        f"## Runtime\n{runtime}\n\n"
        f"## Workspace\n{WORKDIR}"
        ),
    ))

    # 2. Bootstrap files from Edgebot runtime directory
    for filename in BOOTSTRAP_FILES:
        path = _BOOTSTRAP_PATHS[filename]
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                sections.append(SystemPromptSection(
                    source=filename,
                    content=f"## {filename}\n\n{content}",
                    priority=10,
                    hard_cap_tokens=_SECTION_TOKEN_CAPS["bootstrap"],
                ))
            except Exception:
                pass

    # 3. Long-term memory (memory/MEMORY.md)
    from edgebot.agent.memory import _STORE as _memory

    memory_context = _memory.get_memory_context()
    if memory_context:
        sections.append(SystemPromptSection(
            source="Long-term Memory",
            content=memory_context,
            priority=20,
            hard_cap_tokens=_SECTION_TOKEN_CAPS["memory"],
        ))

    # 4. Active always-skills
    from edgebot.tools.registry import SKILLS as _skills

    _skills.reload()
    always_skills = _skills.get_always_skills()
    if always_skills:
        for skill_name in always_skills:
            always_content = _skills.load_skills_for_context([skill_name])
            if always_content:
                sections.append(SystemPromptSection(
                    source=f"Always skill: {skill_name}",
                    content=f"## Active Skill: {skill_name}\n\n{always_content}",
                    priority=30,
                    hard_cap_tokens=_SECTION_TOKEN_CAPS["always_skill"],
                ))

    # 5. Skills summary
    summary = skills_descriptions
    if summary is None:
        summary = _skills.build_skills_summary(exclude=set(always_skills))
    if summary and summary != "(no skills)":
        sections.append(SystemPromptSection(
            source="Available Skills",
            content=f"## Available Skills\n\n{summary}",
            priority=40,
            hard_cap_tokens=_SECTION_TOKEN_CAPS["skills_summary"],
        ))

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
            sections.append(SystemPromptSection(
                source="Recent History",
                content="## Recent History\n\n" + "\n".join(recent_lines),
                priority=50,
                hard_cap_tokens=_SECTION_TOKEN_CAPS["recent_history"],
            ))

    if isinstance(session_summary, str) and session_summary.strip():
        sections.append(SystemPromptSection(
            source="Session Summary",
            content=f"{_SESSION_SUMMARY_HEADING}\n\n{session_summary.strip()}",
            priority=60,
            hard_cap_tokens=_SECTION_TOKEN_CAPS["session_summary"],
        ))
    return sections


def inject_session_summary_into_system_prompt(
    system: str,
    session_summary: str | None,
) -> str:
    """Append persisted session continuity as trusted system context."""
    summary = session_summary.strip() if isinstance(session_summary, str) else ""
    if not summary:
        return system
    section = _bounded_section_content(SystemPromptSection(
        source="Session Summary",
        content=f"{_SESSION_SUMMARY_HEADING}\n\n{summary}",
        priority=60,
        hard_cap_tokens=_SECTION_TOKEN_CAPS["session_summary"],
    ))
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
