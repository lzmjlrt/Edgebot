"""
edgebot/agent/memory.py - Memory store and two-phase Dream consolidation.

Phase 1: LLM analyzes conversation history + archived entries, extracts
         structured facts tagged [USER|SOUL|MEMORY].

Phase 2: AgentRunner with read_file / edit_file tools performs targeted,
         incremental edits to USER.md, SOUL.md, MEMORY.md — instead of
         fragile text-parsing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Iterator

from rich.console import Console

from edgebot.config import MEMORY_DIR, MODEL, RUNTIME_DIR, SOUL_MD_PATH, USER_MD_PATH, WORKDIR
from edgebot.providers.base import LLMProvider
from edgebot.tools.base import BaseTool
from edgebot.utils.gitstore import GitStore

_console = Console()

MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
HISTORY_FILE = MEMORY_DIR / "history.jsonl"
CURSOR_FILE = MEMORY_DIR / ".cursor"
DREAM_CURSOR_FILE = MEMORY_DIR / ".dream_cursor"

_MAX_MESSAGES = 30
_MAX_ARCHIVED_BATCH = 20
_MEMORY_FILE_MAX_CHARS = 32_000
_SOUL_FILE_MAX_CHARS = 16_000
_USER_FILE_MAX_CHARS = 16_000
_HISTORY_ENTRY_PREVIEW_MAX_CHARS = 4_000
_CONVERSATION_MAX_CHARS = 48_000
_HISTORY_ENTRY_HARD_CAP = 64_000
_STALE_THRESHOLD_DAYS = 14

PHASE1_PROMPT = """\
You have TWO equally important tasks:
1. Extract new facts from conversation history
2. Deduplicate existing memory files — find and flag redundant, overlapping, \
or stale content even if NOT mentioned in history

Output one line per finding:
[FILE] atomic fact              (FILE = USER, SOUL, or MEMORY)
[FILE-REMOVE] content to remove, reason why

Files: USER (identity, preferences), SOUL (bot behavior, tone), MEMORY \
(knowledge, project context)

## Task 1 — New fact extraction
STRICT INCLUSION CRITERIA — a fact must meet ALL of:
1. Stable — not transient, one-off, or debugging noise
2. Non-obvious — not derivable from the code or context
3. User-validated — confirmed by the user (not guessed by the assistant)
4. Atomic — "prefers Chinese replies" NOT "discussed language preferences"
5. Absent from current memory files — re-read them and skip duplicates

REJECT AGGRESSIVELY:
- Debug sessions, transient errors, one-off questions
- Conversational filler ("hi", "thanks", "got it", "ok")
- Anything mentioned in passing without emphasis
- Vague summaries ("user asked about X")
- Code patterns or facts derivable from reading the codebase
- Anything already in USER.md / SOUL.md / MEMORY.md (even paraphrased)
- Assistant's own behavior unless user EXPLICITLY corrected it

Category rules:
[USER]   Only identity/preferences/habits the user stated with emphasis
[SOUL]   Only when user EXPLICITLY corrects the assistant's tone/style
[MEMORY] Only new architectural decisions, confirmed solutions, or long-lived \
         project facts

## Task 2 — Deduplication and staleness
Scan ALL memory files for these redundancy patterns:
- Same fact stated in multiple places (e.g., "communicates in Chinese" in both \
  USER.md and MEMORY.md)
- Overlapping or nested sections covering the same topic
- Information in MEMORY.md that is already captured in USER.md or SOUL.md
- Verbose entries that can be condensed without losing information
- Corrections: "location is Tokyo, not Osaka" → update USER.md

For each issue found, output [FILE-REMOVE] with the exact content to remove \
and why. Prefer keeping facts in their canonical location (USER.md for \
identity/preferences, SOUL.md for behavior, MEMORY.md for project knowledge).

Staleness rules:
- User habits/preferences/personality traits in USER.md are permanent — only \
  update with explicit corrections
- SOUL.md entries are permanent — only update with explicit corrections
- MEMORY.md lines may have an age suffix like "← 30d"; age means when the line \
  was last edited, not automatic deletion. Lines older than {stale_threshold_days} \
  days deserve closer review.
- MEMORY.md entries should be pruned if objectively outdated: passed events, \
  resolved issues, superseded approaches
- When uncertain whether to delete, keep but add "(verify currency)"

If nothing qualifies: [SKIP] no high-value information

## Current USER.md
{user_content}

## Current SOUL.md
{soul_content}

## Current MEMORY.md
{memory_content}

## Recent Conversation
{conversation}
"""

PHASE2_SYSTEM_PROMPT = """\
You are a memory maintenance agent. Your job is to update long-term memory
files based on the analysis provided.

You have access to read_file and edit_file tools. Follow this workflow:

1. Read the current contents of USER.md, SOUL.md, and MEMORY.md
2. For each entry in the analysis:
   - [FILE] entries: check if already present (exact or paraphrased). \
If new, append to the correct file.
   - [FILE-REMOVE] entries: find the matching content and delete it using \
edit_file (replace with empty string).
3. Rules:
   - For USER.md: treat "- Key: value" lines as upserts (update if key exists)
   - For SOUL.md and MEMORY.md: append new content, delete flagged content
   - When deleting: include surrounding context (blank lines, section header) \
in old_text to ensure unique match
   - Keep entries as concise bullet points
   - Never duplicate information already present
   - Surgical edits only — never rewrite entire files
   - If nothing to update, do nothing and respond with "No updates needed."

Files are located at:
- USER.md:   {user_path}
- SOUL.md:   {soul_path}
- MEMORY.md: {memory_path}
"""


# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------

class MemoryStore:
    """Pure file I/O for Edgebot memory files."""

    _append_locks: ClassVar[dict[Path, threading.Lock]] = {}
    _append_locks_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, workspace: Path, *, memory_dir: Path | None = None):
        self.workspace = workspace
        self.memory_dir = Path(memory_dir) if memory_dir is not None else MEMORY_DIR
        runtime_dir = self.memory_dir.parent
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.cursor_file = self.memory_dir / ".cursor"
        self.dream_cursor_file = self.memory_dir / ".dream_cursor"
        self.soul_file = runtime_dir / "SOUL.md" if memory_dir is not None else SOUL_MD_PATH
        self.user_file = runtime_dir / "USER.md" if memory_dir is not None else USER_MD_PATH
        lock_key = self.memory_dir.resolve()
        with self._append_locks_guard:
            self._append_lock = self._append_locks.setdefault(lock_key, threading.Lock())
        self.git = GitStore(
            runtime_dir if memory_dir is not None else RUNTIME_DIR,
            tracked_files=[
                "SOUL.md",
                "USER.md",
                "memory/MEMORY.md",
                "memory/.dream_cursor",
            ],
            allow_nested=True,
        )
        legacy_memory_dir = workspace / "memory"
        if not self.memory_dir.exists() and legacy_memory_dir.exists():
            shutil.copytree(legacy_memory_dir, self.memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def ensure_git_initialized(self) -> bool:
        """Initialize the Dream git store after templates have been seeded."""
        if self.git.is_initialized():
            return True
        return self.git.init()

    def read_memory(self) -> str:
        return _read_file(self.memory_file)

    def read_user(self) -> str:
        return _read_file(self.user_file)

    def read_soul(self) -> str:
        return _read_file(self.soul_file)

    def get_memory_context(self) -> str:
        content = self.read_memory().strip()
        return f"## Long-term Memory\n\n{content}" if content and content != "(empty)" else ""

    def _next_cursor(self) -> int:
        if self.cursor_file.exists():
            try:
                return int(self.cursor_file.read_text(encoding="utf-8").strip()) + 1
            except (OSError, ValueError):
                pass
        last = self._read_last_entry()
        if last and isinstance(last.get("cursor"), int):
            return last["cursor"] + 1
        return max((cursor for _entry, cursor in self._iter_valid_entries()), default=0) + 1

    def _read_last_entry(self) -> dict | None:
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [line for line in data.splitlines() if line.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    @staticmethod
    def _valid_cursor(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def append_history(
        self,
        content: str,
        *,
        max_chars: int | None = None,
        session_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self._append_lock:
            cursor = self._next_cursor()
            limit = max_chars if max_chars is not None else _HISTORY_ENTRY_HARD_CAP
            cleaned = content.strip()
            if len(cleaned) > limit:
                cleaned = _truncate_text(cleaned, limit)
            record = {
                "cursor": cursor,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "content": cleaned,
            }
            if session_key:
                record["session_key"] = session_key
            if metadata:
                record.update(metadata)
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self.cursor_file.write_text(str(cursor), encoding="utf-8")
            return cursor

    def _iter_valid_entries(self) -> Iterator[tuple[dict[str, Any], int]]:
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cursor = self._valid_cursor(entry.get("cursor"))
                    if cursor is None:
                        continue
                    yield entry, cursor
        except FileNotFoundError:
            return

    def read_unprocessed_history(
        self,
        since_cursor: int,
        *,
        session_key: str | None = None,
    ) -> list[dict]:
        entries = [
            entry
            for entry, cursor in self._iter_valid_entries()
            if cursor > since_cursor
        ]
        if session_key is None:
            return entries
        return [
            entry
            for entry in entries
            if entry.get("session_key") == session_key
        ]

    def get_last_dream_cursor(self) -> int:
        if self.dream_cursor_file.exists():
            try:
                return int(self.dream_cursor_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pass
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self.dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    _MAX_HISTORY_ENTRIES = 1000

    def compact_history(self) -> None:
        """Drop oldest entries if history.jsonl exceeds the cap."""
        if self._MAX_HISTORY_ENTRIES <= 0:
            return
        entries = [entry for entry, _cursor in self._iter_valid_entries()]
        if not entries:
            return
        if len(entries) <= self._MAX_HISTORY_ENTRIES:
            return
        kept = entries[-self._MAX_HISTORY_ENTRIES:]
        tmp_path = self.history_file.with_suffix(self.history_file.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            for entry in kept:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.history_file)
        with suppress(PermissionError, OSError):
            fd = os.open(str(self.history_file.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


_STORE = MemoryStore(WORKDIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_file(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "(empty)"


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n... (truncated)"
    return text[: max(0, max_chars - len(marker))] + marker


def _format_messages(messages: list[dict]) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if role == "tool":
            continue
        if not content:
            continue
        if isinstance(content, str):
            lines.append(f"[{role}] {_truncate_text(content, 500)}")
    return _truncate_text("\n".join(lines), _CONVERSATION_MAX_CHARS)


def _filter_dedup(analysis: str, existing_blob: str) -> str:
    """Drop Phase 1 lines substantially covered by existing memory.
    Pass through [FILE-REMOVE] and [SKIP] lines unconditionally.
    """
    existing_lower = existing_blob.lower()
    kept: list[str] = []
    for raw in analysis.splitlines():
        line = raw.strip()
        if not line:
            kept.append(raw)
            continue
        m = re.match(
            r"^\[(USER|SOUL|MEMORY|SKIP|(?:USER|SOUL|MEMORY)-REMOVE)\]\s*(.*)$",
            line, re.I,
        )
        if not m:
            kept.append(raw)
            continue
        tag = m.group(1).upper()
        content = m.group(2).lower()
        if tag == "SKIP" or tag.endswith("-REMOVE"):
            kept.append(raw)
            continue
        words = [w for w in re.findall(r"[a-z0-9_一-鿿]+", content) if len(w) > 1]
        if not words:
            kept.append(raw)
            continue
        hit = sum(1 for w in words if w in existing_lower)
        if hit / len(words) >= 0.7:
            continue
        kept.append(raw)
    return "\n".join(kept)


def _normalize_line(line: str) -> str:
    s = line.strip().lstrip("-*").strip()
    s = re.sub(r"\*\*|__|\*|_", "", s)
    s = re.sub(r"\s+", " ", s).lower()
    return s


def cleanup_memory_files_once() -> None:
    """One-shot cleanup for duplicates in USER.md / SOUL.md / MEMORY.md."""
    marker = MEMORY_DIR / ".memory_cleaned"
    if marker.exists():
        return
    _KV_RE = re.compile(r"^[\s\-*]*\*?\*?([A-Za-z][A-Za-z \w/]*?)\*?\*?\s*:\s*(.+)$")
    results: list[str] = []
    for fname, path in (
        ("USER.md", USER_MD_PATH),
        ("SOUL.md", SOUL_MD_PATH),
        ("MEMORY.md", MEMORY_FILE),
    ):
        if not path.exists():
            continue
        original = path.read_text(encoding="utf-8")
        if fname == "USER.md":
            kvs: dict[str, str] = {}
            rest: list[str] = []
            for ln in original.splitlines():
                m = _KV_RE.match(ln.strip())
                if m and m.group(2).strip():
                    kvs[_normalize_line(m.group(1))] = ln.rstrip()
                else:
                    rest.append(ln.rstrip())
            rebuilt = "\n".join(rest).rstrip() + ("\n\n" + "\n".join(kvs.values()) if kvs else "") + "\n"
        else:
            seen: set[str] = set()
            kept: list[str] = []
            for ln in original.splitlines():
                n = _normalize_line(ln)
                if n and n in seen:
                    continue
                if n:
                    seen.add(n)
                kept.append(ln)
            rebuilt = "\n".join(kept).rstrip() + "\n"
        if rebuilt != original:
            path.write_text(rebuilt, encoding="utf-8")
            results.append(fname)
    try:
        marker.write_text("cleaned\n", encoding="utf-8")
    except Exception:
        pass
    if results:
        _console.print(
            f"[dim]  [memory] cleaned duplicates in {', '.join(results)}[/dim]"
        )


# ---------------------------------------------------------------------------
# Dream processor
# ---------------------------------------------------------------------------

class DreamProcessor:
    """Two-phase memory processor using the provider abstraction.

    Phase 1: LLM analyzes conversation + archived history → structured facts.
    Phase 2: AgentRunner with read_file / edit_file tools makes targeted edits.
    """

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        *,
        model: str = MODEL,
        max_live_messages: int = _MAX_MESSAGES,
        max_archived_batch: int = _MAX_ARCHIVED_BATCH,
        emit_output: bool = True,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.max_live_messages = max_live_messages
        self.max_archived_batch = max_archived_batch
        self.emit_output = emit_output

    # ---- input preparation ----

    def _select_archived_batch(self) -> list[dict]:
        entries = self.store.read_unprocessed_history(
            self.store.get_last_dream_cursor()
        )
        return entries[:self.max_archived_batch]

    def _select_live_messages(self, messages: list[dict]) -> list[dict]:
        return messages[-self.max_live_messages:]

    def _build_conversation_context(
        self,
        archived_batch: list[dict],
        live_messages: list[dict],
    ) -> str:
        archived_history = "\n".join(
            f"[{entry['timestamp']}] "
            f"{_truncate_text(str(entry.get('content', '')), _HISTORY_ENTRY_PREVIEW_MAX_CHARS)}"
            for entry in archived_batch
        )
        recent_conversation = _format_messages(live_messages)
        parts: list[str] = []
        if archived_history:
            parts.append(f"## Archived History\n{archived_history}")
        if recent_conversation:
            parts.append(f"## Live Conversation\n{recent_conversation}")
        return _truncate_text("\n\n".join(parts), _CONVERSATION_MAX_CHARS)

    def _annotate_memory_with_ages(self, content: str) -> str:
        """Append per-line git age hints for stale-memory review."""
        try:
            ages = self.store.git.line_ages("memory/MEMORY.md")
        except Exception:
            return content
        if not ages:
            return content

        had_trailing = content.endswith("\n")
        lines = content.splitlines()
        if len(lines) != len(ages):
            return content

        annotated: list[str] = []
        for line, age in zip(lines, ages):
            if line.strip() and age.age_days > _STALE_THRESHOLD_DAYS:
                annotated.append(f"{line}  ← {age.age_days}d")
            else:
                annotated.append(line)
        result = "\n".join(annotated)
        if had_trailing:
            result += "\n"
        return result

    # ---- Phase 1: analysis (plain LLM call, no tools) ----

    async def _phase1_analyze(
        self,
        conversation: str,
        user_content: str,
        soul_content: str,
        memory_content: str,
    ) -> str | None:
        prompt = PHASE1_PROMPT.format(
            user_content=user_content,
            soul_content=soul_content,
            memory_content=memory_content,
            conversation=conversation,
            stale_threshold_days=_STALE_THRESHOLD_DAYS,
        )
        try:
            response = await self.provider.chat_with_retry(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                model=self.model,
                max_tokens=2000,
                temperature=0.3,
            )
            if response.finish_reason == "error":
                return None
            return response.content or ""
        except Exception as exc:
            if self.emit_output:
                _console.print(f"[dim red]  [memory] phase 1 failed: {exc}[/dim red]")
            return None

    # ---- Phase 2: agent-runner with read_file / edit_file ----

    @staticmethod
    def _strip_age_suffix(content: str) -> str:
        return re.sub(r"\s+← \d+d(?=\n|$)", "", content)

    async def _phase2_execute(
        self,
        analysis: str,
        user_content: str,
        soul_content: str,
        memory_content: str,
    ) -> list[dict[str, str]]:
        """Run Phase 2 via AgentRunner with read_file and edit_file tools."""
        from edgebot.agent.runner import AgentRunner, AgentRunSpec

        # Build a minimal tool set for the dream agent
        tools, handlers = self._build_dream_tools()

        system_prompt = PHASE2_SYSTEM_PROMPT.format(
            user_path=str(self.store.user_file),
            soul_path=str(self.store.soul_file),
            memory_path=str(self.store.memory_file),
        )
        user_prompt = (
            f"## Analysis Result\n{analysis}\n\n"
            f"## Current File Contents\n\n"
            f"### USER.md\n{user_content}\n\n"
            f"### SOUL.md\n{soul_content}\n\n"
            f"### MEMORY.md\n{self._strip_age_suffix(memory_content)}"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        runner = AgentRunner(self.provider)
        result = await runner.run(AgentRunSpec(
            initial_messages=messages,
            provider=self.provider,
            tools=tools,
            tool_handlers=handlers,
            model=self.model,
            max_iterations=15,
            max_tokens=4000,
            max_tool_result_chars=16_000,
            emit_output=self.emit_output,
            assistant_label="Dream",
        ))
        if result.stop_reason not in ("completed",):
            raise RuntimeError(f"Dream phase 2 stopped: {result.stop_reason}")

        call_names: dict[str, str] = {}
        changelog: list[dict[str, str]] = []
        for msg in result.messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    func = tc.get("function") or {}
                    if tc.get("id") and isinstance(func, dict):
                        call_names[str(tc["id"])] = str(func.get("name") or "")
            if msg.get("role") != "tool":
                continue
            name = call_names.get(str(msg.get("tool_call_id") or ""))
            content = str(msg.get("content") or "")
            if name in {"edit_file", "write_file"} and content.startswith("Successfully"):
                changelog.append({"name": name, "status": "ok", "detail": content[:200]})
        return changelog

    def _build_dream_tools(self) -> tuple[list[dict], dict[str, Any]]:
        """Build read_file + edit_file tools scoped to the memory workspace."""

        tools: list[dict] = []
        handlers: dict[str, Any] = {}

        read_tool = _DreamReadTool(self.store.workspace)
        edit_tool = _DreamEditTool(
            self.store.workspace,
            allowed_files=(
                self.store.user_file,
                self.store.soul_file,
                self.store.memory_file,
            ),
        )

        tools.append(read_tool.to_openai())
        handlers[read_tool.name] = read_tool.execute
        tools.append(edit_tool.to_openai())
        handlers[edit_tool.name] = edit_tool.execute

        return tools, handlers

    # ---- cursor management ----

    def _advance_cursor(self, archived_batch: list[dict]) -> None:
        if archived_batch:
            self.store.set_last_dream_cursor(archived_batch[-1]["cursor"])
            self.store.compact_history()

    # ---- main entry ----

    async def run(self, messages: list[dict]) -> bool:
        """Run one Dream cycle. Returns True if memory files changed."""
        live_messages = self._select_live_messages(messages)
        archived_batch = self._select_archived_batch()

        substantive = [
            m for m in live_messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        has_signal = bool(archived_batch) or len(substantive) >= 6
        if not has_signal:
            return False

        conversation = self._build_conversation_context(archived_batch, live_messages)
        if not conversation.strip():
            return False

        self.store.ensure_git_initialized()
        raw_user_content = self.store.read_user()
        raw_soul_content = self.store.read_soul()
        raw_memory_content = self.store.read_memory()
        user_content = _truncate_text(raw_user_content, _USER_FILE_MAX_CHARS)
        soul_content = _truncate_text(raw_soul_content, _SOUL_FILE_MAX_CHARS)
        memory_content = _truncate_text(
            self._annotate_memory_with_ages(raw_memory_content),
            _MEMORY_FILE_MAX_CHARS,
        )

        # Phase 1: extract structured facts
        analysis = await self._phase1_analyze(
            conversation, user_content, soul_content, memory_content,
        )
        if analysis is None:
            return False
        if "[SKIP]" in analysis or not analysis.strip():
            self._advance_cursor(archived_batch)
            return False

        # Dedup against existing content
        existing_blob = "\n".join([raw_user_content, raw_soul_content, raw_memory_content])
        filtered = _filter_dedup(analysis, existing_blob)
        if not any(
            re.match(r"^\s*\[(USER|SOUL|MEMORY)(?:-REMOVE)?\]", line)
            for line in filtered.splitlines()
        ):
            self._advance_cursor(archived_batch)
            return False

        # Phase 2: agent edits files via tools
        try:
            changelog = await self._phase2_execute(
                filtered, user_content, soul_content, memory_content,
            )
        except Exception as exc:
            if self.emit_output:
                _console.print(f"[dim red]  [memory] phase 2 failed: {exc}[/dim red]")
            return False

        if changelog:
            ts = archived_batch[-1]["timestamp"] if archived_batch else datetime.now().strftime("%Y-%m-%d %H:%M")
            commit_msg = f"dream: {ts}, {len(changelog)} change(s)\n\n{filtered.strip()}"
            self._advance_cursor(archived_batch)
            self.store.git.auto_commit(commit_msg)
            if self.emit_output:
                files = [ev["name"] for ev in changelog]
                _console.print(
                    f"[dim]  [memory] Dream updated: {', '.join(files)}[/dim]"
                )
            return True
        self._advance_cursor(archived_batch)
        return False


# ---------------------------------------------------------------------------
# Dream-scoped tools (read/edit restricted to workspace memory files)
# ---------------------------------------------------------------------------

def _normalized_path_key(path: Path) -> str:
    key = os.path.abspath(path)
    if os.name == "nt":
        key = os.path.normcase(key)
    return key


class _DreamReadTool(BaseTool):
    """read_file scoped to the workspace for Dream agent."""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read file contents. Use this to check current memory file contents."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file to read."},
            },
            "required": ["path"],
        }

    def is_read_only(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def __init__(self, workspace: Path):
        self._workspace = workspace

    def execute(self, **kwargs: Any) -> Any:
        from edgebot.tools.filesystem import run_read
        return run_read(kwargs["path"], kwargs.get("limit"), kwargs.get("offset", 1))


class _DreamEditTool(BaseTool):
    """edit_file scoped to workspace memory files for Dream agent."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Replace exact text in a file. Use this to update USER.md, "
            "SOUL.md, or MEMORY.md with new information."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to edit."},
                "old_text": {"type": "string", "description": "Exact text to find."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
        }

    def __init__(
        self,
        workspace: Path,
        *,
        allowed_files: tuple[Path, ...] | None = None,
    ):
        self._workspace = workspace
        runtime_dir = workspace / ".edgebot"
        files = allowed_files or (
            runtime_dir / "USER.md",
            runtime_dir / "SOUL.md",
            runtime_dir / "memory" / "MEMORY.md",
        )
        self._allowed_files = {_normalized_path_key(path) for path in files}

    def execute(self, **kwargs: Any) -> Any:
        from edgebot.tools.base import safe_path
        from edgebot.tools.filesystem import run_edit

        try:
            target = safe_path(kwargs["path"])
        except Exception as exc:
            return f"Error: {exc}"
        if _normalized_path_key(target) not in self._allowed_files:
            return (
                "Error: Dream edit_file may only update USER.md, SOUL.md, "
                "or memory/MEMORY.md."
            )
        return run_edit(
            kwargs["path"], kwargs["old_text"], kwargs["new_text"],
            kwargs.get("replace_all", False),
        )


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
