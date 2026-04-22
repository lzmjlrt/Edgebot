"""
edgebot/agent/memory.py - Memory store and two-phase memory consolidation.

Adds a structured middle history layer so compressed context can still feed
later long-term memory updates and prompt construction.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import litellm
from rich.console import Console

from edgebot.config import API_BASE, API_KEY, MODEL, WORKDIR

_console = Console()

MEMORY_DIR = WORKDIR / "memory"
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
HISTORY_FILE = MEMORY_DIR / "history.jsonl"
CURSOR_FILE = MEMORY_DIR / ".cursor"
DREAM_CURSOR_FILE = MEMORY_DIR / ".dream_cursor"

# How many recent messages to analyze per consolidation
_MAX_MESSAGES = 30
_MAX_ARCHIVED_BATCH = 20

PHASE1_PROMPT = """\
Extract ONLY high-value, stable information from the recent conversation.
Output one finding per line with the format:
[USER|SOUL|MEMORY] atomic fact

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
- Any key-value fact (e.g. "Technical Level: expert") whose key already has a value
  in USER.md — even if the value differs. Upsert is handled by code; don't re-emit

Category rules:
[USER]   Only identity/preferences/habits the user stated with emphasis
         (e.g. "I always use Python 3.12", "I prefer Chinese")
[SOUL]   Only when user EXPLICITLY corrects the assistant's tone/style
         (e.g. "please answer more briefly from now on")
[MEMORY] Only new architectural decisions, confirmed solutions, or long-lived
         project facts (e.g. "project uses LiteLLM, not direct SDKs")

If in doubt, SKIP. Polluting memory is worse than missing one fact.
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

PHASE2_PROMPT = """\
Based on the analysis below, output the exact edits needed.
For each edit, use this EXACT format (including the === markers):

===FILE: USER.md===
===ACTION: append===
- New fact to add

===FILE: SOUL.md===
===ACTION: append===
- New behavior note

===FILE: MEMORY.md===
===ACTION: append===
- New knowledge entry

Rules:
- ACTION can be: append (add lines to end of file) or skip (no changes)
- Only output files that actually need changes
- Preserve all existing correct content — only ADD new information
- Keep entries as concise bullet points
- If nothing to update, output only: [SKIP]

## Analysis
{phase1_output}

## Current File Contents

### USER.md
{user_content}

### SOUL.md
{soul_content}

### MEMORY.md
{memory_content}
"""


class MemoryStore:
    """Pure file I/O layer for Edgebot memory files."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory_dir = workspace / "memory"
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.cursor_file = self.memory_dir / ".cursor"
        self.dream_cursor_file = self.memory_dir / ".dream_cursor"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

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
        return 1

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

    def append_history(self, content: str) -> int:
        """Append summarized archived history and return its cursor."""
        cursor = self._next_cursor()
        record = {
            "cursor": cursor,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "content": content.strip(),
        }
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    def read_unprocessed_history(self, since_cursor: int) -> list[dict]:
        entries: list[dict] = []
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
                    if entry.get("cursor", 0) > since_cursor:
                        entries.append(entry)
        except FileNotFoundError:
            pass
        return entries

    def get_last_dream_cursor(self) -> int:
        if self.dream_cursor_file.exists():
            try:
                return int(self.dream_cursor_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pass
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self.dream_cursor_file.write_text(str(cursor), encoding="utf-8")


_STORE = MemoryStore(WORKDIR)


def _read_file(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "(empty)"


def _format_messages(messages: list[dict]) -> str:
    """Format messages into a readable conversation transcript."""
    lines = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if role == "tool":
            continue  # Skip tool results for brevity
        if not content:
            continue
        if isinstance(content, str):
            lines.append(f"[{role}] {content[:500]}")
    return "\n".join(lines)


def _filter_dedup(analysis: str, existing_blob: str) -> str:
    """
    Drop Phase 1 lines whose factual content is substantially covered by the
    existing memory blob (USER+SOUL+MEMORY concatenated).
    Uses a simple word-overlap heuristic.
    """
    existing_lower = existing_blob.lower()
    kept: list[str] = []
    for raw in analysis.splitlines():
        line = raw.strip()
        if not line:
            kept.append(raw)
            continue
        m = re.match(r"^\[(USER|SOUL|MEMORY|SKIP)\]\s*(.*)$", line, re.I)
        if not m:
            kept.append(raw)
            continue
        tag = m.group(1).upper()
        content = m.group(2).lower()
        if tag == "SKIP":
            kept.append(raw)
            continue
        words = [w for w in re.findall(r"[a-z0-9_]+", content) if len(w) > 3]
        if not words:
            kept.append(raw)
            continue
        hit = sum(1 for w in words if w in existing_lower)
        if hit / len(words) >= 0.7:
            continue  # too similar to existing — drop
        kept.append(raw)
    return "\n".join(kept)


def _parse_phase2(output: str) -> list[dict]:
    """
    Parse Phase 2 LLM output into edit operations.
    Returns [{file, action, content}].
    """
    edits = []
    blocks = re.split(r"===FILE:\s*(.+?)===", output)
    # blocks: ['preamble', 'USER.md', '\n===ACTION: append===\n- fact\n', 'SOUL.md', ...]
    i = 1
    while i < len(blocks) - 1:
        filename = blocks[i].strip()
        body = blocks[i + 1]
        action_match = re.search(r"===ACTION:\s*(\w+)===", body)
        action = action_match.group(1) if action_match else "skip"
        # Content is everything after the ACTION line
        content = re.sub(r"===ACTION:\s*\w+===\s*", "", body).strip()
        if action != "skip" and content:
            edits.append({"file": filename, "action": action, "content": content})
        i += 2
    return edits


def _normalize_line(line: str) -> str:
    """Normalize a bullet line for duplicate detection."""
    s = line.strip().lstrip("-*").strip()
    s = re.sub(r"\*\*|__|\*|_", "", s)
    s = re.sub(r"\s+", " ", s).lower()
    return s


_KV_RE = re.compile(r"^[\s\-*]*\*?\*?([A-Za-z][A-Za-z \w/]*?)\*?\*?\s*:\s*(.+)$")


def _parse_kv(text: str) -> tuple[dict, list[str]]:
    """
    Split *text* into key/value upserts (`Language: Chinese`) and everything else
    (headers, blanks, free prose). Returns ({normalized_key: original_line}, non_kv_lines_in_order).
    """
    kvs: dict[str, str] = {}
    rest: list[str] = []
    for line in text.splitlines():
        m = _KV_RE.match(line.strip())
        if m and m.group(2).strip():
            key = _normalize_line(m.group(1))
            # Real values always win over placeholder-only values like "(your name)".
            is_placeholder = m.group(2).strip().startswith("(")
            existing = kvs.get(key)
            if existing is None or not is_placeholder:
                # Latest real value wins; placeholders never overwrite an existing entry.
                kvs[key] = line.rstrip()
        else:
            rest.append(line.rstrip())
    return kvs, rest


def _apply_user_upsert(path, new_block: str) -> bool:
    """
    For USER.md: treat '- Key: value' lines as upserts; preserve non-KV content.
    Returns True if the file was rewritten.
    """
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    old_kvs, old_rest = _parse_kv(existing)
    new_kvs, _ = _parse_kv(new_block)
    if not new_kvs:
        return False
    merged = {**old_kvs, **new_kvs}  # new overrides
    rebuilt = "\n".join(old_rest).rstrip() + "\n\n" + "\n".join(merged.values()) + "\n"
    if rebuilt == existing:
        return False
    path.write_text(rebuilt, encoding="utf-8")
    return True


def _apply_line_dedup_append(path, new_block: str) -> bool:
    """For SOUL.md / MEMORY.md: append only lines not already present."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    existing_norm = {n for n in (_normalize_line(l) for l in existing.splitlines()) if n}
    kept: list[str] = []
    for raw in new_block.splitlines():
        n = _normalize_line(raw)
        if not n:
            kept.append(raw)
            continue
        if n in existing_norm:
            continue
        existing_norm.add(n)
        kept.append(raw)
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    if not kept:
        return False
    new_content = existing.rstrip() + "\n\n" + "\n".join(kept) + "\n"
    path.write_text(new_content, encoding="utf-8")
    return True


def _apply_edits(edits: list[dict]) -> set[str]:
    """Apply parsed edits to workspace files. Returns the set of file names updated."""
    file_map = {
        "USER.md": WORKDIR / "USER.md",
        "SOUL.md": WORKDIR / "SOUL.md",
        "MEMORY.md": MEMORY_FILE,
    }
    updated: set[str] = set()
    for edit in edits:
        path = file_map.get(edit["file"])
        if not path or edit["action"] != "append":
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if edit["file"] == "USER.md":
            changed = _apply_user_upsert(path, edit["content"])
        else:
            changed = _apply_line_dedup_append(path, edit["content"])
        if changed:
            updated.add(edit["file"])
    return updated


def cleanup_memory_files_once() -> None:
    """
    One-shot cleanup for existing USER.md/SOUL.md/MEMORY.md accumulated duplicates.
    Leaves a marker so it runs at most once per workspace.
    """
    marker = WORKDIR / ".memory_cleaned"
    if marker.exists():
        return
    results: list[str] = []
    for fname, path in (
        ("USER.md",   WORKDIR / "USER.md"),
        ("SOUL.md",   WORKDIR / "SOUL.md"),
        ("MEMORY.md", MEMORY_FILE),
    ):
        if not path.exists():
            continue
        original = path.read_text(encoding="utf-8")
        if fname == "USER.md":
            kvs, rest = _parse_kv(original)
            rebuilt = "\n".join(rest).rstrip() + ("\n\n" + "\n".join(kvs.values()) if kvs else "") + "\n"
        else:
            seen: set[str] = set()
            kept: list[str] = []
            for line in original.splitlines():
                n = _normalize_line(line)
                if n and n in seen:
                    continue
                if n:
                    seen.add(n)
                kept.append(line)
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


class DreamProcessor:
    """
    Dream-like memory processor for Edgebot.

    This sits between short-lived session history and long-term memory files.
    It consumes archived history in batches, optionally mixes in recent live
    conversation, and then performs the existing two-phase extraction flow.
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        model: str = MODEL,
        api_key: str = API_KEY,
        api_base: str | None = API_BASE,
        max_live_messages: int = _MAX_MESSAGES,
        max_archived_batch: int = _MAX_ARCHIVED_BATCH,
    ):
        self.store = store
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.max_live_messages = max_live_messages
        self.max_archived_batch = max_archived_batch

    def _select_archived_batch(self) -> list[dict]:
        entries = self.store.read_unprocessed_history(self.store.get_last_dream_cursor())
        return entries[: self.max_archived_batch]

    def _select_live_messages(self, messages: list[dict]) -> list[dict]:
        return messages[-self.max_live_messages :]

    def _build_inputs(self, messages: list[dict]) -> dict[str, object]:
        live_messages = self._select_live_messages(messages)
        archived_batch = self._select_archived_batch()
        substantive = [
            message
            for message in live_messages
            if message.get("role") in ("user", "assistant") and message.get("content")
        ]
        return {
            "live_messages": live_messages,
            "archived_batch": archived_batch,
            "has_signal": bool(archived_batch) or len(substantive) >= 6,
        }

    def _build_conversation_context(
        self,
        *,
        archived_batch: list[dict],
        live_messages: list[dict],
    ) -> str:
        archived_history = "\n".join(
            f"[{entry['timestamp']}] {entry['content']}"
            for entry in archived_batch
        )
        recent_conversation = _format_messages(live_messages)

        parts: list[str] = []
        if archived_history:
            parts.append(f"## Archived History\n{archived_history}")
        if recent_conversation:
            parts.append(f"## Live Conversation\n{recent_conversation}")
        return "\n\n".join(parts)

    async def _phase1_analyze(
        self,
        *,
        user_content: str,
        soul_content: str,
        memory_content: str,
        conversation: str,
    ) -> str | None:
        prompt = PHASE1_PROMPT.format(
            user_content=user_content,
            soul_content=soul_content,
            memory_content=memory_content,
            conversation=conversation,
        )
        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
                api_key=self.api_key,
                api_base=self.api_base,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            _console.print(f"[dim red]  [memory] phase 1 failed: {exc}[/dim red]")
            return None

    async def _phase2_plan_edits(
        self,
        *,
        analysis: str,
        user_content: str,
        soul_content: str,
        memory_content: str,
    ) -> str | None:
        prompt = PHASE2_PROMPT.format(
            phase1_output=analysis,
            user_content=user_content,
            soul_content=soul_content,
            memory_content=memory_content,
        )
        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
                api_key=self.api_key,
                api_base=self.api_base,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            _console.print(f"[dim red]  [memory] phase 2 failed: {exc}[/dim red]")
            return None

    def _advance_cursor(self, archived_batch: list[dict]) -> None:
        if archived_batch:
            self.store.set_last_dream_cursor(archived_batch[-1]["cursor"])

    async def run(self, messages: list[dict]) -> bool:
        """
        Process one Dream cycle. Returns True if memory files changed.
        """
        inputs = self._build_inputs(messages)
        archived_batch = inputs["archived_batch"]
        live_messages = inputs["live_messages"]
        if not inputs["has_signal"]:
            return False

        conversation = self._build_conversation_context(
            archived_batch=archived_batch,
            live_messages=live_messages,
        )
        if not conversation.strip():
            return False

        user_content = self.store.read_user()
        soul_content = self.store.read_soul()
        memory_content = self.store.read_memory()

        analysis = await self._phase1_analyze(
            user_content=user_content,
            soul_content=soul_content,
            memory_content=memory_content,
            conversation=conversation,
        )
        if analysis is None:
            return False
        if "[SKIP]" in analysis or not analysis.strip():
            self._advance_cursor(archived_batch)
            return False

        existing_blob = "\n".join([user_content, soul_content, memory_content])
        filtered_analysis = _filter_dedup(analysis, existing_blob)
        if not any(
            re.match(r"^\s*\[(USER|SOUL|MEMORY)\]", line)
            for line in filtered_analysis.splitlines()
        ):
            self._advance_cursor(archived_batch)
            return False

        edit_output = await self._phase2_plan_edits(
            analysis=filtered_analysis,
            user_content=user_content,
            soul_content=soul_content,
            memory_content=memory_content,
        )
        if edit_output is None:
            return False
        if "[SKIP]" in edit_output:
            self._advance_cursor(archived_batch)
            return False

        edits = _parse_phase2(edit_output)
        if not edits:
            self._advance_cursor(archived_batch)
            return False

        updated = _apply_edits(edits)
        self._advance_cursor(archived_batch)
        if updated:
            _console.print(f"[dim]  [memory] updated {', '.join(sorted(updated))}[/dim]")
            return True
        return False


_DREAMS: dict[Path, DreamProcessor] = {}


def get_dream_processor(store: MemoryStore | None = None) -> DreamProcessor:
    """Return a cached DreamProcessor for the given store/workspace."""
    target_store = store or _STORE
    key = target_store.workspace.resolve()
    processor = _DREAMS.get(key)
    if processor is None:
        processor = DreamProcessor(target_store)
        _DREAMS[key] = processor
    return processor


async def consolidate_memory(messages: list[dict], store: MemoryStore | None = None) -> bool:
    """
    Backward-compatible wrapper around the Dream-like processor.
    """
    return await get_dream_processor(store).run(messages)
