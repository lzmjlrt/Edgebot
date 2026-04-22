# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## bash — Safety

- Commands timeout at 120s
- Dangerous commands (rm -rf /, sudo, shutdown, reboot) are blocked
- URLs targeting internal networks are blocked (SSRF protection)
- Output is truncated at 50,000 characters

## read_file / write_file / edit_file

- Paths are sandboxed to the workspace directory
- Always read a file before editing it
- edit_file replaces the first occurrence of old_text only

## task (subagent)

- "Explore" type: read-only access (bash + read_file)
- "general-purpose" type: full access (bash + read/write/edit)
- Use Explore for investigation, general-purpose for implementation
- Subagents run up to 30 iterations

## background_run / check_background

- Runs commands in a background thread (default timeout 120s)
- Use check_background with the returned task_id to poll status
- Background task notifications are automatically drained each turn

## cron

- Schedules one-shot or recurring agent tasks
- `add` supports `every_seconds`, `at`, or `cron_expr`
- Cron expressions require optional dependency `croniter`
- Jobs are persisted in `.edgebot/cron/jobs.json`

## task_create / task_update / task_list

- File-backed persistent tasks in .tasks/ directory
- Support dependencies (blockedBy, blocks)
- Use for multi-step work tracking

## TodoWrite

- In-memory checklist — lighter than file tasks
- Only one item can be in_progress at a time
- Max 20 items
