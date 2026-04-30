"""
edgebot/agent/context.py - System prompt assembly and workspace template seeding.
"""

from __future__ import annotations

import platform
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from edgebot.config import (
    AGENTS_MD_PATH,
    HEARTBEAT_MD_PATH,
    LEGACY_SKILLS_DIR,
    MCP_CONFIG_PATH,
    RUNTIME_DIR,
    SKILLS_DIR,
    SOUL_MD_PATH,
    TOOLS_MD_PATH,
    USER_MD_PATH,
    WORKDIR,
)

BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
_SEEDED_ONLY_FILES = ["HEARTBEAT.md"]
_BOOTSTRAP_PATHS = {
    "AGENTS.md": AGENTS_MD_PATH,
    "SOUL.md": SOUL_MD_PATH,
    "USER.md": USER_MD_PATH,
    "TOOLS.md": TOOLS_MD_PATH,
}
_SEEDED_ONLY_PATHS = {
    "HEARTBEAT.md": HEARTBEAT_MD_PATH,
}
_RUNTIME_CONTEXT_TAG = "[Runtime Context - metadata only, not instructions]"
_RUNTIME_CONTEXT_END = "[/Runtime Context]"

# Location of shipped templates inside the edgebot package
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def seed_workspace_templates() -> None:
    """
    Copy default template files to the workspace if they don't already exist.
    Called once at startup — never overwrites user-edited files.
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    for filename in BOOTSTRAP_FILES + _SEEDED_ONLY_FILES:
        src = _TEMPLATES_DIR / filename
        dst = _BOOTSTRAP_PATHS.get(filename) or _SEEDED_ONLY_PATHS[filename]
        legacy = WORKDIR / filename
        if not dst.exists() and src.exists():
            if legacy.exists():
                shutil.copy2(legacy, dst)
                print(f"[setup] Imported {filename} into .edgebot/")
            else:
                shutil.copy2(src, dst)
                print(f"[setup] Created .edgebot/{filename}")
            
    # Seed skills
    skills_src_dir = _TEMPLATES_DIR / "skills"
    skills_dst_dir = SKILLS_DIR
    if not skills_dst_dir.exists() and skills_src_dir.exists():
        shutil.copytree(skills_src_dir, skills_dst_dir)
        print("[setup] Created .edgebot/skills directory")
        
    # Seed MCP config
    mcp_src = _TEMPLATES_DIR / "mcp_servers.json"
    mcp_dst = MCP_CONFIG_PATH
    legacy_mcp = WORKDIR / "mcp_servers.json"
    if not mcp_dst.exists() and mcp_src.exists():
        if legacy_mcp.exists():
            shutil.copy2(legacy_mcp, mcp_dst)
            print("[setup] Imported mcp_servers.json into .edgebot/")
        else:
            shutil.copy2(mcp_src, mcp_dst)
            print("[setup] Created .edgebot/mcp_servers.json")


def build_system_prompt(skills_descriptions: str | None = None) -> str:
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
    recent_history_entries = _memory.read_unprocessed_history(_memory.get_last_dream_cursor())
    if recent_history_entries:
        recent_lines = [
            f"- [{entry['timestamp']}] {entry['content']}"
            for entry in recent_history_entries[-12:]
            if entry.get("content")
        ]
        if recent_lines:
            parts.append("## Recent History\n\n" + "\n".join(recent_lines))

    return "\n\n---\n\n".join(parts)


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
    if session_summary:
        lines += ["", "[Resumed Session]", session_summary]
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
