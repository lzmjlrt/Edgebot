"""
edgebot/cli/ui_state.py - Shared CLI singletons.

One Rich Console drives all CLI rendering (width detection, color system),
and one MemoryStore instance backs the /dream-* commands and cron memory
consolidation.
"""

from pathlib import Path

from rich.console import Console

from edgebot.agent.memory import MemoryStore

console = Console()
_MEMORY = MemoryStore(Path.cwd())
