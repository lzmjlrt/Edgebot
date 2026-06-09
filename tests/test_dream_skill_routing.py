from pathlib import Path

from edgebot.agent import memory
from edgebot.agent.memory import DreamProcessor, MemoryStore, _DreamEditTool
from edgebot.providers.base import LLMResponse


class FakeProvider:
    async def chat_with_retry(self, **kwargs):
        return LLMResponse(content="[SKIP] no-op", finish_reason="stop")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_phase1_actionable_findings_include_skill_tags() -> None:
    analysis = "\n".join(
        [
            "[MEMORY] Project uses uv for tests",
            "[SKILL] pytest workflow: run uv run pytest before commit",
            "[skill-remove] obsolete npm test workflow",
            "[SKIP] transient shell output",
        ]
    )

    assert memory._extract_actionable_findings(analysis) == "\n".join(
        [
            "[MEMORY] Project uses uv for tests",
            "[SKILL] pytest workflow: run uv run pytest before commit",
            "[SKILL-REMOVE] obsolete npm test workflow",
        ]
    )


def test_dream_toolset_includes_skill_write_tool(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, memory_dir=tmp_path / ".edgebot" / "memory")
    processor = DreamProcessor(store, FakeProvider(), emit_output=False)

    tools, handlers = processor._build_dream_tools()
    names = [tool["function"]["name"] for tool in tools]

    assert "write_file" in names
    assert "write_file" in handlers


def test_dream_write_only_creates_skill_markdown(monkeypatch, tmp_path: Path) -> None:
    import edgebot.tools.base as tool_base

    monkeypatch.setattr(tool_base, "WORKDIR", tmp_path)
    tool_cls = getattr(memory, "_DreamWriteTool", None)
    assert tool_cls is not None
    tool = tool_cls(tmp_path, skills_dir=tmp_path / ".edgebot" / "skills")

    result = tool.execute(
        path=".edgebot/skills/pytest-workflow/SKILL.md",
        content="# Pytest Workflow\n\nRun `uv run pytest` before commit.\n",
    )
    assert "Successfully wrote" in str(result)
    assert (tmp_path / ".edgebot" / "skills" / "pytest-workflow" / "SKILL.md").exists()

    for path in (
        ".edgebot/skills/pytest-workflow/README.md",
        ".edgebot/skills/pytest-workflow/nested/SKILL.md",
        ".edgebot/skills/../memory/MEMORY.md",
        "README.md",
    ):
        blocked = tool.execute(path=path, content="blocked")
        assert str(blocked).startswith("Error:")


def test_dream_write_refuses_to_overwrite_existing_skill(monkeypatch, tmp_path: Path) -> None:
    import edgebot.tools.base as tool_base

    monkeypatch.setattr(tool_base, "WORKDIR", tmp_path)
    skill_file = tmp_path / ".edgebot" / "skills" / "pytest-workflow" / "SKILL.md"
    _write(skill_file, "existing skill")
    tool = memory._DreamWriteTool(tmp_path, skills_dir=tmp_path / ".edgebot" / "skills")

    result = tool.execute(
        path=".edgebot/skills/pytest-workflow/SKILL.md",
        content="replacement",
    )

    assert str(result).startswith("Error:")
    assert skill_file.read_text(encoding="utf-8") == "existing skill"


def test_dream_edit_allows_existing_skill_markdown_only(monkeypatch, tmp_path: Path) -> None:
    import edgebot.tools.base as tool_base

    monkeypatch.setattr(tool_base, "WORKDIR", tmp_path)
    skill_file = tmp_path / ".edgebot" / "skills" / "pytest-workflow" / "SKILL.md"
    readme_file = tmp_path / ".edgebot" / "skills" / "pytest-workflow" / "README.md"
    _write(skill_file, "# Pytest Workflow\n\nOld command\n")
    _write(readme_file, "Old readme\n")

    tool = _DreamEditTool(
        tmp_path,
        allowed_files=(),
        allowed_skill_dir=tmp_path / ".edgebot" / "skills",
    )

    assert "Successfully edited" in str(
        tool.execute(
            path=".edgebot/skills/pytest-workflow/SKILL.md",
            old_text="Old command",
            new_text="New command",
        )
    )
    assert "New command" in skill_file.read_text(encoding="utf-8")

    blocked = tool.execute(
        path=".edgebot/skills/pytest-workflow/README.md",
        old_text="Old readme",
        new_text="New readme",
    )
    assert str(blocked).startswith("Error:")
    assert readme_file.read_text(encoding="utf-8") == "Old readme\n"
