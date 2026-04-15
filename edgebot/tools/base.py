"""
edgebot/tools/base.py - Path safety utility.
"""

from pathlib import Path

from edgebot.config import WORKDIR


def safe_path(p: str) -> Path:
    """Resolve a path and ensure it stays inside WORKDIR."""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path
