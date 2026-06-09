---
name: memory
description: Use Edgebot's two-layer memory system and search archived history safely.
always: true
---

# Memory

## Structure

- `.edgebot/SOUL.md` - bot personality and communication style. Managed by Dream.
- `.edgebot/USER.md` - user profile and durable preferences. Managed by Dream.
- `.edgebot/memory/MEMORY.md` - long-term facts and project context. Managed by Dream.
- `.edgebot/skills/<name>/SKILL.md` - reusable workflows, commands, API parameters, and operational procedures. Managed by Dream.
- `.edgebot/memory/history.jsonl` - append-only JSONL archive. It is not fully loaded into context.

## Search Past Events

Use `grep`/`rg` over `.edgebot/memory/history.jsonl` when you need old details not present in the current context.

- Start broad with a count or files-with-matches search before expanding output.
- Use content mode with nearby context when you need exact lines.
- Use fixed-string matching for timestamps, cursor ids, paths, or JSON fragments.
- Prefer searching `.edgebot/memory/*.jsonl` instead of loading the whole file.

Examples:

```text
grep(pattern="keyword", path=".edgebot/memory/history.jsonl", case_insensitive=true)
grep(pattern="2026-04-02 10:00", path=".edgebot/memory/history.jsonl", fixed_strings=true)
grep(pattern="oauth|token", path=".edgebot/memory", glob="*.jsonl", output_mode="content", case_insensitive=true)
```

## Important

- Do not edit `.edgebot/SOUL.md`, `.edgebot/USER.md`, or `.edgebot/memory/MEMORY.md` directly unless the user explicitly asks. Dream maintains them.
- Do not manually move reusable workflow details into `.edgebot/memory/MEMORY.md`; Dream routes those to `.edgebot/skills/<name>/SKILL.md`.
- Use `/memory` to manually run Dream consolidation.
- Use `/dream-log` to inspect Dream changes and `/dream-restore` to roll one back.
