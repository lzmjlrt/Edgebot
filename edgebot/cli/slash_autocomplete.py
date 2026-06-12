"""Slash command discovery and prompt_toolkit completion support."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from prompt_toolkit.completion import Completer, Completion


@dataclass(frozen=True)
class SlashCommand:
    """Metadata used to display and match an interactive slash command."""

    name: str
    description: str
    source: str = "built-in"
    category: str = "General"
    aliases: tuple[str, ...] = field(default_factory=tuple)
    shortcut: str | None = None

    @property
    def display_name(self) -> str:
        return f"/{self.name}"


@dataclass(frozen=True)
class _ScoredCommand:
    command: SlashCommand
    score: int
    source_rank: int


class SlashCommandRegistry:
    """In-memory command registry with fuzzy, case-insensitive search."""

    def __init__(self, commands: Iterable[SlashCommand] | None = None):
        self._commands: dict[str, SlashCommand] = {}
        for command in commands or []:
            self.register(command)

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name] = command

    def get_all_commands(self) -> list[SlashCommand]:
        return list(self._commands.values())

    def search(self, partial: str, *, limit: int = 50) -> list[SlashCommand]:
        query = partial.strip().lstrip("/").lower()
        scored: list[_ScoredCommand] = []
        for index, command in enumerate(self._commands.values()):
            score = _match_score(query, command)
            if score is None:
                continue
            source_rank = _source_rank(command.source) * 1000 + index
            scored.append(_ScoredCommand(command, score, source_rank))
        scored.sort(key=lambda item: (-item.score, item.source_rank, item.command.name))
        return [item.command for item in scored[:limit]]


class SlashCommandCompleter(Completer):
    """prompt_toolkit completer activated by slash command tokens."""

    def __init__(
        self,
        registry: SlashCommandRegistry | Callable[[], SlashCommandRegistry],
        *,
        limit: int = 15,
    ):
        self._registry = registry
        self.limit = limit

    def get_completions(self, document, complete_event):
        context = _slash_context(document.text_before_cursor)
        if context is None:
            return

        start, partial = context
        for command in self._current_registry().search(partial, limit=self.limit):
            yield Completion(
                text=f"{command.name} ",
                start_position=start - len(document.text_before_cursor),
                display=_display_text(command),
                display_meta=_display_meta(command),
                style=_style_for_source(command.source),
            )

    def _current_registry(self) -> SlashCommandRegistry:
        if callable(self._registry):
            return self._registry()
        return self._registry


def build_default_slash_registry(skill_loader) -> SlashCommandRegistry:
    """Build the CLI slash registry from built-ins and known skills."""

    _reload_skills(skill_loader)

    registry = SlashCommandRegistry(_builtin_commands())
    for entry in skill_loader.list_skills(filter_unavailable=False):
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        metadata = skill_loader.get_skill_metadata(name) or {}
        description = metadata.get("description")
        registry.register(
            SlashCommand(
                name=name,
                description=str(description or f"Load skill: {name}"),
                source="skill",
                category="Installed Skills",
            )
        )
    return registry


def resolve_skill_slash_prompt(query: str, skill_loader) -> str | None:
    """Translate a known skill slash command into an agent instruction."""

    match = re.match(r"^/([A-Za-z0-9_-]+)(?:\s+(.*))?$", query.strip(), re.DOTALL)
    if not match:
        return None

    skill_name = match.group(1)
    _reload_skills(skill_loader)
    available = {
        str(entry.get("name", "")).strip()
        for entry in skill_loader.list_skills(filter_unavailable=False)
    }
    if skill_name not in available:
        return None

    request = (match.group(2) or "").strip()
    if request:
        return (
            f"Use `load_skill` to load the `{skill_name}` skill, then follow that "
            f"skill for this request:\n\n{request}"
        )
    return (
        f"Use `load_skill` to load the `{skill_name}` skill, then summarize how "
        "to use it."
    )


def _reload_skills(skill_loader) -> None:
    reload_skills = getattr(skill_loader, "reload", None)
    if callable(reload_skills):
        reload_skills()


def _builtin_commands() -> list[SlashCommand]:
    return [
        SlashCommand("new", "Start a new conversation", category="Conversation"),
        SlashCommand("sessions", "List saved sessions", category="Conversation"),
        SlashCommand("resume", "Resume a saved session", category="Conversation"),
        SlashCommand("compact", "Compress conversation context", category="Conversation"),
        SlashCommand("memory", "Run memory consolidation now", category="Memory"),
        SlashCommand("dream-log", "Show the latest Dream memory change", category="Memory"),
        SlashCommand("dream-restore", "Restore a Dream memory version", category="Memory"),
        SlashCommand("cron", "Show or manage cron jobs", category="Automation"),
        SlashCommand("heartbeat", "Trigger one heartbeat tick now", category="Automation"),
        SlashCommand("mcp", "Show MCP servers and loaded capabilities", category="Tools"),
        SlashCommand("tasks", "Show task board", category="Tools"),
        SlashCommand("bg", "Inspect background tasks", category="Tools"),
        SlashCommand("subagents", "List or control subagents", category="Tools"),
        SlashCommand("permissions", "Show permission rules", category="Security"),
        SlashCommand("status", "Show current session info", category="Conversation"),
        SlashCommand("help", "Show this help", category="Help"),
        SlashCommand("exit", "Quit", category="Help"),
    ]


def _slash_context(text_before_cursor: str) -> tuple[int, str] | None:
    match = re.search(r"(^|\s)/([A-Za-z0-9_-]*)$", text_before_cursor)
    if not match:
        return None
    return match.start(2), match.group(2)


def _match_score(query: str, command: SlashCommand) -> int | None:
    if not query:
        return 100

    targets = [command.name, *command.aliases]
    if command.shortcut:
        targets.append(command.shortcut.lstrip("/"))

    best: int | None = None
    for target in targets:
        target_score = _target_score(query, target.lower())
        if target_score is None:
            continue
        best = target_score if best is None else max(best, target_score)
    return best


def _target_score(query: str, target: str) -> int | None:
    if target == query:
        return 1000
    if target.startswith(query):
        return 900 - len(target)

    initials = "".join(word[0] for word in re.split(r"[-_\s]+", target) if word)
    if initials.startswith(query):
        return 800 - len(target)
    if query in target:
        return 700 - target.index(query)

    positions = _fuzzy_positions(query, target)
    if positions is None:
        return None
    gaps = sum((right - left - 1) for left, right in zip(positions, positions[1:]))
    return 500 - gaps - positions[0]


def _fuzzy_positions(query: str, target: str) -> list[int] | None:
    positions: list[int] = []
    search_from = 0
    for char in query:
        idx = target.find(char, search_from)
        if idx < 0:
            return None
        positions.append(idx)
        search_from = idx + 1
    return positions


def _source_rank(source: str) -> int:
    if source == "built-in":
        return 0
    if source == "skill":
        return 1
    return 2


def _display_text(command: SlashCommand) -> str:
    suffix = f"  {command.shortcut}" if command.shortcut else ""
    return f"{command.display_name}{suffix}"


def _display_meta(command: SlashCommand) -> str:
    return command.description


def _style_for_source(source: str) -> str:
    if source == "skill":
        return "fg:ansigreen"
    if source == "custom":
        return "fg:ansiyellow"
    return "fg:ansiblue"
