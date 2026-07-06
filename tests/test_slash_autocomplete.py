from pathlib import Path
from typing import Any

from prompt_toolkit.document import Document

from edgebot.cli import repl
from edgebot.cli.slash_autocomplete import (
    SlashCommand,
    SlashCommandCompleter,
    SlashCommandRegistry,
    build_default_slash_registry,
    resolve_skill_slash_prompt,
)


class FakeSkillLoader:
    def __init__(self):
        self.reload_count = 0

    def reload(self):
        self.reload_count += 1

    def list_skills(self, filter_unavailable: bool = False):
        return [
            {"name": "docker-expert", "source": "builtin"},
            {"name": "frontend-design", "source": "workspace"},
        ]

    def get_skill_metadata(self, name: str):
        return {
            "docker-expert": {
                "description": "Docker containerization expert",
            },
            "frontend-design": {
                "description": "Create frontend interfaces",
            },
        }.get(name, {})


class MutableSkillLoader:
    def __init__(self):
        self.names = ["docker-expert"]
        self.reload_count = 0

    def reload(self):
        self.reload_count += 1

    def list_skills(self, filter_unavailable: bool = False) -> list[dict[str, Any]]:
        return [{"name": name, "source": "workspace"} for name in self.names]

    def get_skill_metadata(self, name: str) -> dict[str, str]:
        return {"description": f"{name} description"}


def _completion_texts(completer: SlashCommandCompleter, text: str) -> list[str]:
    document = Document(text, cursor_position=len(text))
    return [completion.text for completion in completer.get_completions(document, None)]


def test_registry_filters_case_insensitive_prefix_and_fuzzy_word_initials() -> None:
    registry = SlashCommandRegistry([
        SlashCommand("review", "Review a pull request", category="Code Quality"),
        SlashCommand("code-review", "Code review with quality checks", category="Code Quality"),
        SlashCommand("security-review", "Complete a security review", category="Code Quality"),
        SlashCommand("sessions", "List saved sessions", category="Session"),
    ])

    rev_matches = [cmd.name for cmd in registry.search("REV")]

    assert rev_matches[0] == "review"
    assert "code-review" in rev_matches
    assert [cmd.name for cmd in registry.search("cr")][:1] == ["code-review"]
    assert "security-review" in [cmd.name for cmd in registry.search("sr")]


def test_completer_only_activates_for_slash_command_token() -> None:
    registry = SlashCommandRegistry([
        SlashCommand("review", "Review a pull request"),
        SlashCommand("code-review", "Code review with quality checks", shortcut="/cr"),
    ])
    completer = SlashCommandCompleter(registry)

    assert _completion_texts(completer, "please/re") == []
    assert _completion_texts(completer, "/re")[0] == "review "
    assert _completion_texts(completer, "please /re")[0] == "review "
    assert _completion_texts(completer, "/cr") == ["code-review "]


def test_default_registry_includes_builtin_commands_and_skills() -> None:
    loader = FakeSkillLoader()
    registry = build_default_slash_registry(loader)

    commands = {cmd.name: cmd for cmd in registry.get_all_commands()}

    assert loader.reload_count == 1
    assert commands["help"].description == "Show this help"
    assert commands["compact"].source == "built-in"
    assert commands["docker-expert"].source == "skill"
    assert commands["frontend-design"].description == "Create frontend interfaces"


def test_skill_slash_command_builds_load_skill_prompt() -> None:
    loader = FakeSkillLoader()
    prompt = resolve_skill_slash_prompt(
        "/frontend-design polish the dashboard",
        loader,
    )

    assert loader.reload_count == 1
    assert prompt == (
        "Use `load_skill` to load the `frontend-design` skill, then follow that "
        "skill for this request:\n\npolish the dashboard"
    )


def test_skill_slash_command_ignores_unknown_or_non_slash_text() -> None:
    assert resolve_skill_slash_prompt("hello /frontend-design", FakeSkillLoader()) is None
    assert resolve_skill_slash_prompt("/missing do work", FakeSkillLoader()) is None


def test_completer_can_refresh_registry_for_newly_installed_skills() -> None:
    loader = MutableSkillLoader()
    completer = SlashCommandCompleter(lambda: build_default_slash_registry(loader))

    loader.names.append("frontend-design")

    assert "frontend-design " in _completion_texts(completer, "/front")
    assert loader.reload_count >= 1


def test_prompt_session_is_configured_with_slash_completer(monkeypatch) -> None:
    captured = {}

    class FakePromptSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(repl, "PromptSession", FakePromptSession)
    monkeypatch.setattr(repl, "_prompt_session", None)
    monkeypatch.setattr(repl, "_HISTORY_PATH", Path("history"))

    session = repl._prompt()

    assert session is repl._prompt_session
    assert isinstance(captured["completer"], SlashCommandCompleter)
    assert captured["complete_while_typing"] is True
    assert captured["reserve_space_for_menu"] >= 8
