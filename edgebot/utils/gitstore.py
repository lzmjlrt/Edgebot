"""Git-backed version control for Edgebot memory files.

This is intentionally optional: if dulwich is unavailable or git operations
fail, callers can continue without memory version history.
"""

from __future__ import annotations

import io
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class CommitInfo:
    sha: str
    message: str
    timestamp: str


@dataclass
class LineAge:
    age_days: int


def _compute_line_ages(annotated) -> list[LineAge]:
    now = datetime.now(tz=timezone.utc).date()
    ages: list[LineAge] = []
    for (commit, _tree_entry), _line_bytes in annotated:
        dt = datetime.fromtimestamp(commit.commit_time, tz=timezone.utc).date()
        ages.append(LineAge(age_days=(now - dt).days))
    return ages


class GitStore:
    """Small dulwich-backed git store for memory audit and restore."""

    def __init__(
        self,
        workspace: Path,
        tracked_files: list[str],
        *,
        allow_nested: bool = False,
    ):
        self._workspace = workspace
        self._tracked_files = tracked_files
        self._allow_nested = allow_nested

    def is_initialized(self) -> bool:
        return (self._workspace / ".git").is_dir()

    def init(self) -> bool:
        """Initialize a git repo for tracked memory files if needed."""
        if self.is_initialized():
            return False
        if not self._allow_nested and self._is_inside_git_repo():
            return False

        try:
            from dulwich import porcelain

            self._workspace.mkdir(parents=True, exist_ok=True)
            porcelain.init(str(self._workspace))

            gitignore = self._workspace / ".gitignore"
            gitignore.write_text(self._build_gitignore(), encoding="utf-8")

            for rel in self._tracked_files:
                p = self._workspace / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    p.write_text("", encoding="utf-8")

            porcelain.add(str(self._workspace), paths=[".gitignore"] + self._tracked_files)
            porcelain.commit(
                str(self._workspace),
                message=b"init: edgebot memory store",
                author=b"edgebot <edgebot@dream>",
                committer=b"edgebot <edgebot@dream>",
            )
            return True
        except Exception:
            return self._init_cli()

    def auto_commit(self, message: str) -> str | None:
        """Commit tracked memory files if they changed."""
        if not self.is_initialized():
            return None

        try:
            from dulwich import porcelain

            st = porcelain.status(str(self._workspace))
            if not st.unstaged and not any(st.staged.values()):
                return None

            msg_bytes = message.encode("utf-8") if isinstance(message, str) else message
            porcelain.add(str(self._workspace), paths=self._tracked_files)
            sha_bytes = porcelain.commit(
                str(self._workspace),
                message=msg_bytes,
                author=b"edgebot <edgebot@dream>",
                committer=b"edgebot <edgebot@dream>",
            )
            if sha_bytes is None:
                return None
            return sha_bytes.hex()[:8]
        except Exception:
            return self._auto_commit_cli(message)

    def log(self, max_entries: int = 20) -> list[CommitInfo]:
        if not self.is_initialized():
            return []

        try:
            from dulwich.repo import Repo

            entries: list[CommitInfo] = []
            with Repo(str(self._workspace)) as repo:
                try:
                    sha = repo.refs[b"HEAD"]
                except KeyError:
                    return []

                while sha and len(entries) < max_entries:
                    commit = repo[sha]
                    if commit.type_name != b"commit":
                        break
                    ts = time.strftime(
                        "%Y-%m-%d %H:%M",
                        time.localtime(commit.commit_time),
                    )
                    entries.append(CommitInfo(
                        sha=sha.hex()[:8],
                        message=commit.message.decode("utf-8", errors="replace").strip(),
                        timestamp=ts,
                    ))
                    sha = commit.parents[0] if commit.parents else None
            return entries
        except Exception:
            return self._log_cli(max_entries=max_entries)

    def line_ages(self, file_path: str) -> list[LineAge]:
        if not self.is_initialized():
            return []
        target = self._workspace / file_path
        if not target.exists() or target.stat().st_size == 0:
            return []

        try:
            from dulwich import porcelain

            annotated = porcelain.annotate(str(self._workspace), file_path)
        except Exception:
            return self._line_ages_cli(file_path)
        if not annotated:
            return []
        return _compute_line_ages(annotated)

    def diff_commits(self, sha1: str, sha2: str) -> str:
        if not self.is_initialized():
            return ""

        try:
            from dulwich import porcelain

            full1 = self._resolve_sha(sha1)
            full2 = self._resolve_sha(sha2)
            if not full1 or not full2:
                return ""

            out = io.BytesIO()
            porcelain.diff(
                str(self._workspace),
                commit=full1,
                commit2=full2,
                outstream=out,
            )
            return out.getvalue().decode("utf-8", errors="replace")
        except Exception:
            return self._diff_commits_cli(sha1, sha2)

    def show_commit_diff(
        self,
        short_sha: str,
        max_entries: int = 20,
    ) -> tuple[CommitInfo, str] | None:
        commits = self.log(max_entries=max_entries)
        for idx, commit in enumerate(commits):
            if commit.sha.startswith(short_sha):
                if idx + 1 < len(commits):
                    diff = self.diff_commits(commits[idx + 1].sha, commit.sha)
                else:
                    diff = ""
                return commit, diff
        return None

    def revert(self, commit: str) -> str | None:
        """Restore tracked files to the state before *commit* and commit that restore."""
        if not self.is_initialized():
            return None

        try:
            from dulwich.repo import Repo

            full_sha = self._resolve_sha(commit)
            if not full_sha:
                return None

            with Repo(str(self._workspace)) as repo:
                commit_obj = repo[full_sha]
                if commit_obj.type_name != b"commit" or not commit_obj.parents:
                    return None

                parent_obj = repo[commit_obj.parents[0]]
                tree = repo[parent_obj.tree]

                restored: list[str] = []
                for filepath in self._tracked_files:
                    content = self._read_blob_from_tree(repo, tree, filepath)
                    if content is None:
                        continue
                    dest = self._workspace / filepath
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content, encoding="utf-8")
                    restored.append(filepath)

            if not restored:
                return None
            return self.auto_commit(f"revert: undo {commit}")
        except Exception:
            return self._revert_cli(commit)

    def _resolve_sha(self, short_sha: str) -> bytes | None:
        try:
            from dulwich.repo import Repo

            with Repo(str(self._workspace)) as repo:
                try:
                    sha = repo.refs[b"HEAD"]
                except KeyError:
                    return None

                while sha:
                    if sha.hex().startswith(short_sha):
                        return sha
                    commit = repo[sha]
                    if commit.type_name != b"commit":
                        break
                    sha = commit.parents[0] if commit.parents else None
            return None
        except Exception:
            return None

    def _is_inside_git_repo(self) -> bool:
        current = self._workspace.resolve()
        while current != current.parent:
            if (current / ".git").exists():
                return True
            current = current.parent
        return False

    def _run_git(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self._workspace), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def _init_cli(self) -> bool:
        try:
            self._workspace.mkdir(parents=True, exist_ok=True)
            self._run_git(["init"])

            gitignore = self._workspace / ".gitignore"
            gitignore.write_text(self._build_gitignore(), encoding="utf-8")

            for rel in self._tracked_files:
                p = self._workspace / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    p.write_text("", encoding="utf-8")

            self._run_git(["add", ".gitignore", *self._tracked_files])
            self._run_git([
                "-c", "user.name=edgebot",
                "-c", "user.email=edgebot@dream",
                "commit", "-m", "init: edgebot memory store",
            ])
            return True
        except Exception:
            return False

    def _auto_commit_cli(self, message: str) -> str | None:
        try:
            status = self._run_git(
                ["status", "--porcelain", "--", *self._tracked_files],
                check=False,
            )
            if not status.stdout.strip():
                return None
            self._run_git(["add", *self._tracked_files])
            commit = self._run_git([
                "-c", "user.name=edgebot",
                "-c", "user.email=edgebot@dream",
                "commit", "-m", message,
            ], check=False)
            if commit.returncode != 0:
                return None
            head = self._run_git(["rev-parse", "--short=8", "HEAD"])
            return head.stdout.strip() or None
        except Exception:
            return None

    def _log_cli(self, max_entries: int = 20) -> list[CommitInfo]:
        try:
            proc = self._run_git([
                "log", f"-n{max_entries}", "--format=%H%x09%ct%x09%s",
                "--", *self._tracked_files,
            ], check=False)
            if proc.returncode != 0:
                return []
            entries: list[CommitInfo] = []
            for line in proc.stdout.splitlines():
                parts = line.split("\t", 2)
                if len(parts) != 3:
                    continue
                sha, raw_ts, message = parts
                try:
                    ts_val = int(raw_ts)
                except ValueError:
                    ts_val = 0
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_val))
                entries.append(CommitInfo(sha=sha[:8], message=message, timestamp=ts))
            return entries
        except Exception:
            return []

    def _line_ages_cli(self, file_path: str) -> list[LineAge]:
        try:
            proc = self._run_git(["blame", "--line-porcelain", "--", file_path], check=False)
            if proc.returncode != 0:
                return []
            now = datetime.now(tz=timezone.utc).date()
            ages: list[LineAge] = []
            for line in proc.stdout.splitlines():
                if not line.startswith("author-time "):
                    continue
                try:
                    ts = int(line.split(" ", 1)[1])
                except ValueError:
                    continue
                dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                ages.append(LineAge(age_days=(now - dt).days))
            return ages
        except Exception:
            return []

    def _diff_commits_cli(self, sha1: str, sha2: str) -> str:
        try:
            proc = self._run_git([
                "diff", sha1, sha2, "--", *self._tracked_files,
            ], check=False)
            if proc.returncode not in (0, 1):
                return ""
            return proc.stdout
        except Exception:
            return ""

    def _revert_cli(self, commit: str) -> str | None:
        try:
            parent = self._run_git([
                "rev-parse", "--verify", f"{commit}^",
            ], check=False)
            if parent.returncode != 0:
                return None
            parent_sha = parent.stdout.strip()
            checkout = self._run_git([
                "checkout", parent_sha, "--", *self._tracked_files,
            ], check=False)
            if checkout.returncode != 0:
                return None
            return self.auto_commit(f"revert: undo {commit}")
        except Exception:
            return None

    def _build_gitignore(self) -> str:
        dirs: set[str] = set()
        for f in self._tracked_files:
            parent = str(Path(f).parent).replace("\\", "/")
            if parent != ".":
                dirs.add(parent)
        lines = ["/*"]
        for d in sorted(dirs):
            lines.append(f"!{d}/")
        for f in self._tracked_files:
            lines.append("!" + f.replace("\\", "/"))
        lines.append("!.gitignore")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _read_blob_from_tree(repo, tree, filepath: str) -> str | None:
        parts = Path(filepath).parts
        current = tree
        for part in parts:
            try:
                entry = current[part.encode()]
            except KeyError:
                return None
            obj = repo[entry[1]]
            if obj.type_name == b"blob":
                return obj.data.decode("utf-8", errors="replace")
            if obj.type_name == b"tree":
                current = obj
                continue
            return None
        return None
