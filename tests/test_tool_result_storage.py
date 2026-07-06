from pathlib import Path

from edgebot.agent.tool_results import ToolResultPolicy, prepare_tool_result_content


def test_tool_result_storage_offloads_large_non_read_outputs(tmp_path: Path) -> None:
    output = "A" * 5000

    content = prepare_tool_result_content(
        output,
        tool_name="bash",
        tool_call_id="../call one",
        policy=ToolResultPolicy(
            max_chars=1000,
            session_key="chat/main",
            root=tmp_path / "tool-results",
        ),
    )

    stored = tmp_path / "tool-results" / "chat_main" / "call_one.txt"
    assert stored.read_text(encoding="utf-8") == output
    assert "Tool result offloaded" in content
    assert f"Path: {stored}" in content
    assert len(content) < 1800


def test_tool_result_storage_truncates_large_read_file_outputs(tmp_path: Path) -> None:
    content = prepare_tool_result_content(
        "B" * 1200,
        tool_name="read_file",
        tool_call_id="call_read",
        policy=ToolResultPolicy(
            max_chars=1000,
            session_key="chat/main",
            root=tmp_path / "tool-results",
        ),
    )

    assert content == ("B" * 1000) + "\n...[truncated]"
    assert not (tmp_path / "tool-results").exists()
