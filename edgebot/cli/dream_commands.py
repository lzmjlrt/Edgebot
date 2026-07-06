"""
edgebot/cli/dream_commands.py - /dream-log and /dream-restore command handlers.

Formats and renders Dream memory-version history from the git-backed store.
"""

import shlex

from edgebot.cli.ui_state import _MEMORY, console


def _extract_changed_files(diff: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _format_changed_files(diff: str) -> str:
    files = _extract_changed_files(diff)
    if not files:
        return "No tracked memory files changed."
    return ", ".join(f"`{path}`" for path in files)


def _format_dream_log_content(commit, diff: str, *, requested_sha: str | None = None) -> str:
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_sha else "Here is the latest Dream memory change.",
        "",
        f"- Commit: `{commit.sha}`",
        f"- Time: {commit.timestamp}",
        f"- Changed files: {_format_changed_files(diff)}",
    ]
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {commit.sha}` to undo this change.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    else:
        lines.extend(["", "Dream recorded this version, but there is no file diff to display."])
    return "\n".join(lines)


def _format_dream_restore_list(commits: list) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream memory version to restore. Latest first:",
        "",
    ]
    for commit in commits:
        lines.append(f"- `{commit.sha}` {commit.timestamp} - {commit.message.splitlines()[0]}")
    lines.extend([
        "",
        "Preview a version with `/dream-log <sha>` before restoring it.",
        "Restore a version with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


def _handle_dream_log_command(query: str) -> None:
    _MEMORY.ensure_git_initialized()
    git = _MEMORY.git
    if not git.is_initialized():
        console.print("[dim]  Dream history is not available because memory versioning is not initialized.[/dim]")
        return

    parts = shlex.split(query)
    if len(parts) > 1:
        sha = parts[1]
        result = git.show_commit_diff(sha)
        if not result:
            console.print(f"[dim]  Couldn't find Dream change {sha}.[/dim]")
            return
        commit, diff = result
        console.print(_format_dream_log_content(commit, diff, requested_sha=sha), highlight=False)
        return

    commits = git.log(max_entries=1)
    if not commits:
        console.print("[dim]  Dream memory has no saved versions yet.[/dim]")
        return
    result = git.show_commit_diff(commits[0].sha)
    if not result:
        console.print("[dim]  Dream memory has no diff to display yet.[/dim]")
        return
    commit, diff = result
    console.print(_format_dream_log_content(commit, diff), highlight=False)


def _handle_dream_restore_command(query: str) -> None:
    _MEMORY.ensure_git_initialized()
    git = _MEMORY.git
    if not git.is_initialized():
        console.print("[dim]  Dream history is not available because memory versioning is not initialized.[/dim]")
        return

    parts = shlex.split(query)
    if len(parts) == 1:
        commits = git.log(max_entries=10)
        if not commits:
            console.print("[dim]  Dream memory has no saved versions to restore yet.[/dim]")
            return
        console.print(_format_dream_restore_list(commits), highlight=False)
        return

    sha = parts[1]
    result = git.show_commit_diff(sha)
    changed_files = _format_changed_files(result[1]) if result else "the tracked memory files"
    new_sha = git.revert(sha)
    if new_sha:
        console.print(
            (
                f"Restored Dream memory to the state before `{sha}`.\n\n"
                f"- New safety commit: `{new_sha}`\n"
                f"- Restored files: {changed_files}\n\n"
                f"Use `/dream-log {new_sha}` to inspect the restore diff."
            ),
            highlight=False,
        )
    else:
        console.print(f"[dim]  Couldn't restore Dream change {sha}.[/dim]")
