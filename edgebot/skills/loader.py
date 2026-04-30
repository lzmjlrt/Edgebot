"""
edgebot/skills/loader.py - Skill discovery, metadata parsing, and loading.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

_STRIP_SKILL_FRONTMATTER = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)
_BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent / "templates" / "skills"


class SkillLoader:
    """
    Loader for workspace and built-in skills.

    Workspace skills override built-ins with the same name. The loader keeps
    lightweight metadata in memory and returns full bodies on demand.
    """

    def __init__(
        self,
        skills_dir: Path,
        builtin_skills_dir: Path | None = None,
        extra_workspace_dirs: list[Path] | None = None,
    ):
        self.skills_dir = skills_dir
        self.builtin_skills_dir = builtin_skills_dir or _BUILTIN_SKILLS_DIR
        self.extra_workspace_dirs = list(extra_workspace_dirs or [])
        self._skill_entries: dict[str, dict[str, str]] = {}
        self.reload()

    def reload(self) -> None:
        """Reload skill metadata from built-in templates and workspace."""
        self._skill_entries.clear()

        builtin_entries = self._entries_from_dir(self.builtin_skills_dir, "builtin")
        for entry in builtin_entries:
            self._skill_entries[entry["name"]] = entry

        for workspace_dir in [self.skills_dir, *self.extra_workspace_dirs]:
            workspace_entries = self._entries_from_dir(workspace_dir, "workspace")
            for entry in workspace_entries:
                self._skill_entries[entry["name"]] = entry

    def _entries_from_dir(self, base_dir: Path, source: str) -> list[dict[str, str]]:
        if not base_dir.exists():
            return []

        entries: list[dict[str, str]] = []
        for skill_dir in sorted(base_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            skill_name = skill_dir.name
            metadata = self._read_frontmatter(skill_file)
            if metadata and isinstance(metadata.get("name"), str) and metadata["name"].strip():
                skill_name = metadata["name"].strip()

            entries.append({
                "name": skill_name,
                "path": str(skill_file),
                "source": source,
            })
        return entries

    def list_skills(self, filter_unavailable: bool = False) -> list[dict[str, str]]:
        """List known skills with workspace override already applied."""
        entries = list(self._skill_entries.values())
        if not filter_unavailable:
            return entries
        return [entry for entry in entries if self._check_requirements(self._get_skill_meta(entry["name"]))]

    def get_skill_path(self, name: str) -> Path | None:
        """Return the resolved SKILL.md path for a named skill."""
        entry = self._skill_entries.get(name)
        if not entry:
            return None
        return Path(entry["path"])

    def load_skill(self, name: str) -> str | None:
        """Load the raw markdown of a skill."""
        path = self.get_skill_path(name)
        if not path or not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def load(self, name: str) -> str:
        """Load a skill body for the existing load_skill tool."""
        markdown = self.load_skill(name)
        if markdown is None:
            available = ", ".join(sorted(self._skill_entries.keys()))
            return f"Error: Unknown skill '{name}'. Available: {available}"
        return f'<skill name="{name}">\n{self._strip_frontmatter(markdown)}\n</skill>'

    def descriptions(self) -> str:
        """Backward-compatible summary hook used by older call sites."""
        summary = self.build_skills_summary()
        return summary if summary else "(no skills)"

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """Load full bodies for skills that should be injected into context."""
        parts = []
        for name in skill_names:
            markdown = self.load_skill(name)
            if not markdown:
                continue
            parts.append(f"### Skill: {name}\n\n{self._strip_frontmatter(markdown)}")
        return "\n\n---\n\n".join(parts)

    def build_skills_summary(self, exclude: set[str] | None = None) -> str:
        """
        Build a progressive-loading skill summary with path and availability.
        """
        entries = self.list_skills(filter_unavailable=False)
        if not entries:
            return ""

        lines: list[str] = [
            "Use `load_skill` to load a skill body when you need the detailed instructions.",
            "You can also inspect the file directly with `read_file` if needed.",
            "",
        ]
        for entry in entries:
            skill_name = entry["name"]
            if exclude and skill_name in exclude:
                continue
            desc = self._get_skill_description(skill_name)
            meta = self._get_skill_meta(skill_name)
            available = self._check_requirements(meta)
            label = (
                f"- **{skill_name}** - {desc}  `{entry['path']}`"
                if desc
                else f"- **{skill_name}**  `{entry['path']}`"
            )
            if not available:
                missing = self._get_missing_requirements(meta)
                label += f" (unavailable: {missing})" if missing else " (unavailable)"
            lines.append(label)
        return "\n".join(lines).strip()

    def get_always_skills(self) -> list[str]:
        """Return available skills marked as always-on."""
        names: list[str] = []
        for entry in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(entry["name"]) or {}
            nanobot_meta = self._parse_nested_metadata(meta.get("metadata"))
            if nanobot_meta.get("always") or meta.get("always"):
                names.append(entry["name"])
        return names

    def get_skill_metadata(self, name: str) -> dict[str, Any] | None:
        """Return parsed frontmatter metadata for a skill."""
        path = self.get_skill_path(name)
        if not path or not path.exists():
            return None
        return self._read_frontmatter(path)

    def _read_frontmatter(self, path: Path) -> dict[str, Any] | None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.startswith("---"):
            return None
        match = _STRIP_SKILL_FRONTMATTER.match(text)
        if not match:
            return None
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _strip_frontmatter(self, content: str) -> str:
        if not content.startswith("---"):
            return content.strip()
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if match:
            return content[match.end():].strip()
        return content.strip()

    def _get_skill_description(self, name: str) -> str:
        meta = self.get_skill_metadata(name)
        if meta and isinstance(meta.get("description"), str):
            return meta["description"]
        return name

    def _get_skill_meta(self, name: str) -> dict[str, Any]:
        raw_meta = self.get_skill_metadata(name) or {}
        nested_meta = self._parse_nested_metadata(raw_meta.get("metadata"))
        if nested_meta:
            return nested_meta
        return {
            "requires": raw_meta.get("requires", {}),
            "always": raw_meta.get("always", False),
        }

    def _parse_nested_metadata(self, raw: object) -> dict[str, Any]:
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        else:
            return {}
        if not isinstance(data, dict):
            return {}
        payload = data.get("nanobot", data.get("openclaw", {}))
        return payload if isinstance(payload, dict) else {}

    def _check_requirements(self, skill_meta: dict[str, Any]) -> bool:
        requires = skill_meta.get("requires", {}) or {}
        required_bins = requires.get("bins", []) or []
        required_env_vars = requires.get("env", []) or []
        return all(shutil.which(str(cmd)) for cmd in required_bins) and all(
            os.environ.get(str(var)) for var in required_env_vars
        )

    def _get_missing_requirements(self, skill_meta: dict[str, Any]) -> str:
        requires = skill_meta.get("requires", {}) or {}
        required_bins = requires.get("bins", []) or []
        required_env_vars = requires.get("env", []) or []
        missing_bins = [f"CLI: {cmd}" for cmd in required_bins if not shutil.which(str(cmd))]
        missing_env = [f"ENV: {var}" for var in required_env_vars if not os.environ.get(str(var))]
        return ", ".join(missing_bins + missing_env)
