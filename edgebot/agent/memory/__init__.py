"""
edgebot/agent/memory - Memory store and two-phase Dream consolidation.

Package layout:
- store.py        MemoryStore (pure file I/O: MEMORY.md, history.jsonl, cursors)
- heuristics.py   text helpers, history filters, Phase 1 dedup, one-shot cleanup
- prompts.py      PHASE1_PROMPT / PHASE2_SYSTEM_PROMPT templates
- dream.py        DreamProcessor (two-phase consolidation orchestrator)
- dream_tools.py  sandboxed read/edit/write tools for the Dream agent

This __init__ preserves the historical `edgebot.agent.memory` module surface:
every symbol that used to live in the old single-file module is re-exported
here, and the module-level singletons (`_STORE`, `_DREAMS`) plus the public
API (`get_dream_processor`, `consolidate_memory`) are defined in this
namespace so existing monkeypatches on `edgebot.agent.memory` keep working.
"""

from __future__ import annotations

from pathlib import Path

from edgebot.config import WORKDIR

from edgebot.agent.memory.dream import (  # noqa: F401
    DreamProcessor,
    _MAX_ARCHIVED_BATCH,
    _MAX_MESSAGES,
    _MEMORY_FILE_MAX_CHARS,
    _SOUL_FILE_MAX_CHARS,
    _STALE_THRESHOLD_DAYS,
    _USER_FILE_MAX_CHARS,
)
from edgebot.agent.memory.dream_tools import (  # noqa: F401
    _DreamEditTool,
    _DreamReadTool,
    _DreamWriteTool,
    _is_allowed_skill_file,
    _normalized_path_key,
)
from edgebot.agent.memory.heuristics import (  # noqa: F401
    _CONVERSATION_MAX_CHARS,
    _HISTORY_ENTRY_PREVIEW_MAX_CHARS,
    _extract_actionable_findings,
    _filter_dedup,
    _format_history_entry_for_dream,
    _format_messages,
    _has_durable_history_signal,
    _history_tags,
    _is_dream_visible_history,
    _normalize_history_tags,
    _normalize_line,
    _read_file,
    _truncate_text,
    cleanup_memory_files_once,
)
from edgebot.agent.memory.prompts import (  # noqa: F401
    PHASE1_PROMPT,
    PHASE2_SYSTEM_PROMPT,
)
from edgebot.agent.memory.store import (  # noqa: F401
    CURSOR_FILE,
    DREAM_CURSOR_FILE,
    HISTORY_FILE,
    MEMORY_FILE,
    MemoryStore,
    _HISTORY_ENTRY_HARD_CAP,
    _SKILL_FILE_MAX_CHARS,
    _SKILLS_CONTEXT_MAX_CHARS,
)

_STORE = MemoryStore(WORKDIR)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_DREAMS: dict[tuple[Path, bool], DreamProcessor] = {}


def get_dream_processor(
    store: MemoryStore | None = None,
    *,
    emit_output: bool = True,
) -> DreamProcessor:
    """Return a cached DreamProcessor for the given store/workspace."""
    from edgebot.config import create_provider

    target_store = store or _STORE
    key = (target_store.workspace.resolve(), emit_output)
    processor = _DREAMS.get(key)
    if processor is None:
        processor = DreamProcessor(
            target_store,
            provider=create_provider(),
            emit_output=emit_output,
        )
        _DREAMS[key] = processor
    return processor


async def consolidate_memory(
    messages: list[dict],
    store: MemoryStore | None = None,
    *,
    emit_output: bool = True,
) -> bool:
    """Run one Dream consolidation cycle."""
    return await get_dream_processor(store, emit_output=emit_output).run(messages)
