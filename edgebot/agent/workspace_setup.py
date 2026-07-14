"""
edgebot/agent/workspace_setup.py - Workspace template and config seeding.

One-shot startup setup: copies shipped templates (SOUL.md, AGENTS.md, USER.md,
TOOLS.md, HEARTBEAT.md, default skills, mcp_servers.json) into the per-workspace
runtime directory and creates a user-local config.env. Never overwrites user-edited
files.

Config values are imported lazily inside the functions (not at module import
time) so that callers which reload edgebot.config — e.g. tests that re-import
edgebot.agent.context after switching workspaces — always seed against the
current configuration.
"""

from __future__ import annotations

import shutil
from pathlib import Path

BOOTSTRAP_FILES = [ "SOUL.md","AGENTS.md", "USER.md", "TOOLS.md"]
_SEEDED_ONLY_FILES = ["HEARTBEAT.md"]

# Location of shipped templates inside the edgebot package
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _bootstrap_paths() -> dict[str, Path]:
    from edgebot.config import (
        AGENTS_MD_PATH,
        SOUL_MD_PATH,
        TOOLS_MD_PATH,
        USER_MD_PATH,
    )

    return {
        "AGENTS.md": AGENTS_MD_PATH,
        "SOUL.md": SOUL_MD_PATH,
        "USER.md": USER_MD_PATH,
        "TOOLS.md": TOOLS_MD_PATH,
    }


def _seeded_only_paths() -> dict[str, Path]:
    from edgebot.config import HEARTBEAT_MD_PATH

    return {
        "HEARTBEAT.md": HEARTBEAT_MD_PATH,
    }


def _seed_runtime_config() -> None:
    from edgebot.config import (
        API_BASE,
        GENERATION_TEMPERATURE,
        MODEL,
        RUNTIME_CONFIG_ENV,
    )

    if RUNTIME_CONFIG_ENV.exists():
        return
    lines = [
        "# Per-workspace Edgebot LLM settings.",
        "# Values here override the workspace .env on the next Edgebot start.",
        f"MODEL_ID={MODEL}",
        "# API_KEY=your-api-key-here",
    ]
    if API_BASE:
        lines.append(f"API_BASE={API_BASE}")
    else:
        lines.append("# API_BASE=https://api.example.com/v1")
    lines.extend([
        f"TEMPERATURE={GENERATION_TEMPERATURE}",
        "",
        "# Kimi K2.7 Code example:",
        "# MODEL_ID=moonshot/kimi-k2.7-code",
        "# API_BASE=https://api.moonshot.cn/v1",
        "# TEMPERATURE=1.0",
        "",
    ])
    RUNTIME_CONFIG_ENV.write_text("\n".join(lines), encoding="utf-8")
    print(f"[setup] Created runtime config: {RUNTIME_CONFIG_ENV}")


def seed_workspace_templates() -> None:
    """
    Copy default template files to user-level runtime state if absent.
    Called once at startup — never overwrites user-edited files.
    """
    from edgebot.config import MCP_CONFIG_PATH, RUNTIME_DIR, SKILLS_DIR, WORKDIR

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    _seed_runtime_config()

    bootstrap_paths = _bootstrap_paths()
    seeded_only_paths = _seeded_only_paths()
    for filename in BOOTSTRAP_FILES + _SEEDED_ONLY_FILES:
        src = _TEMPLATES_DIR / filename
        dst = bootstrap_paths.get(filename) or seeded_only_paths[filename]
        legacy = WORKDIR / filename
        if not dst.exists() and src.exists():
            if legacy.exists():
                shutil.copy2(legacy, dst)
                print(f"[setup] Imported {filename} into runtime state")
            else:
                shutil.copy2(src, dst)
                print(f"[setup] Created runtime file: {dst}")

    # Seed skills
    skills_src_dir = _TEMPLATES_DIR / "skills"
    skills_dst_dir = SKILLS_DIR
    if not skills_dst_dir.exists() and skills_src_dir.exists():
        shutil.copytree(skills_src_dir, skills_dst_dir)
        print(f"[setup] Created runtime skills directory: {skills_dst_dir}")

    # Seed MCP config
    mcp_src = _TEMPLATES_DIR / "mcp_servers.json"
    mcp_dst = MCP_CONFIG_PATH
    legacy_mcp = WORKDIR / "mcp_servers.json"
    if not mcp_dst.exists() and mcp_src.exists():
        if legacy_mcp.exists():
            shutil.copy2(legacy_mcp, mcp_dst)
            print("[setup] Imported mcp_servers.json into runtime state")
        else:
            shutil.copy2(mcp_src, mcp_dst)
            print(f"[setup] Created runtime MCP config: {mcp_dst}")
