"""
edgebot/agent/memory.py - Two-phase memory consolidation.

Extracts user preferences, decisions, and knowledge from conversation history
and surgically updates SOUL.md, USER.md, and memory/MEMORY.md.
"""

import json
import re

import litellm
from rich.console import Console

from edgebot.config import API_BASE, API_KEY, MODEL, WORKDIR

_console = Console()

MEMORY_DIR = WORKDIR / "memory"
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"

# How many recent messages to analyze per consolidation
_MAX_MESSAGES = 30

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


async def consolidate_memory(messages: list[dict]) -> None:
    """
    Two-phase memory consolidation — runs silently.
    Prints a single summary line only when files actually change.
    """
    # Require a minimum amount of substantive conversation before running
    substantive = [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    if len(substantive) < 6:
        return
    recent = messages[-_MAX_MESSAGES:]

    user_content = _read_file(WORKDIR / "USER.md")
    soul_content = _read_file(WORKDIR / "SOUL.md")
    memory_content = _read_file(MEMORY_FILE)
    conversation = _format_messages(recent)

    if not conversation.strip():
        return

    # --- Phase 1: Analyze (silent) ---
    p1_prompt = PHASE1_PROMPT.format(
        user_content=user_content,
        soul_content=soul_content,
        memory_content=memory_content,
        conversation=conversation,
    )
    try:
        resp1 = await litellm.acompletion(
            model=MODEL,
            messages=[{"role": "user", "content": p1_prompt}],
            max_tokens=2000,
            api_key=API_KEY, api_base=API_BASE,
        )
        analysis = resp1.choices[0].message.content or ""
    except Exception as e:
        _console.print(f"[dim red]  [memory] phase 1 failed: {e}[/dim red]")
        return

    if "[SKIP]" in analysis or not analysis.strip():
        return

    # --- Dedup filter: drop analysis lines already covered by existing memory ---
    existing_blob = "\n".join([user_content, soul_content, memory_content])
    analysis = _filter_dedup(analysis, existing_blob)
    # If after dedup nothing useful is left, bail
    if not any(
        re.match(r"^\s*\[(USER|SOUL|MEMORY)\]", ln)
        for ln in analysis.splitlines()
    ):
        return

    # --- Phase 2: Generate edits (silent) ---
    p2_prompt = PHASE2_PROMPT.format(
        phase1_output=analysis,
        user_content=user_content,
        soul_content=soul_content,
        memory_content=memory_content,
    )
    try:
        resp2 = await litellm.acompletion(
            model=MODEL,
            messages=[{"role": "user", "content": p2_prompt}],
            max_tokens=2000,
            api_key=API_KEY, api_base=API_BASE,
        )
        edit_output = resp2.choices[0].message.content or ""
    except Exception as e:
        _console.print(f"[dim red]  [memory] phase 2 failed: {e}[/dim red]")
        return

    if "[SKIP]" in edit_output:
        return

    edits = _parse_phase2(edit_output)
    if not edits:
        return

    updated = _apply_edits(edits)
    if updated:
        _console.print(
            f"[dim]  [memory] updated {', '.join(sorted(updated))}[/dim]"
        )
