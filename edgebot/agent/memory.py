"""
edgebot/agent/memory.py - Two-phase memory consolidation.

Extracts user preferences, decisions, and knowledge from conversation history
and surgically updates SOUL.md, USER.md, and memory/MEMORY.md.
"""

import json
import re

import litellm

from edgebot.config import API_BASE, API_KEY, MODEL, WORKDIR

MEMORY_DIR = WORKDIR / "memory"
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"

# How many recent messages to analyze per consolidation
_MAX_MESSAGES = 30

PHASE1_PROMPT = """\
Compare the recent conversation against the current memory files below.
Output one finding per line with a file prefix:
[USER] new fact about the user (identity, preferences, habits)
[SOUL] new behavior/tone preference for the assistant
[MEMORY] new knowledge, project context, or confirmed solutions

Rules:
- Only NEW or CONFLICTING information — skip duplicates and ephemera
- Prefer atomic facts: "prefers Chinese responses" not "discussed language"
- Corrections override old info: [USER] timezone is UTC+8, not UTC
- Skip ephemeral topics (one-off debug questions, transient errors)
- Priority: user corrections > preferences > solutions > decisions > events

If nothing needs updating: [SKIP] no new information

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


def _apply_edits(edits: list[dict]) -> list[str]:
    """Apply parsed edits to workspace files. Returns list of changes made."""
    file_map = {
        "USER.md": WORKDIR / "USER.md",
        "SOUL.md": WORKDIR / "SOUL.md",
        "MEMORY.md": MEMORY_FILE,
    }
    changes = []
    for edit in edits:
        path = file_map.get(edit["file"])
        if not path:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if edit["action"] == "append":
            existing = _read_file(path)
            if existing == "(empty)":
                existing = ""
            new_content = existing.rstrip() + "\n\n" + edit["content"] + "\n"
            path.write_text(new_content, encoding="utf-8")
            changes.append(f"  Updated {edit['file']} (+{len(edit['content'])} chars)")
    return changes


async def consolidate_memory(messages: list[dict]) -> None:
    """
    Two-phase memory consolidation.
    Extracts new facts from recent conversation and writes to workspace files.
    """
    recent = messages[-_MAX_MESSAGES:]
    if len(recent) < 2:
        return

    user_content = _read_file(WORKDIR / "USER.md")
    soul_content = _read_file(WORKDIR / "SOUL.md")
    memory_content = _read_file(MEMORY_FILE)
    conversation = _format_messages(recent)

    if not conversation.strip():
        return

    # --- Phase 1: Analyze ---
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
        print(f"[memory] Phase 1 failed: {e}")
        return

    if "[SKIP]" in analysis:
        print("[memory] No new information to consolidate.")
        return

    print(f"[memory] Phase 1 found updates:\n{analysis[:300]}")

    # --- Phase 2: Generate edits ---
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
        print(f"[memory] Phase 2 failed: {e}")
        return

    if "[SKIP]" in edit_output:
        print("[memory] No edits needed.")
        return

    edits = _parse_phase2(edit_output)
    if not edits:
        print("[memory] No parseable edits.")
        return

    changes = _apply_edits(edits)
    for c in changes:
        print(f"[memory]{c}")
