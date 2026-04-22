"""
edgebot/agent/context.py - System prompt assembly and workspace template seeding.
"""

import platform
import shutil
from pathlib import Path

from edgebot.config import MEMORY_DIR, SKILLS_DIR, WORKDIR
from edgebot.skills.loader import SkillLoader

BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
_SKILLS = SkillLoader(SKILLS_DIR)

# Location of shipped templates inside the edgebot package
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def seed_workspace_templates() -> None:
    """
    Copy default template files to the workspace if they don't already exist.
    Called once at startup — never overwrites user-edited files.
    """
    for filename in BOOTSTRAP_FILES:
        src = _TEMPLATES_DIR / filename
        dst = WORKDIR / filename
        if not dst.exists() and src.exists():
            shutil.copy2(src, dst)
            print(f"[setup] Created {filename}")
            
    # Seed skills
    skills_src_dir = _TEMPLATES_DIR / "skills"
    skills_dst_dir = WORKDIR / "skills"
    if not skills_dst_dir.exists() and skills_src_dir.exists():
        shutil.copytree(skills_src_dir, skills_dst_dir)
        print("[setup] Created sample skills directory")
        
    # Seed MCP config
    mcp_src = _TEMPLATES_DIR / "mcp_servers.json"
    mcp_dst = WORKDIR / "mcp_servers.json"
    if not mcp_dst.exists() and mcp_src.exists():
        shutil.copy2(mcp_src, mcp_dst)
        print("[setup] Created mcp_servers.json")


def build_system_prompt(skills_descriptions: str | None = None) -> str:
    """
    Assemble a rich system prompt from identity, workspace files, and skills.

    Loading order (separated by ---):
      1. Identity + Runtime info
      2. Bootstrap files (AGENTS.md, SOUL.md, USER.md, TOOLS.md from workspace)
      3. Active always-skills
      4. Skills summary
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

    # 2. Bootstrap files from workspace
    for filename in BOOTSTRAP_FILES:
        path = WORKDIR / filename
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
            except Exception:
                pass

    # 3. Long-term memory (memory/MEMORY.md)
    memory_file = MEMORY_DIR / "MEMORY.md"
    if memory_file.exists():
        try:
            content = memory_file.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"## Long-term Memory\n\n{content}")
        except Exception:
            pass

    # 4. Active always-skills
    _SKILLS.reload()
    always_skills = _SKILLS.get_always_skills()
    if always_skills:
        always_content = _SKILLS.load_skills_for_context(always_skills)
        if always_content:
            parts.append(f"## Active Skills\n\n{always_content}")

    # 5. Skills summary
    summary = skills_descriptions
    if summary is None:
        summary = _SKILLS.build_skills_summary(exclude=set(always_skills))
    if summary and summary != "(no skills)":
        parts.append(f"## Available Skills\n\n{summary}")

    return "\n\n---\n\n".join(parts)
