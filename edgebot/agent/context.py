"""
edgebot/agent/context.py - System prompt assembly and workspace template seeding.
"""

import platform
import shutil
from pathlib import Path

from edgebot.config import MEMORY_DIR, WORKDIR

BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]

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


def build_system_prompt(skills_descriptions: str) -> str:
    """
    Assemble a rich system prompt from identity, workspace files, and skills.

    Loading order (separated by ---):
      1. Identity + Runtime info
      2. Bootstrap files (AGENTS.md, SOUL.md, USER.md, TOOLS.md from workspace)
      3. Skills summary
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

    # 4. Skills
    if skills_descriptions and skills_descriptions != "(no skills)":
        parts.append(f"## Available Skills\n\n{skills_descriptions}")

    return "\n\n---\n\n".join(parts)
