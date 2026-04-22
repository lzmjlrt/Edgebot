"""
edgebot/skills/loader.py - SKILL.md discovery and loading.
"""

import re
from pathlib import Path

_FRONTMATTER_RE = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n)?(.*)$",
    re.DOTALL,
)
_BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent / "templates" / "skills"


class SkillLoader:
    def __init__(self, skills_dir: Path, builtin_skills_dir: Path | None = None):
        self.skills_dir = skills_dir
        self.builtin_skills_dir = builtin_skills_dir or _BUILTIN_SKILLS_DIR
        self.skills = {}
        self.reload()

    @staticmethod
    def _parse_skill_markdown(text: str) -> tuple[dict, str]:
        match = _FRONTMATTER_RE.match(text)
        meta, body = {}, text
        if match:
            for line in match.group(1).strip().splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
            body = match.group(2).strip()
        return meta, body

    def _load_from_dir(self, base_dir: Path, *, allow_override: bool) -> None:
        if not base_dir.exists():
            return
        for skill_file in sorted(base_dir.rglob("SKILL.md")):
            text = skill_file.read_text(encoding="utf-8")
            meta, body = self._parse_skill_markdown(text)
            name = meta.get("name", skill_file.parent.name)
            if not allow_override and name in self.skills:
                continue
            self.skills[name] = {"meta": meta, "body": body}

    def reload(self) -> None:
        """Reload skills from built-in templates and workspace."""
        self.skills.clear()
        # Built-ins provide fallback skill content.
        self._load_from_dir(self.builtin_skills_dir, allow_override=False)
        # Workspace skills override built-ins with the same name.
        self._load_from_dir(self.skills_dir, allow_override=True)

    def descriptions(self) -> str:
        if not self.skills:
            return "(no skills)"
        return "\n".join(
            f"  - {n}: {s['meta'].get('description', '-')}"
            for n, s in self.skills.items()
        )

    def load(self, name: str) -> str:
        s = self.skills.get(name)
        if not s:
            available = ", ".join(sorted(self.skills.keys()))
            return f"Error: Unknown skill '{name}'. Available: {available}"
        return f'<skill name="{name}">\n{s["body"]}\n</skill>'
