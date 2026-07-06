from pathlib import Path

from edgebot.agent.memory import DreamProcessor, MemoryStore, _DreamEditTool, _DreamReadTool
from edgebot.tools.registry import ToolRegistry


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_dream_edit_rejects_non_memory_files(monkeypatch, tmp_path: Path) -> None:
    import edgebot.tools.base as tool_base

    monkeypatch.setattr(tool_base, "WORKDIR", tmp_path)
    readme = tmp_path / "README.md"
    loop_file = tmp_path / "edgebot" / "agent" / "loop.py"
    _write(readme, "original readme")
    _write(loop_file, "original loop")

    tool = _DreamEditTool(tmp_path)

    for path in (
        "README.md",
        "edgebot/agent/loop.py",
        "./.edgebot/../README.md",
        str(readme),
        ".edgebot\\..\\README.md",
    ):
        result = tool.execute(path=path, old_text="original", new_text="changed")

        assert str(result).startswith("Error:")

    assert readme.read_text(encoding="utf-8") == "original readme"
    assert loop_file.read_text(encoding="utf-8") == "original loop"


def test_dream_edit_allows_memory_files(monkeypatch, tmp_path: Path) -> None:
    import edgebot.tools.base as tool_base

    monkeypatch.setattr(tool_base, "WORKDIR", tmp_path)
    user_file = tmp_path / ".edgebot" / "USER.md"
    soul_file = tmp_path / ".edgebot" / "SOUL.md"
    memory_file = tmp_path / ".edgebot" / "memory" / "MEMORY.md"
    _write(user_file, "- Name: old")
    _write(soul_file, "- Tone: old")
    _write(memory_file, "- Fact: old")

    tool = _DreamEditTool(tmp_path)

    assert "Successfully edited" in str(tool.execute(path=".edgebot/USER.md", old_text="old", new_text="new"))
    assert "Successfully edited" in str(tool.execute(path=str(soul_file), old_text="old", new_text="new"))
    assert "Successfully edited" in str(tool.execute(path=".edgebot\\memory\\MEMORY.md", old_text="old", new_text="new"))

    assert user_file.read_text(encoding="utf-8") == "- Name: new"
    assert soul_file.read_text(encoding="utf-8") == "- Tone: new"
    assert memory_file.read_text(encoding="utf-8") == "- Fact: new"


def test_dream_read_returns_content_after_global_read_dedup(monkeypatch, tmp_path: Path) -> None:
    import edgebot.tools.base as tool_base
    from edgebot.tools import file_state
    from edgebot.tools.filesystem import run_read

    monkeypatch.setattr(tool_base, "WORKDIR", tmp_path)
    file_state.clear()
    memory_file = tmp_path / ".edgebot" / "memory" / "MEMORY.md"
    _write(memory_file, "- Durable fact: current")

    assert "- Durable fact: current" in run_read(".edgebot/memory/MEMORY.md")
    assert run_read(".edgebot/memory/MEMORY.md").startswith("[File unchanged")

    result = _DreamReadTool(tmp_path).execute(path=".edgebot/memory/MEMORY.md")

    assert "- Durable fact: current" in str(result)
    assert not str(result).startswith("[File unchanged")


def test_dream_read_schema_exposes_force_parameter() -> None:
    schema = _DreamReadTool(Path.cwd()).parameters

    assert "force" in schema["properties"]
    assert schema["properties"]["force"]["type"] == "boolean"


def test_dream_builds_restricted_tool_registry(tmp_path: Path) -> None:
    processor = DreamProcessor(
        MemoryStore(tmp_path),
        provider=object(),
        emit_output=False,
    )

    registry = processor._build_dream_tools()

    assert isinstance(registry, ToolRegistry)
    assert sorted(registry.tool_names) == ["edit_file", "read_file", "write_file"]
    assert registry.get("bash") is None
