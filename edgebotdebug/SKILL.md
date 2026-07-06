---
name: edgebotdebug
description: A brief description of what this skill does
---


# EdgeBot Debug

Use this skill to debug the EdgeBot project from a Markdown handoff document.

## Workflow

1. Read the user's Markdown handoff document first. If the document path is missing or ambiguous, ask for the exact path before changing code.
2. Build an initial understanding of the project state from the handoff:
   - Identify unresolved bug points.
   - Identify the expected behavior, observed behavior, suspected files, commands, and prior attempts.
   - Do not start implementation until one concrete bug point is selected.
3. Solve only one bug point per run:
   - Choose the first unresolved bug unless the user explicitly names another one.
   - Keep the change scoped to the files needed for that bug.
   - Do not refactor unrelated code or fix additional bugs opportunistically.
4. Reproduce or inspect the bug before editing when practical. Read relevant files before modifying them.
5. Implement the smallest defensible fix that matches the existing code style and project patterns.
6. Validate the fix with the most relevant available command, such as a focused test, lint check, type check, or manual reproduction step. If validation cannot be run, record why.
7. Re-read the edited files when accuracy matters, especially for handoff-document updates and commit-sensitive changes.
8. Update the Markdown handoff document after the bug is resolved:
   - Mark the selected bug point as resolved.
   - Add the exact fix summary.
   - Add validation performed and any remaining risk.
   - Leave unresolved bug points clearly unchanged.
9. Create a git commit after validation:
   - Inspect `git status` before staging.
   - Stage only core code changes for the resolved bug.
   - Do not stage unrelated work, generated noise, local environment files, or broad formatting churn.
   - Do not include the handoff document in the commit unless the user explicitly asks for documentation changes to be committed.
   - Use a concise commit message that names the fixed bug.

## Debugging Rules

- Preserve user changes. Never revert files you did not intentionally modify unless the user explicitly asks.
- Treat the handoff document as the source of truth for task order and bug status.
- If the handoff contains multiple unclear or conflicting bug points, ask one concise clarification before editing.
- If a bug cannot be reproduced, continue with static diagnosis only when the handoff provides enough evidence; otherwise ask for the missing reproduction details.
- Prefer targeted tests over broad test suites unless the fix touches shared behavior.
- Stop after one resolved bug point, the handoff update, and the commit.

## Handoff Update Format

When the handoff document does not define its own status format, use this minimal format under the selected bug point:

```markdown
Status: Resolved
Fix: <short summary of the code change>
Validation: <command or manual check performed>
Commit: <commit hash or commit message>
Remaining risk: <none or concise note>
```

