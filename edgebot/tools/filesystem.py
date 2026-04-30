"""File read/write/edit/list helpers."""

from __future__ import annotations

import difflib
import mimetypes
import os
from pathlib import Path

from edgebot.tools import file_state
from edgebot.tools.base import safe_path

_MAX_READ_CHARS = 128_000
_DEFAULT_READ_LIMIT = 2000
_MAX_EDIT_FILE_SIZE = 1024 * 1024 * 1024
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".coverage", "htmlcov",
}
_BLOCKED_DEVICE_PATHS = frozenset({
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/stdout", "/dev/stderr",
    "/dev/tty", "/dev/console", "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})


def _is_blocked_device(path: str | Path) -> bool:
    raw = str(path)
    try:
        resolved = str(Path(raw).resolve())
    except (OSError, ValueError):
        resolved = raw
    return (
        raw in _BLOCKED_DEVICE_PATHS
        or resolved in _BLOCKED_DEVICE_PATHS
        or resolved.startswith("/dev/")
    )


def run_read(path: str, limit: int | None = None, offset: int = 1) -> str:
    try:
        fp = safe_path(path)
        if _is_blocked_device(fp):
            return f"Error: Reading {path} is blocked (device path)."
        if not fp.exists():
            return f"Error: File not found: {path}"
        if not fp.is_file():
            return f"Error: Not a file: {path}"

        entry = file_state._state.get(str(fp.resolve()))
        try:
            current_mtime = os.path.getmtime(fp)
        except OSError:
            current_mtime = 0.0
        if entry and entry.can_dedup and entry.offset == offset and entry.limit == limit:
            current_hash = file_state._hash_file(fp)
            if current_mtime == entry.mtime and current_hash == entry.content_hash:
                return f"[File unchanged since last read: {path}]"

        raw = fp.read_bytes()
        if not raw:
            return f"(Empty file: {path})"

        mime = mimetypes.guess_type(str(fp))[0]
        if mime and mime.startswith("image/"):
            return f"(Image file: {path} — use a vision-capable workflow to inspect it)"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"Error: Cannot read binary file {path}. Only UTF-8 text is supported."

        text = text.replace("\r\n", "\n")
        all_lines = text.splitlines()
        total = len(all_lines)
        if total == 0:
            file_state.record_read(fp, offset=1, limit=limit)
            return f"(Empty file: {path})"

        if offset < 1:
            offset = 1
        if offset > total:
            return f"Error: offset {offset} is beyond end of file ({total} lines)"

        end = min(total, offset - 1 + (limit or _DEFAULT_READ_LIMIT))
        numbered = [f"{i + 1}| {line}" for i, line in enumerate(all_lines[offset - 1:end], start=offset - 1)]
        result = "\n".join(numbered)
        if len(result) > _MAX_READ_CHARS:
            result = result[:_MAX_READ_CHARS] + "\n\n(Output truncated at ~128K chars)"
        if end < total:
            result += f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
        else:
            result += f"\n\n(End of file — {total} lines total)"
        file_state.record_read(fp, offset=offset, limit=limit)
        return result
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        file_state.record_write(fp)
        return f"Successfully wrote {len(content)} characters to {fp}"
    except Exception as e:
        return f"Error writing file: {e}"


def _best_window(old_text: str, content: str) -> tuple[float, int, list[str]]:
    lines = content.splitlines(keepends=True)
    old_lines = old_text.splitlines(keepends=True) or [old_text]
    window = max(1, len(old_lines))
    best_ratio, best_start, best_window_lines = -1.0, 0, []
    for i in range(max(1, len(lines) - window + 1)):
        current = lines[i:i + window]
        ratio = difflib.SequenceMatcher(None, old_lines, current).ratio()
        if ratio > best_ratio:
            best_ratio, best_start, best_window_lines = ratio, i, current
    return best_ratio, best_start, best_window_lines


def run_edit(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    try:
        fp = safe_path(path)
        if not fp.exists():
            if old_text == "":
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(new_text, encoding="utf-8")
                file_state.record_write(fp)
                return f"Successfully created {fp}"
            return f"Error: File not found: {path}"
        if not fp.is_file():
            return f"Error: Not a file: {path}"
        try:
            fsize = fp.stat().st_size
        except OSError:
            fsize = 0
        if fsize > _MAX_EDIT_FILE_SIZE:
            return f"Error: File too large to edit ({fsize / (1024**3):.1f} GiB). Maximum is 1 GiB."

        warning = file_state.check_read(fp)
        content = fp.read_text(encoding="utf-8").replace("\r\n", "\n")
        if old_text == "":
            if content.strip():
                return f"Error: Cannot create file — {path} already exists and is not empty."
            fp.write_text(new_text, encoding="utf-8")
            file_state.record_write(fp)
            return f"Successfully edited {fp}"

        count = content.count(old_text)
        if count == 0:
            best_ratio, best_start, best_window_lines = _best_window(old_text, content)
            if best_ratio > 0.5:
                diff = "\n".join(difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    best_window_lines,
                    fromfile="old_text (provided)",
                    tofile=f"{path} (actual, line {best_start + 1})",
                    lineterm="",
                ))
                return (
                    f"Error: old_text not found in {path}. "
                    f"Best match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
                )
            return f"Error: old_text not found in {path}. No similar text found."
        if count > 1 and not replace_all:
            return (
                f"Warning: old_text appears {count} times. "
                "Provide more context to make it unique, or set replace_all=true."
            )

        updated = content.replace(old_text, new_text, -1 if replace_all else 1)
        fp.write_text(updated, encoding="utf-8")
        file_state.record_write(fp)
        msg = f"Successfully edited {fp}"
        return f"{warning}\n{msg}" if warning else msg
    except Exception as e:
        return f"Error editing file: {e}"


def run_list_dir(path: str, recursive: bool = False, max_entries: int = 200) -> str:
    try:
        dp = safe_path(path)
        if not dp.exists():
            return f"Error: Directory not found: {path}"
        if not dp.is_dir():
            return f"Error: Not a directory: {path}"
        items: list[str] = []
        total = 0
        iterator = dp.rglob("*") if recursive else dp.iterdir()
        for item in sorted(iterator):
            if any(part in _IGNORE_DIRS for part in item.parts):
                continue
            total += 1
            if len(items) >= max_entries:
                continue
            rel = item.relative_to(dp)
            if recursive:
                items.append(f"{rel}/" if item.is_dir() else str(rel))
            else:
                items.append((f"📁 {item.name}" if item.is_dir() else f"📄 {item.name}"))
        if not items and total == 0:
            return f"Directory {path} is empty"
        result = "\n".join(items)
        if total > max_entries:
            result += f"\n\n(truncated, showing first {max_entries} of {total} entries)"
        return result
    except Exception as e:
        return f"Error listing directory: {e}"
