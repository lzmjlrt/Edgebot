from pathlib import Path

from edgebot.agent import context


def test_project_instructions_follow_active_directory_hierarchy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    task_dir = workspace / "packages" / "api"
    task_dir.mkdir(parents=True)
    root_instructions = workspace / "AGENTS.md"
    nested_instructions = workspace / "packages" / "AGENTS.md"
    root_instructions.write_text("root instructions", encoding="utf-8")
    nested_instructions.write_text("package instructions", encoding="utf-8")

    sections = context.discover_project_instruction_sections(
        workspace=workspace,
        active_path=task_dir / "handler.py",
    )

    assert [section.source for section in sections] == [
        "Project instructions: AGENTS.md",
        "Project instructions: packages/AGENTS.md",
    ]
    assert [section.content for section in sections] == [
        "## Project Instructions: AGENTS.md\n\nroot instructions",
        "## Project Instructions: packages/AGENTS.md\n\npackage instructions",
    ]


def test_project_instruction_edits_are_visible_on_the_next_prompt_build(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instructions = workspace / "AGENTS.md"
    instructions.write_text("first version", encoding="utf-8")
    monkeypatch.setattr(context, "WORKDIR", workspace)
    monkeypatch.setattr(
        context,
        "_BOOTSTRAP_PATHS",
        {filename: tmp_path / "runtime" / filename for filename in context.BOOTSTRAP_FILES},
    )

    class FakeMemory:
        def get_memory_context(self):
            return ""

        def get_last_dream_cursor(self):
            return 0

        def read_unprocessed_history(self, *args, **kwargs):
            return []

    class FakeSkills:
        def reload(self):
            pass

        def get_always_skills(self):
            return []

        def build_skills_summary(self, exclude=None):
            return "(no skills)"

    from edgebot.agent import memory
    from edgebot.tools import registry

    monkeypatch.setattr(memory, "_STORE", FakeMemory())
    monkeypatch.setattr(registry, "SKILLS", FakeSkills())

    first_prompt = context.build_system_prompt()
    instructions.write_text("updated version", encoding="utf-8")
    second_prompt = context.build_system_prompt()

    assert "first version" in first_prompt
    assert "updated version" not in first_prompt
    assert "updated version" in second_prompt
    assert "first version" not in second_prompt


def test_system_prompt_source_precedence_is_explicit_and_ordered(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("project rules", encoding="utf-8")
    runtime_agents = tmp_path / "runtime" / "AGENTS.md"
    runtime_agents.parent.mkdir()
    runtime_agents.write_text("user rules", encoding="utf-8")
    monkeypatch.setattr(context, "WORKDIR", workspace)
    monkeypatch.setattr(
        context,
        "_BOOTSTRAP_PATHS",
        {
            "AGENTS.md": runtime_agents,
            **{
                filename: tmp_path / "runtime" / filename
                for filename in context.BOOTSTRAP_FILES
                if filename != "AGENTS.md"
            },
        },
    )

    class FakeMemory:
        def get_memory_context(self):
            return ""

        def get_last_dream_cursor(self):
            return 0

        def read_unprocessed_history(self, *args, **kwargs):
            return []

    class FakeSkills:
        def reload(self):
            pass

        def get_always_skills(self):
            return ["always"]

        def load_skills_for_context(self, names):
            return "skill rules"

        def build_skills_summary(self, exclude=None):
            return "(no skills)"

    from edgebot.agent import memory
    from edgebot.tools import registry

    monkeypatch.setattr(memory, "_STORE", FakeMemory())
    monkeypatch.setattr(registry, "SKILLS", FakeSkills())

    sections = context.build_system_prompt_sections(
        mcp_instructions="MCP rules",
        append_prompt="append rules",
    )

    assert context.SYSTEM_PROMPT_PRECEDENCE == (
        "Built-in safety",
        "Runtime identity",
        "Project instructions",
        "Runtime user configuration",
        "Always skills",
        "MCP instructions",
        "Append prompt",
    )
    sources = [section.source for section in sections]
    assert sources.index("Runtime identity") < sources.index(
        "Project instructions: AGENTS.md",
    )
    assert sources.index("Project instructions: AGENTS.md") < sources.index("AGENTS.md")
    assert sources.index("AGENTS.md") < sources.index("Always skill: always")
    assert sources.index("Always skill: always") < sources.index("MCP instructions")
    assert sources.index("MCP instructions") < sources.index("Append prompt")
    assert "When source instructions conflict, follow this order" in sections[0].content


def test_workspace_agents_file_is_not_migrated_into_runtime_user_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from edgebot import config
    from edgebot.agent import workspace_setup

    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("live project rules", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", workspace)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "SKILLS_DIR", runtime / "skills")
    monkeypatch.setattr(config, "MCP_CONFIG_PATH", runtime / "mcp_servers.json")
    monkeypatch.setattr(config, "AGENTS_MD_PATH", runtime / "AGENTS.md")
    monkeypatch.setattr(config, "SOUL_MD_PATH", runtime / "SOUL.md")
    monkeypatch.setattr(config, "USER_MD_PATH", runtime / "USER.md")
    monkeypatch.setattr(config, "TOOLS_MD_PATH", runtime / "TOOLS.md")
    monkeypatch.setattr(config, "HEARTBEAT_MD_PATH", runtime / "HEARTBEAT.md")
    monkeypatch.setattr(workspace_setup, "_seed_runtime_config", lambda: None)

    workspace_setup.seed_workspace_templates()

    assert (runtime / "AGENTS.md").read_text(encoding="utf-8") != "live project rules"
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "live project rules"
