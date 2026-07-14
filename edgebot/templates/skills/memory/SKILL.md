---
name: memory
description: Use Edgebot's two-layer memory system and search archived history safely.
always: true
---

# Memory

## Structure

- `SOUL.md` - bot personality and communication style. Managed by Dream.
- `USER.md` - user profile and durable preferences. Managed by Dream.
- `MEMORY.md` - long-term facts and project context. Managed by Dream.
- `skills/<name>/SKILL.md` - reusable workflows, commands, API parameters, and operational procedures. Managed by Dream.
- `history.jsonl` - append-only JSONL archive. It is not fully loaded into context.

These are runtime-managed files, outside the workspace. Their locations are
provided by the runtime when needed; do not infer or create a workspace-local
memory directory.

## Search Past Events

Use the runtime memory workflow when you need old details not present in the current context.

- Start broad with a count or files-with-matches search before expanding output.
- Use content mode with nearby context when you need exact lines.
- Use fixed-string matching for timestamps, cursor ids, paths, or JSON fragments.
- Prefer targeted searches over loading an entire archive.

## Important

- When the user explicitly asks to save durable information, pass it to the Dream memory workflow; never use workspace filesystem tools to edit runtime memory files.
- Do not manually move reusable workflow details into `MEMORY.md`; Dream routes those to `skills/<name>/SKILL.md`.
- Use `/memory` to manually run Dream consolidation.
- Use `/dream-log` to inspect Dream changes and `/dream-restore` to roll one back.
