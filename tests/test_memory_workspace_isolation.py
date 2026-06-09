from pathlib import Path

from edgebot.agent.memory import MemoryStore


def test_memory_store_default_paths_are_derived_from_workspace(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)

    assert store.memory_dir == tmp_path / ".edgebot" / "memory"
    assert store.memory_file == tmp_path / ".edgebot" / "memory" / "MEMORY.md"
    assert store.history_file == tmp_path / ".edgebot" / "memory" / "history.jsonl"
    assert store.cursor_file == tmp_path / ".edgebot" / "memory" / ".cursor"
    assert store.dream_cursor_file == tmp_path / ".edgebot" / "memory" / ".dream_cursor"
    assert store.user_file == tmp_path / ".edgebot" / "USER.md"
    assert store.soul_file == tmp_path / ".edgebot" / "SOUL.md"
    assert store.skills_dir == tmp_path / ".edgebot" / "skills"
    assert store.memory_dir.exists()


def test_memory_store_default_paths_keep_workspaces_isolated(tmp_path: Path) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    first = MemoryStore(workspace_a)
    second = MemoryStore(workspace_b)

    first.append_history("first workspace", session_key="a")
    second.append_history("second workspace", session_key="b")

    assert first.history_file != second.history_file
    assert "first workspace" in first.history_file.read_text(encoding="utf-8")
    assert "second workspace" not in first.history_file.read_text(encoding="utf-8")
    assert "second workspace" in second.history_file.read_text(encoding="utf-8")
    assert "first workspace" not in second.history_file.read_text(encoding="utf-8")
    assert first.cursor_file.read_text(encoding="utf-8") == "1"
    assert second.cursor_file.read_text(encoding="utf-8") == "1"
