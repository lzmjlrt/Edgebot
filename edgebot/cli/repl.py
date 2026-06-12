"""
edgebot/cli/repl.py - Interactive REPL (async) with Rich UI and prompt_toolkit.

REPL commands: /new /sessions /resume /compact /memory /dream-log /dream-restore /cron /heartbeat /mcp /tasks /bg /subagents /permissions /status /help
"""

import asyncio
import json
import re
import shlex
import time
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.keys import Keys
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from edgebot.agent.compression import auto_compact, extract_session_summary
from edgebot.agent.context import build_system_prompt, seed_workspace_templates
from edgebot.agent.loop import agent_loop, get_autocompact
from edgebot.agent.memory import MemoryStore, cleanup_memory_files_once, consolidate_memory
from edgebot.cron.service import _get_croniter
from edgebot.cron.types import CronSchedule
from edgebot.config import (
    HEARTBEAT_INTERVAL_SECONDS,
    IDLE_COMPACT_MINUTES,
    LEGACY_SESSION_DIR,
    MCP_CONFIG_PATH,
    MEMORY_CONSOLIDATION_INTERVAL_SECONDS,
    MODEL,
    SESSION_DIR,
    WORKDIR,
)
from edgebot.cron.types import CronJob, CronPayload
from edgebot.heartbeat.service import HeartbeatService
from edgebot.mcp.loader import load_mcp
from edgebot.session.store import SessionStore
from edgebot.cli.slash_autocomplete import (
    SlashCommandCompleter,
    build_default_slash_registry,
    resolve_skill_slash_prompt,
)
from edgebot.tools.builtin.ask import AskOption, AskQuestion, build_ask_user_result, set_ask_handler
from edgebot.tools.registry import (
    BG,
    CRON,
    SKILLS,
    SUBAGENT,
    TASK_MGR,
    TODO,
    TOOL_HANDLERS,
    TOOLS,
    set_batch_permission_prompt_handler,
    set_permission_prompt_handler,
)

console = Console()
_MEMORY = MemoryStore(Path.cwd())

_HISTORY_PATH = Path.home() / ".edgebot" / "cli_history"
_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
_prompt_session: PromptSession | None = None


def _prompt() -> PromptSession:
    global _prompt_session
    if _prompt_session is None:
        _prompt_session = PromptSession(
            history=FileHistory(str(_HISTORY_PATH)),
            completer=SlashCommandCompleter(lambda: build_default_slash_registry(SKILLS)),
            complete_while_typing=True,
            reserve_space_for_menu=10,
        )
    return _prompt_session


async def _ask_user() -> str:
    """Prompt the user for a single line; returns stripped text."""
    with patch_stdout():
        line = await _prompt().prompt_async(
            HTML("<b><ansiblue>You:</ansiblue></b> ")
        )
    return (line or "").strip()


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can display it safely."""
    ansi_console = Console(
        force_terminal=True,
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


async def _interactive_print(render_fn) -> None:
    """Print async updates without corrupting the active prompt line."""
    def _write() -> None:
        ansi = _render_interactive_ansi(render_fn)
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _interactive_notice(text: str) -> None:
    await _interactive_print(lambda c: c.print(f"[dim]{escape(text)}[/dim]"))


async def _interactive_response(label: str, body: str) -> None:
    safe_label = escape(label)
    safe_body = escape(body)
    await _interactive_print(
        lambda c: (
            c.print(f"[cyan]{safe_label}[/cyan]"),
            c.print(safe_body, highlight=False, soft_wrap=True),
            c.print(),
        )
    )


async def _permission_prompt(request: dict) -> dict | None:
    result = await _run_permission_prompt(request)
    return result if isinstance(result, dict) else {"action": "deny"}


async def _batch_permission_prompt(requests: list[dict]) -> dict | None:
    result = await _run_batch_permission_prompt(requests)
    return result if isinstance(result, dict) else {"action": "deny_all"}


def _permission_title(request: dict) -> str:
    tool = str(request.get("tool", "")).strip()
    if tool == "bash":
        return "Bash command"
    if tool == "write_file":
        return "Write file"
    if tool == "edit_file":
        return "Edit file"
    if tool == "background_run":
        return "Background task"
    if tool == "web_fetch":
        return "Web fetch"
    if tool == "web_search":
        return "Web search"
    if tool == "task":
        return "Subagent"
    return f"{tool or 'Tool'} request"


def _permission_subject(request: dict) -> str:
    tool = str(request.get("tool", "")).strip()
    if tool == "bash":
        return str(request.get("raw_command") or request.get("scope_value") or "").strip()
    message = str(request.get("message", "")).strip()
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if len(lines) >= 2:
        return lines[-1]
    return str(request.get("scope_value") or tool or "this operation").strip()


def _permission_description(request: dict) -> str:
    description = str(request.get("description") or "").strip()
    if description:
        return description
    tool = str(request.get("tool", "")).strip()
    if tool == "bash":
        command = _permission_subject(request)
        if "git reset --hard" in command.lower():
            return "Reset git history or working tree state (destructive operation)"
        if re.search(r">\s*\S+", command):
            return "Write command output to a file"
        if request.get("requires_confirmation"):
            return "Sensitive shell operation"
        return "Shell command requested by Edgebot"
    if tool in {"write_file", "edit_file"}:
        return "Modify file contents"
    if tool == "background_run":
        return "Start a background command"
    if tool in {"web_fetch", "web_search"}:
        return "Network access requested by Edgebot"
    if tool == "task":
        return "Start a delegated agent task"
    return "Sensitive tool operation"


def _permission_scope_label(request: dict) -> str:
    tool = str(request.get("tool", "")).strip()
    scope = str(request.get("scope_hint") or "allow_tool")
    subject = _permission_subject(request)
    if tool == "bash":
        if scope == "allow_program":
            program = str(request.get("scope_value") or "").strip()
            return program or subject or "this command"
        pattern = _bash_permission_pattern(subject)
        return pattern or subject or "this command"
    if tool in {"write_file", "edit_file"}:
        try:
            path = Path(subject)
            if not path.is_absolute():
                first = path.parts[0] if path.parts else subject
                return f"{first}{path.anchor or ''} from this project"
        except (OSError, ValueError):
            pass
    return subject or str(request.get("scope_value") or tool or "this tool")


def _bash_permission_pattern(command: str) -> str:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    head = tokens[0]
    if head.lower() == "git" and len(tokens) >= 2:
        return f"git {tokens[1]} *"
    return f"{head} *"


async def _run_permission_prompt(request: dict) -> dict:
    selected_index = 0
    save_index = 0
    mode = "select"
    feedback = ""
    tool = str(request.get("tool", "")).strip()
    subject = _permission_subject(request)
    amended_subject = subject
    title = _permission_title(request)
    description = _permission_description(request)
    scope = str(request.get("scope_hint") or "allow_tool")
    scope_label = _permission_scope_label(request)
    requires_confirmation = bool(request.get("requires_confirmation"))
    confirmed = not requires_confirmation
    confirmation_input = ""

    options = [
        ("Yes", "allow_once"),
        (f"Yes, and don't ask again for: {scope_label}", "allow_always"),
        ("No", "deny"),
    ]
    save_options = [
        ("Save to project settings (.claude/settings.json)", "project"),
        ("Save to user settings (~/.claude/settings.json)", "user"),
        ("Cancel - don't save, allow once", "cancel"),
    ]

    def _render_permission():
        fragments: list[tuple[str, str]] = []
        if requires_confirmation and not confirmed:
            fragments.extend([
                ("class:rule", "-" * 72 + "\n"),
                ("class:danger", "HIGH RISK COMMAND\n\n"),
                ("class:title", f"{title}\n\n"),
                ("class:item", f"    {subject}\n"),
                ("class:desc", f"    {description}\n\n"),
                ("class:item", "This command needs an extra confirmation.\n"),
                ("class:item", "Type the command exactly to continue:\n"),
                ("class:input_label", "> "),
                ("class:input", confirmation_input + "|\n\n"),
                ("class:hint", "Esc to cancel\n"),
            ])
            return fragments
        fragments.extend([
            ("class:rule", "-" * 72 + "\n"),
            ("class:header", f"{title}\n\n"),
            ("class:item", f"    {subject}\n"),
            ("class:desc", f"    {description}\n\n"),
        ])
        if requires_confirmation:
            fragments.append(("class:danger", "High risk command confirmed.\n\n"))
        fragments.extend([
            ("class:item", "This command requires approval\n\n"),
        ])
        if mode == "explain":
            fragments.extend([
                ("class:item", "Tell Edgebot what to do differently:\n"),
                ("class:input_label", "> "),
                ("class:input", feedback + "|\n\n"),
                ("class:hint", "Enter to send · Esc to cancel\n"),
            ])
            return fragments
        if mode == "save":
            rule_preview = str(request.get("rule_preview") or scope_label or subject)
            fragments.extend([
                ("class:title", "Rule Preview\n\n"),
                ("class:item", f"    {rule_preview}\n\n"),
                ("class:desc", "Choose where to save this allow rule.\n\n"),
            ])
            for idx, (label, _target) in enumerate(save_options):
                style = "class:selected" if idx == save_index else "class:item"
                prefix = "> " if idx == save_index else "  "
                fragments.append((style, f"{prefix}{idx + 1}. {label}\n"))
            fragments.extend([
                ("", "\n"),
                ("class:hint", "Esc to cancel save · Enter to confirm\n"),
            ])
            return fragments
        if mode == "amend":
            fragments.extend([
                ("class:item", "Amend command before running:\n"),
                ("class:input_label", "> "),
                ("class:input", amended_subject + "|\n\n"),
                ("class:hint", "Enter to allow amended command · Esc to cancel\n"),
            ])
            return fragments
        fragments.append(("class:title", "Do you want to proceed?\n"))
        for idx, (label, _action) in enumerate(options):
            style = "class:selected" if idx == selected_index else "class:item"
            prefix = "> " if idx == selected_index else "  "
            fragments.append((style, f"{prefix}{idx + 1}. {label}\n"))
        fragments.extend([
            ("", "\n"),
            ("class:hint", "Esc to cancel · Tab to amend · ctrl+e to explain\n"),
        ])
        return fragments

    body = FormattedTextControl(_render_permission, focusable=True, show_cursor=False)
    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _move_up(event) -> None:
        nonlocal selected_index
        nonlocal save_index
        if not confirmed or mode != "select":
            if confirmed and mode == "save":
                save_index = (save_index - 1) % len(save_options)
                event.app.invalidate()
            return
        selected_index = (selected_index - 1) % len(options)
        event.app.invalidate()

    @kb.add("down")
    @kb.add("j")
    def _move_down(event) -> None:
        nonlocal selected_index
        nonlocal save_index
        if not confirmed or mode != "select":
            if confirmed and mode == "save":
                save_index = (save_index + 1) % len(save_options)
                event.app.invalidate()
            return
        selected_index = (selected_index + 1) % len(options)
        event.app.invalidate()

    @kb.add("tab")
    def _amend(event) -> None:
        nonlocal mode
        if confirmed:
            mode = "amend"
            event.app.invalidate()

    @kb.add("c-e")
    def _explain(event) -> None:
        nonlocal mode
        if confirmed:
            mode = "explain"
            event.app.invalidate()

    @kb.add("enter")
    def _accept(event) -> None:
        nonlocal confirmed
        nonlocal mode
        if requires_confirmation and not confirmed:
            if confirmation_input == subject:
                confirmed = True
                event.app.invalidate()
            else:
                event.app.exit(result={"action": "deny"})
            return
        if mode == "explain":
            event.app.exit(result={
                "action": "deny",
                "feedback": feedback.strip(),
            })
            return
        if mode == "save":
            target = save_options[save_index][1]
            if target == "cancel":
                event.app.exit(result={"action": "allow"})
                return
            event.app.exit(result={
                "action": "allow",
                "scope": scope,
                "persist": True,
                "save_target": target,
            })
            return
        if mode == "amend":
            updated = amended_subject.strip()
            if not updated:
                event.app.exit(result={"action": "deny"})
                return
            raw_params = request.get("params")
            updated_params = dict(raw_params) if isinstance(raw_params, dict) else {}
            result = {"action": "allow"}
            if tool in {"bash", "background_run"}:
                updated_params["command"] = updated
            else:
                updated_params["path"] = updated
            result["updated_params"] = updated_params
            event.app.exit(result=result)
            return
        action = options[selected_index][1]
        if action == "allow_once":
            event.app.exit(result={"action": "allow"})
        elif action == "allow_always":
            mode = "save"
            event.app.invalidate()
        else:
            event.app.exit(result={"action": "deny"})

    @kb.add("c-h")
    @kb.add("backspace")
    def _backspace(event) -> None:
        nonlocal feedback
        nonlocal confirmation_input
        nonlocal amended_subject
        if requires_confirmation and not confirmed:
            confirmation_input = confirmation_input[:-1]
            event.app.invalidate()
            return
        if mode == "explain":
            feedback = feedback[:-1]
            event.app.invalidate()
            return
        if mode == "amend":
            amended_subject = amended_subject[:-1]
            event.app.invalidate()

    @kb.add(Keys.Any)
    def _type_text(event) -> None:
        nonlocal feedback
        nonlocal confirmation_input
        nonlocal amended_subject
        data = event.data
        if not data or data in {"\r", "\n", "\t", "\x1b"}:
            return
        if requires_confirmation and not confirmed:
            confirmation_input += data
            event.app.invalidate()
            return
        if mode == "explain":
            feedback += data
            event.app.invalidate()
            return
        if mode == "amend":
            amended_subject += data
            event.app.invalidate()

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event) -> None:
        if mode == "save":
            event.app.exit(result={"action": "allow"})
            return
        event.app.exit(result={"action": "deny"})

    app = Application(
        layout=Layout(HSplit([Window(content=body, always_hide_cursor=True)])),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
        style=Style.from_dict({
            "rule": "ansibrightblue",
            "header": "ansibrightblue bold",
            "title": "bold",
            "hint": "ansibrightblack",
            "item": "",
            "desc": "ansibrightblack",
            "selected": "ansibrightblue bold",
            "danger": "ansired bold",
            "input_label": "ansibrightblue",
            "input": "ansiwhite",
        }),
    )

    with patch_stdout():
        result = await app.run_async()
    return result or {"action": "deny"}


async def _run_batch_permission_prompt(requests: list[dict]) -> dict:
    selected_index = 0
    options = [
        ("Approve ALL", "allow_all"),
        ("Deny ALL", "deny_all"),
        ("Review one-by-one", "review_one_by_one"),
    ]
    total = len(requests)

    def _request_summary(index: int, request: dict) -> tuple[str, str]:
        title = _permission_title(request)
        subject = _permission_subject(request)
        if not subject:
            subject = str(request.get("message") or "").strip().splitlines()[-1:] or ["this operation"]
            subject = str(subject[0]).strip()
        return title, subject

    def _render_batch_permission():
        fragments: list[tuple[str, str]] = [
            ("class:rule", "-" * 72 + "\n"),
            ("class:header", f"{total} Permissions Required\n\n"),
        ]
        for idx, request in enumerate(requests, 1):
            title, subject = _request_summary(idx, request)
            fragments.extend([
                ("class:title", f"[{idx}/{total}] {title}\n"),
                ("class:item", f"    {subject}\n"),
            ])
            description = _permission_description(request)
            if description:
                fragments.append(("class:desc", f"    {description}\n"))
            fragments.append(("", "\n"))
        fragments.append(("class:title", "How do you want to proceed?\n"))
        for idx, (label, _action) in enumerate(options):
            style = "class:selected" if idx == selected_index else "class:item"
            prefix = "> " if idx == selected_index else "  "
            fragments.append((style, f"{prefix}{idx + 1}. {label}\n"))
        fragments.extend([
            ("", "\n"),
            ("class:hint", "Esc to deny all · ↑↓/jk to navigate · Enter to confirm\n"),
        ])
        return fragments

    body = FormattedTextControl(_render_batch_permission, focusable=True, show_cursor=False)
    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _move_up(event) -> None:
        nonlocal selected_index
        selected_index = (selected_index - 1) % len(options)
        event.app.invalidate()

    @kb.add("down")
    @kb.add("j")
    def _move_down(event) -> None:
        nonlocal selected_index
        selected_index = (selected_index + 1) % len(options)
        event.app.invalidate()

    @kb.add("enter")
    def _accept(event) -> None:
        event.app.exit(result={"action": options[selected_index][1]})

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event) -> None:
        event.app.exit(result={"action": "deny_all"})

    app = Application(
        layout=Layout(HSplit([Window(content=body, always_hide_cursor=True)])),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
        style=Style.from_dict({
            "rule": "ansibrightblue",
            "header": "ansibrightblue bold",
            "title": "bold",
            "hint": "ansibrightblack",
            "item": "",
            "desc": "ansibrightblack",
            "selected": "ansibrightblue bold",
        }),
    )

    with patch_stdout():
        result = await app.run_async()
    return result or {"action": "deny_all"}


def _display_width(text: str) -> int:
    return get_cwidth(text)


def _pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _clip_display(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _display_width(text) <= width:
        return text
    if width <= 3:
        return "." * width
    clipped = ""
    used = 0
    for char in text:
        char_width = _display_width(char)
        if used + char_width > max(0, width - 3):
            break
        clipped += char
        used += char_width
    return clipped + "..."


def _preview_box(preview: str | None, width: int = 58, height: int = 8) -> list[str]:
    inner = max(12, width - 2)
    lines = (preview or "No preview available").splitlines() or [""]
    visible = lines[:height]
    hidden = max(0, len(lines) - len(visible))
    rendered = ["+" + "-" * inner + "+"]
    for line in visible:
        rendered.append("|" + _pad_display(_clip_display(line, inner), inner) + "|")
    if hidden:
        marker = f"... {hidden} lines hidden"
        rendered[-1] = "|" + _pad_display(_clip_display(marker, inner), inner) + "|"
    while len(rendered) < height + 1:
        rendered.append("|" + " " * inner + "|")
    rendered.append("+" + "-" * inner + "+")
    return rendered


async def _ask_free_text(question: str) -> str:
    safe_q = escape(question)
    await _interactive_print(
        lambda c: (
            c.print(f"[bold]{safe_q}[/bold]"),
            c.print(),
        )
    )
    with patch_stdout():
        line = await _prompt().prompt_async(
            HTML("<b><ansicyan>You:</ansicyan></b> ")
        )
    return (line or "").strip() or "(no response)"


def _answer_has_value(answer: object | None) -> bool:
    if isinstance(answer, list):
        return any(str(item).strip() for item in answer)
    if answer is None:
        return False
    return bool(str(answer).strip())


def _display_answer(answer: object | None) -> str:
    if isinstance(answer, list):
        values = [str(item).strip() for item in answer if str(item).strip()]
        return ", ".join(values) if values else "(no response)"
    value = str(answer or "").strip()
    return value or "(no response)"


async def _run_ask_questions(questions: list[AskQuestion]) -> dict[str, object]:
    """Run one multi-step ask_user picker with a final submit review."""
    if not questions:
        return {}
    if all(not question.options for question in questions):
        return {
            question.question: await _ask_free_text(question.question)
            for question in questions
        }

    selected_indices = [0 for _ in questions]
    checked: list[set[int]] = [set() for _ in questions]
    custom_inputs = ["" for _ in questions]
    answers: dict[str, object] = {}
    step_index = 0
    submit_choice = 0

    def _question_options(question_index: int) -> list[AskOption]:
        return questions[question_index].options

    def _selected_option(question_index: int) -> AskOption | None:
        options = _question_options(question_index)
        if not options:
            return None
        index = selected_indices[question_index] % len(options)
        return options[index]

    def _current_custom_active() -> bool:
        if step_index >= len(questions):
            return False
        selected = _selected_option(step_index)
        return bool(selected and selected.is_other)

    def _answer_for(question_index: int) -> object:
        question = questions[question_index]
        selected = _selected_option(question_index)
        custom = custom_inputs[question_index].strip()
        if not question.options:
            return custom or "(no response)"
        if question.multi_select:
            chosen = sorted(checked[question_index]) or [selected_indices[question_index]]
            values: list[str] = []
            for idx in chosen:
                option = question.options[idx]
                if option.is_other:
                    if custom:
                        values.append(custom)
                else:
                    values.append(option.label)
            return values or [custom or "(no response)"]
        if selected and selected.is_other:
            return custom or ""
        return selected.label if selected else custom

    def _store_current_answer() -> None:
        if step_index >= len(questions):
            return
        question = questions[step_index]
        answers[question.question] = _answer_for(step_index)

    def _current_question_complete() -> bool:
        if step_index >= len(questions):
            return True
        return _answer_has_value(_answer_for(step_index))

    def _go_to_submit() -> None:
        nonlocal step_index
        if step_index < len(questions):
            _store_current_answer()
        for idx, question in enumerate(questions):
            answers[question.question] = _answer_for(idx)
        step_index = len(questions)

    def _go_to_next_question() -> None:
        nonlocal step_index
        if step_index >= len(questions):
            return
        if not _current_question_complete():
            return
        _store_current_answer()
        if step_index + 1 < len(questions):
            step_index += 1
        else:
            _go_to_submit()

    def _go_to_previous_question() -> None:
        nonlocal step_index
        if step_index >= len(questions):
            step_index = max(0, len(questions) - 1)
            return
        if step_index > 0:
            _store_current_answer()
            step_index -= 1

    def _render_step_nav() -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = [("class:rule", "-" * 72 + "\n")]
        for idx, question in enumerate(questions):
            done = _answer_has_value(answers.get(question.question))
            marker = "[x]" if done else "[ ]"
            style = "class:active_tab" if idx == step_index else "class:done_tab" if done else "class:tab"
            fragments.append((style, f"{marker} {question.header}"))
            fragments.append(("", "   "))
        style = "class:active_tab" if step_index == len(questions) else "class:tab"
        fragments.append((style, "Submit"))
        fragments.append(("", "  ->\n\n"))
        return fragments

    def _option_lines(question_index: int) -> list[tuple[str, str]]:
        question = questions[question_index]
        lines: list[tuple[str, str]] = []
        for idx, option in enumerate(question.options):
            is_selected = idx == selected_indices[question_index]
            style = "class:selected" if is_selected else "class:item"
            prefix = "> " if is_selected else "  "
            if question.multi_select:
                mark = "[x]" if idx in checked[question_index] else "[ ]"
                label = f"{prefix}{idx + 1}. {mark} {option.label}"
            elif option.is_other:
                label = f"{prefix}{idx + 1}. Type something"
                value = custom_inputs[question_index]
                if value:
                    label += f": {value}"
                elif is_selected:
                    label += "."
            else:
                label = f"{prefix}{idx + 1}. {option.label}"
            lines.append((style, label))
            if option.description and not option.is_other:
                lines.append(("class:desc", f"     {option.description}"))
        return lines

    def _render_question() -> list[tuple[str, str]]:
        question = questions[step_index]
        selected = _selected_option(step_index)
        fragments = _render_step_nav()
        fragments.extend([
            ("class:title", f"{question.question}\n\n"),
        ])
        option_lines = _option_lines(step_index)
        has_preview = (
            not question.multi_select
            and any(option.preview for option in question.options)
        )
        if not has_preview:
            for style, text in option_lines:
                fragments.append((style, text + "\n"))
        else:
            left_width = 42
            right = _preview_box(
                selected.preview if selected else None,
                width=60,
                height=max(3, len(option_lines) - 1),
            )
            rows = max(len(option_lines), len(right))
            for row in range(rows):
                if row < len(option_lines):
                    style, text = option_lines[row]
                    fragments.append((style, _pad_display(_clip_display(text, left_width), left_width)))
                else:
                    fragments.append(("", " " * left_width))
                fragments.append(("", "  "))
                if row < len(right):
                    fragments.append(("class:preview", right[row]))
                fragments.append(("", "\n"))
            fragments.append(("", "\n"))
        if _current_custom_active():
            fragments.append(("class:input_label", "Custom answer: "))
            fragments.append(("class:input", custom_inputs[step_index] + "|\n"))
        fragments.append(("", "\n"))
        fragments.append((
            "class:hint",
            "Up/Down or j/k select, type on 'Type something', Tab next, Shift-Tab back, Enter continue.\n",
        ))
        return fragments

    def _render_submit() -> list[tuple[str, str]]:
        fragments = _render_step_nav()
        fragments.extend([
            ("class:title", "Review your answers\n\n"),
        ])
        for idx, question in enumerate(questions):
            answer = answers.get(question.question, _answer_for(idx))
            fragments.extend([
                ("class:item", f"- {question.question}\n"),
                ("class:answer", f"  -> {_display_answer(answer)}\n"),
            ])
        fragments.extend([
            ("", "\n"),
            ("class:hint", "Ready to submit your answers?\n\n"),
        ])
        options = ["Submit answers", "Cancel"]
        for idx, label in enumerate(options):
            style = "class:selected" if idx == submit_choice else "class:item"
            prefix = "> " if idx == submit_choice else "  "
            fragments.append((style, f"{prefix}{idx + 1}. {label}\n"))
        fragments.append(("", "\n"))
        fragments.append(("class:hint", "Enter submit, Shift-Tab back to edit.\n"))
        return fragments

    def _render_ask():
        if step_index >= len(questions):
            return _render_submit()
        return _render_question()

    body = FormattedTextControl(_render_ask, focusable=True, show_cursor=False)
    kb = KeyBindings()

    def _invalidate(event) -> None:
        event.app.invalidate()

    @kb.add("up")
    @kb.add("k")
    def _move_up(event) -> None:
        nonlocal submit_choice
        if _current_custom_active() and event.data in {"j", "k"}:
            custom_inputs[step_index] += event.data
            _invalidate(event)
            return
        if step_index >= len(questions):
            submit_choice = (submit_choice - 1) % 2
            _invalidate(event)
            return
        options = _question_options(step_index)
        if options:
            selected_indices[step_index] = (selected_indices[step_index] - 1) % len(options)
        _invalidate(event)

    @kb.add("down")
    @kb.add("j")
    def _move_down(event) -> None:
        nonlocal submit_choice
        if _current_custom_active() and event.data in {"j", "k"}:
            custom_inputs[step_index] += event.data
            _invalidate(event)
            return
        if step_index >= len(questions):
            submit_choice = (submit_choice + 1) % 2
            _invalidate(event)
            return
        options = _question_options(step_index)
        if options:
            selected_indices[step_index] = (selected_indices[step_index] + 1) % len(options)
        _invalidate(event)

    @kb.add("tab")
    @kb.add("right")
    def _next(event) -> None:
        _go_to_next_question()
        _invalidate(event)

    @kb.add("s-tab")
    @kb.add("left")
    def _previous(event) -> None:
        _go_to_previous_question()
        _invalidate(event)

    @kb.add(" ")
    def _toggle(event) -> None:
        if step_index >= len(questions):
            return
        question = questions[step_index]
        if not question.multi_select:
            return
        selected = selected_indices[step_index]
        if selected in checked[step_index]:
            checked[step_index].remove(selected)
        else:
            checked[step_index].add(selected)
        _invalidate(event)

    @kb.add("enter")
    def _accept(event) -> None:
        if step_index >= len(questions):
            if submit_choice == 0:
                for idx, question in enumerate(questions):
                    answers[question.question] = _answer_for(idx)
                event.app.exit(result=answers)
            else:
                event.app.exit(result={})
            return
        _go_to_next_question()
        _invalidate(event)

    @kb.add("c-h")
    @kb.add("backspace")
    def _backspace(event) -> None:
        if not _current_custom_active():
            return
        custom_inputs[step_index] = custom_inputs[step_index][:-1]
        _invalidate(event)

    @kb.add(Keys.Any)
    def _type_custom(event) -> None:
        if not _current_custom_active():
            return
        data = event.data
        if not data or data in {"\r", "\n", "\t"}:
            return
        if data == "\x1b":
            return
        custom_inputs[step_index] += data
        _invalidate(event)

    @kb.add("escape")
    def _jump_to_other(event) -> None:
        if step_index >= len(questions):
            return
        question = questions[step_index]
        if question.multi_select:
            for idx, option in enumerate(question.options):
                if option.is_other:
                    checked[step_index].add(idx)
                    selected_indices[step_index] = idx
                    break
            _invalidate(event)
            return
        for idx, option in enumerate(question.options):
            if option.is_other:
                selected_indices[step_index] = idx
                break
        _invalidate(event)

    @kb.add("c-c")
    def _cancel(event) -> None:
        event.app.exit(result={})

    app = Application(
        layout=Layout(HSplit([Window(content=body, always_hide_cursor=True)])),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
        style=Style.from_dict({
            "rule": "ansibrightblack",
            "tab": "ansibrightblack",
            "active_tab": "ansiblack bg:ansibrightblue",
            "done_tab": "ansiwhite",
            "title": "bold",
            "hint": "ansibrightblack",
            "item": "",
            "desc": "ansibrightblack",
            "selected": "ansibrightblue bold",
            "answer": "ansigreen",
            "preview": "ansiwhite",
            "input_label": "ansibrightblue",
            "input": "ansiwhite",
        }),
    )

    with patch_stdout():
        result = await app.run_async()
    return result or {}


async def _run_ask_question(question: AskQuestion) -> str | list[str] | None:
    if not question.options:
        return await _ask_free_text(question.question)

    selected_index = 0
    checked: set[int] = set()
    has_preview = (
        not question.multi_select
        and any(opt.preview for opt in question.options)
    )

    def _render_ask():
        selected = question.options[selected_index]
        fragments: list[tuple[str, str]] = [
            ("class:header", f"[{question.header}] "),
            ("class:title", f"{question.question}\n"),
            ("class:hint", (
                "Up/Down or j/k select, Enter confirm, Esc free text"
                + (", Space toggles" if question.multi_select else "")
                + ".\n\n"
            )),
        ]
        option_lines: list[tuple[str, str]] = []
        for idx, opt in enumerate(question.options):
            is_selected = idx == selected_index
            style = "class:selected" if is_selected else "class:item"
            prefix = "> " if is_selected else "  "
            if question.multi_select:
                mark = "[x]" if idx in checked else "[ ]"
                label = f"{prefix}{mark} {idx + 1}. {opt.label}"
            else:
                label = f"{prefix}{idx + 1}. {opt.label}"
            option_lines.append((style, label))
            if opt.description:
                option_lines.append(("class:desc", f"     {opt.description}"))

        if not has_preview:
            for style, text in option_lines:
                fragments.append((style, text + "\n"))
            return fragments

        left_width = 42
        left = option_lines
        right = _preview_box(selected.preview, width=60, height=max(6, len(left) - 2))
        rows = max(len(left), len(right))
        for row in range(rows):
            if row < len(left):
                style, text = left[row]
                fragments.append((style, _pad_display(_clip_display(text, left_width), left_width)))
            else:
                fragments.append(("", " " * left_width))
            fragments.append(("", "  "))
            if row < len(right):
                fragments.append(("class:preview", right[row]))
            fragments.append(("", "\n"))
        return fragments

    body = FormattedTextControl(_render_ask, focusable=True, show_cursor=False)
    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _move_up(event) -> None:
        nonlocal selected_index
        selected_index = (selected_index - 1) % len(question.options)
        event.app.invalidate()

    @kb.add("down")
    @kb.add("j")
    def _move_down(event) -> None:
        nonlocal selected_index
        selected_index = (selected_index + 1) % len(question.options)
        event.app.invalidate()

    @kb.add(" ")
    def _toggle(event) -> None:
        if not question.multi_select:
            return
        if selected_index in checked:
            checked.remove(selected_index)
        else:
            checked.add(selected_index)
        event.app.invalidate()

    @kb.add("enter")
    def _accept(event) -> None:
        if question.multi_select:
            selected = sorted(checked) or [selected_index]
            labels = [question.options[idx].label for idx in selected]
            event.app.exit(result=labels)
            return
        event.app.exit(result=question.options[selected_index].label)

    @kb.add("escape")
    @kb.add("c-c")
    def _free_text(event) -> None:
        event.app.exit(result=None)

    app = Application(
        layout=Layout(HSplit([Window(content=body, always_hide_cursor=True)])),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
        style=Style.from_dict({
            "header": "ansibrightblue bold",
            "title": "bold",
            "hint": "ansibrightblack",
            "item": "",
            "desc": "ansibrightblack",
            "selected": "ansibrightblue bold",
            "preview": "ansiwhite",
        }),
    )

    with patch_stdout():
        return await app.run_async()


async def _ask_user_handler(questions: list[AskQuestion]) -> str:
    """Interactive ask_user prompt with structured option picker."""
    answers = await _run_ask_questions(questions)
    return build_ask_user_result(questions, answers)
_HELP_TEXT = """[bold]Edgebot commands:[/bold]
  /new                Start a new conversation
  /sessions           List saved sessions
  /resume             Interactively pick a session to resume
  /resume <#|key>     Resume a specific session
  /compact            Compress conversation context
  /memory             Run memory consolidation now
  /dream-log          Show the latest Dream memory change
  /dream-log <sha>    Show a specific Dream memory change
  /dream-restore      List recent Dream memory versions
  /dream-restore <sha> Restore a Dream memory version
  /cron               Show cron jobs and service state
  /cron run <id>      Run a job immediately
  /cron rm <id>       Remove a job
  /cron on/off <id>   Enable or disable a job
  /cron add every <seconds> <message>
  /cron add at <iso-datetime> <message>
  /cron add expr <cron-expr> <message> [tz]
  /heartbeat          Trigger one heartbeat tick now
  /mcp                Show MCP servers and loaded capabilities
  /tasks              Show task board
  /bg                 Show all background tasks
  /bg <id>            Show one background task
  /bg output <id>     Show task output
  /subagents          List subagents
  /subagents <id>     Show subagent details
  /subagents output <id>
  /subagents transcript <id>
  /subagents fg <id>  Move subagent to foreground
  /subagents bg <id>  Move subagent to background
  /subagents stop <id> [reason]
  /permissions        Show permission rules (persisted + session)
  /status             Show current session info
  /help               Show this help
  /exit                Quit"""


_REPLAY_TAIL = 10  # How many visible turns to replay on /resume
_MEMORY_JOB_ID = "memory_consolidation"
_SESSION_PICK_LIMIT = 20


def _extract_changed_files(diff: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _format_changed_files(diff: str) -> str:
    files = _extract_changed_files(diff)
    if not files:
        return "No tracked memory files changed."
    return ", ".join(f"`{path}`" for path in files)


def _format_dream_log_content(commit, diff: str, *, requested_sha: str | None = None) -> str:
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_sha else "Here is the latest Dream memory change.",
        "",
        f"- Commit: `{commit.sha}`",
        f"- Time: {commit.timestamp}",
        f"- Changed files: {_format_changed_files(diff)}",
    ]
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {commit.sha}` to undo this change.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    else:
        lines.extend(["", "Dream recorded this version, but there is no file diff to display."])
    return "\n".join(lines)


def _format_dream_restore_list(commits: list) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream memory version to restore. Latest first:",
        "",
    ]
    for commit in commits:
        lines.append(f"- `{commit.sha}` {commit.timestamp} - {commit.message.splitlines()[0]}")
    lines.extend([
        "",
        "Preview a version with `/dream-log <sha>` before restoring it.",
        "Restore a version with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


def _handle_dream_log_command(query: str) -> None:
    _MEMORY.ensure_git_initialized()
    git = _MEMORY.git
    if not git.is_initialized():
        console.print("[dim]  Dream history is not available because memory versioning is not initialized.[/dim]")
        return

    parts = shlex.split(query)
    if len(parts) > 1:
        sha = parts[1]
        result = git.show_commit_diff(sha)
        if not result:
            console.print(f"[dim]  Couldn't find Dream change {sha}.[/dim]")
            return
        commit, diff = result
        console.print(_format_dream_log_content(commit, diff, requested_sha=sha), highlight=False)
        return

    commits = git.log(max_entries=1)
    if not commits:
        console.print("[dim]  Dream memory has no saved versions yet.[/dim]")
        return
    result = git.show_commit_diff(commits[0].sha)
    if not result:
        console.print("[dim]  Dream memory has no diff to display yet.[/dim]")
        return
    commit, diff = result
    console.print(_format_dream_log_content(commit, diff), highlight=False)


def _handle_dream_restore_command(query: str) -> None:
    _MEMORY.ensure_git_initialized()
    git = _MEMORY.git
    if not git.is_initialized():
        console.print("[dim]  Dream history is not available because memory versioning is not initialized.[/dim]")
        return

    parts = shlex.split(query)
    if len(parts) == 1:
        commits = git.log(max_entries=10)
        if not commits:
            console.print("[dim]  Dream memory has no saved versions to restore yet.[/dim]")
            return
        console.print(_format_dream_restore_list(commits), highlight=False)
        return

    sha = parts[1]
    result = git.show_commit_diff(sha)
    changed_files = _format_changed_files(result[1]) if result else "the tracked memory files"
    new_sha = git.revert(sha)
    if new_sha:
        console.print(
            (
                f"Restored Dream memory to the state before `{sha}`.\n\n"
                f"- New safety commit: `{new_sha}`\n"
                f"- Restored files: {changed_files}\n\n"
                f"Use `/dream-log {new_sha}` to inspect the restore diff."
            ),
            highlight=False,
        )
    else:
        console.print(f"[dim]  Couldn't restore Dream change {sha}.[/dim]")


def _render_mcp_startup(mcp_client) -> None:
    """Print a concise MCP startup summary."""
    for line in mcp_client.startup_summary_lines():
        console.print(f"[dim]{line}[/dim]")


def _render_mcp_details(mcp_client) -> None:
    """Print detailed MCP summary including capability names."""
    for line in mcp_client.detailed_summary_lines():
        console.print(f"[dim]{line}[/dim]")


def _format_cron_snapshot() -> list[str]:
    status = CRON.status(include_system=False)
    lines = [
        f"  Cron: {'running' if status['enabled'] else 'stopped'}, {status['jobs']} job(s)"
    ]
    for job in CRON.list_jobs(include_disabled=True, include_system=False):
        state_bits = []
        if job.state.next_run_at_ms:
            state_bits.append(
                "next " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job.state.next_run_at_ms / 1000))
            )
        if job.state.last_status:
            state_bits.append(f"last {job.state.last_status}")
        state = f" [{' | '.join(state_bits)}]" if state_bits else ""
        lines.append(f"  - {job.id} {job.name} ({'on' if job.enabled else 'off'}){state}")
    return lines


def _render_cron_table() -> None:
    jobs = CRON.list_jobs(include_disabled=True, include_system=False)
    status = CRON.status(include_system=False)
    console.print(
        f"[dim]  Cron: {'running' if status['enabled'] else 'stopped'}, "
        f"{status['jobs']} job(s)[/dim]"
    )
    if not jobs:
        console.print("[dim]  No scheduled jobs.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("State", no_wrap=True)
    table.add_column("Message")

    for job in jobs:
        if job.schedule.kind == "every" and job.schedule.every_ms:
            schedule = f"every {job.schedule.every_ms // 1000}s"
        elif job.schedule.kind == "cron":
            tz = f" {job.schedule.tz}" if job.schedule.tz else ""
            schedule = f"{job.schedule.expr}{tz}"
        elif job.schedule.kind == "at" and job.schedule.at_ms:
            schedule = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job.schedule.at_ms / 1000))
        else:
            schedule = job.schedule.kind

        state_parts = ["on" if job.enabled else "off"]
        if job.state.next_run_at_ms:
            state_parts.append(
                "next " + time.strftime("%m-%d %H:%M:%S", time.localtime(job.state.next_run_at_ms / 1000))
            )
        if job.state.last_status:
            state_parts.append(f"last {job.state.last_status}")
        message = " ".join(job.payload.message.split())
        if len(message) > 56:
            message = message[:53] + "..."
        table.add_row(job.id, job.name, schedule, " | ".join(state_parts), message)

    console.print(table)


async def _handle_cron_command(query: str) -> None:
    parts = shlex.split(query)
    if len(parts) == 1:
        _render_cron_table()
        return

    sub = parts[1].lower()
    if sub in {"list", "ls"}:
        _render_cron_table()
        return
    if sub == "status":
        for line in _format_cron_snapshot():
            console.print(f"[dim]{line}[/dim]")
        return
    if sub in {"rm", "remove", "run", "on", "off"}:
        if len(parts) < 3:
            console.print(f"[dim]  Usage: /cron {sub} <job_id>[/dim]")
            return
        job_id = parts[2]
        if sub in {"rm", "remove"}:
            result = CRON.remove_job(job_id)
            if result == "removed":
                msg = "Removed " + job_id
            elif result == "protected":
                msg = "Job is system-managed: " + job_id
            else:
                msg = "Job not found: " + job_id
            console.print(f"[dim]  {msg}[/dim]")
            return
        if sub == "run":
            ran = await CRON.run_job(job_id, force=True)
            console.print(f"[dim]  {'Ran ' + job_id if ran else 'Job not found: ' + job_id}[/dim]")
            return
        if sub == "on":
            job = CRON.enable_job(job_id, True)
            if job == "protected":
                msg = "Job is system-managed: " + job_id
            else:
                msg = "Enabled " + job_id if job else "Job not found: " + job_id
            console.print(f"[dim]  {msg}[/dim]")
            return
        if sub == "off":
            job = CRON.enable_job(job_id, False)
            if job == "protected":
                msg = "Job is system-managed: " + job_id
            else:
                msg = "Disabled " + job_id if job else "Job not found: " + job_id
            console.print(f"[dim]  {msg}[/dim]")
            return

    if sub == "add":
        if len(parts) < 5:
            console.print("[dim]  Usage: /cron add every <seconds> <message>[/dim]")
            console.print("[dim]         /cron add at <iso-datetime> <message>[/dim]")
            console.print("[dim]         /cron add expr <cron-expr> <message> [tz][/dim]")
            return
        mode = parts[2].lower()
        if mode == "every":
            try:
                seconds = int(parts[3])
            except ValueError:
                console.print("[dim]  Invalid seconds value.[/dim]")
                return
            message = " ".join(parts[4:]).strip()
            job = CRON.add_job(
                name=message[:40],
                schedule=CronSchedule(kind="every", every_ms=seconds * 1000),
                message=message,
                deliver=True,
                channel="cli",
                to="direct",
                session_key="manual_cron",
            )
            console.print(f"[dim]  Created job {job.id} ({job.name}).[/dim]")
            return
        if mode == "at":
            try:
                at_ms = int(datetime.fromisoformat(parts[3]).timestamp() * 1000)
            except ValueError:
                console.print("[dim]  Invalid ISO datetime.[/dim]")
                return
            message = " ".join(parts[4:]).strip()
            job = CRON.add_job(
                name=message[:40],
                schedule=CronSchedule(kind="at", at_ms=at_ms),
                message=message,
                deliver=True,
                channel="cli",
                to="direct",
                session_key="manual_cron",
                delete_after_run=True,
            )
            console.print(f"[dim]  Created job {job.id} ({job.name}).[/dim]")
            return
        if mode == "expr":
            if len(parts) < 5:
                console.print("[dim]  Usage: /cron add expr <cron-expr> <message> [tz][/dim]")
                return
            if _get_croniter() is None:
                console.print("[dim]  croniter is not installed, cron expressions are unavailable.[/dim]")
                return
            expr = parts[3]
            message = parts[4]
            tz = parts[5] if len(parts) >= 6 else None
            job = CRON.add_job(
                name=message[:40],
                schedule=CronSchedule(kind="cron", expr=expr, tz=tz),
                message=message,
                deliver=True,
                channel="cli",
                to="direct",
                session_key="manual_cron",
            )
            console.print(f"[dim]  Created job {job.id} ({job.name}).[/dim]")
            return

    console.print("[dim]  Unknown /cron usage. Subcommands: list, run, rm, on, off, add[/dim]")


def _render_history(history: list[dict]) -> None:
    """Pretty-print previous conversation after /resume, dimmed with a divider."""
    def _is_visible(m: dict) -> bool:
        if m.get("role") not in ("user", "assistant"):
            return False
        c = m.get("content")
        if not isinstance(c, str) or not c.strip():
            return False
        skip_prefixes = (
            "<background-results>",
            "<reminder>",
            "[System: Context auto-compressed",
            "[System: User was idle",
        )
        return not c.startswith(skip_prefixes)

    visible = [m for m in history if _is_visible(m)]
    if not visible:
        return

    shown = visible[-_REPLAY_TAIL:]
    omitted = len(visible) - len(shown)

    console.rule("[dim]session history[/dim]", style="dim")
    if omitted > 0:
        console.print(
            f"[dim italic]  \u2026 {omitted} earlier message(s) hidden \u2026[/dim italic]\n"
        )

    for msg in shown:
        body = msg["content"]
        if len(body) > 2000:
            body = body[:2000] + f"\n[\u2026 {len(body) - 2000} chars truncated \u2026]"
        # Escape Rich markup so user content like "[bold]" isn't interpreted.
        from rich.markup import escape
        body_esc = escape(body)
        if msg["role"] == "user":
            console.print(f"[dim bold]You:[/dim bold] [dim]{body_esc}[/dim]")
        else:
            console.print(f"[dim bold cyan]Edgebot:[/dim bold cyan] [dim]{body_esc}[/dim]")
        console.print()

    console.rule(style="dim")
    console.print()


def _time_ago(dt) -> str:
    delta = time.time() - dt.timestamp()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _format_timestamp(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _render_subagent_list() -> None:
    tasks = SUBAGENT.list_all()
    if not tasks:
        console.print("[dim]  No subagents.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Mode", no_wrap=True)
    table.add_column("Capability", no_wrap=True)
    table.add_column("Tools", no_wrap=True)
    table.add_column("Started", no_wrap=True)
    table.add_column("Description")

    for task in tasks:
        mode = "bg" if task.get("is_backgrounded", True) else "fg"
        table.add_row(
            task["task_id"],
            task["status"],
            mode,
            task.get("capability", "-"),
            str(task.get("tool_uses", 0)),
            _format_timestamp(task.get("started_at")),
            task.get("description", ""),
        )
    console.print(table)


def _print_subagent_blob(title: str, body: str) -> None:
    console.rule(f"[dim]{title}[/dim]", style="dim")
    console.print(escape(body) if body else "[dim](empty)[/dim]", highlight=False, soft_wrap=True)


def _render_subagent_detail(task: dict[str, object]) -> None:
    if task.get("error") and "task_id" not in task:
        console.print(f"[dim]  {task['error']}[/dim]")
        return

    status = str(task.get("status", "-"))
    mode = "background" if task.get("is_backgrounded", True) else "foreground"
    console.print(f"[cyan]{escape(str(task.get('task_id', '-')))}[/cyan] [dim]({escape(status)} / {mode})[/dim]")
    console.print(f"[dim]  Capability : {escape(str(task.get('capability', '-')))}[/dim]")
    console.print(f"[dim]  Description: {escape(str(task.get('description', '')))}[/dim]")
    console.print(f"[dim]  Started    : {_format_timestamp(task.get('started_at'))}[/dim]")
    console.print(f"[dim]  Finished   : {_format_timestamp(task.get('finished_at'))}[/dim]")
    console.print(f"[dim]  Tool uses  : {task.get('tool_uses', 0)}[/dim]")
    console.print(f"[dim]  Output     : {escape(str(task.get('output_file', '-')))}[/dim]")
    console.print(f"[dim]  Transcript : {escape(str(task.get('transcript_file', '-')))}[/dim]")
    if task.get("stop_requested"):
        console.print(f"[dim]  Stop req   : {escape(str(task.get('stop_reason') or 'requested'))}[/dim]")
    if task.get("error") and status not in {"running"}:
        console.print(f"[dim]  Error      : {escape(str(task.get('error')))}[/dim]")

    output_preview = str(task.get("output_preview", "") or "")
    transcript_preview = str(task.get("transcript_preview", "") or "")
    if output_preview:
        _print_subagent_blob("output preview", output_preview)
    if transcript_preview:
        _print_subagent_blob("transcript preview", transcript_preview)
    console.print()


def _resolve_session_summary(state: dict) -> str | None:
    """Prefer explicit session metadata, then fall back to compacted history."""
    metadata = state.get("metadata", {})
    summary = metadata.get("session_summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return extract_session_summary(state.get("messages", []))


async def _pick_session_interactive(
    sessions: list[dict],
    *,
    current_session_key: str,
) -> dict | None:
    """Inline arrow-key session picker for bare /resume."""
    if not sessions:
        return None

    visible = sessions[:_SESSION_PICK_LIMIT]
    selected_index = next(
        (idx for idx, item in enumerate(visible) if item["key"] == current_session_key),
        0,
    )

    def _render_picker():
        fragments: list[tuple[str, str]] = [
            ("class:title", "Resume session\n"),
            ("class:hint", "Up/Down select, Enter resume, Esc cancel.\n\n"),
        ]
        for idx, item in enumerate(visible):
            ago = _time_ago(item["updated_at"])
            pointer = "> " if idx == selected_index else "  "
            key_style = "class:selected" if idx == selected_index else "class:item"
            meta_style = "class:selected-meta" if idx == selected_index else "class:meta"
            current = "  [current]" if item["key"] == current_session_key else ""
            fragments.append((key_style, f"{pointer}{item['key']}{current}\n"))
            fragments.append((meta_style, f"   {item['message_count']} msgs, {ago}\n"))
            fragments.append(("", "\n"))
        if len(sessions) > len(visible):
            fragments.append(
                ("class:hint", f"Showing latest {len(visible)} of {len(sessions)} sessions.\n")
            )
        return fragments

    body = FormattedTextControl(_render_picker, focusable=True, show_cursor=False)
    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _move_up(event) -> None:
        nonlocal selected_index
        selected_index = (selected_index - 1) % len(visible)
        event.app.invalidate()

    @kb.add("down")
    @kb.add("j")
    def _move_down(event) -> None:
        nonlocal selected_index
        selected_index = (selected_index + 1) % len(visible)
        event.app.invalidate()

    @kb.add("enter")
    def _accept(event) -> None:
        event.app.exit(result=visible[selected_index])

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    app = Application(
        layout=Layout(HSplit([Window(content=body, always_hide_cursor=True)])),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
        style=Style.from_dict({
            "title": "bold",
            "hint": "ansibrightblack",
            "item": "",
            "meta": "ansibrightblack",
            "selected": "reverse bold",
            "selected-meta": "reverse",
        }),
    )

    with patch_stdout():
        return await app.run_async()


_LOGO = """
[bold cyan]    ,------.    [/bold cyan]
[bold cyan]   / ,----. \\   [/bold cyan]    [bold white]E D G E B O T[/bold white]
[bold blue]  / / ,--| \\ \\  [/bold blue]    [dim]Autonomous Workspace Agent[/dim]
[bold blue]  \\ \\ `--/ / /  [/bold blue]
[bold magenta]   \\ -----/ /   [/bold magenta]    [dim]Model:[/dim] [cyan]{model}[/cyan]
[bold purple]    `------'    [/bold purple]
"""


def _print_banner(heartbeat) -> None:
    console.print(_LOGO.format(model=MODEL))
    for line in _format_cron_snapshot():
        console.print(f"[dim]{line}[/dim]")
    hb = heartbeat.status()
    console.print(
        f"[dim]  Heartbeat: {'running' if hb['running'] else 'stopped'}, "
        f"every {hb['interval_s']}s, file={'yes' if hb['present'] else 'no'}[/dim]"
    )
    if hb.get("last_action"):
        console.print(f"[dim]  Heartbeat last: {hb['last_action']} ({hb.get('last_reason') or 'n/a'})[/dim]")
    console.print("  [dim]Type [bold]/help[/bold] for commands, [bold]exit[/bold] to quit[/dim]\n")


async def main():
    # --- Seed workspace templates (first run) ---
    seed_workspace_templates()
    # Skill loader is instantiated at import time; refresh after first-run seeding.
    SKILLS.reload()

    # --- One-shot dedup cleanup of accumulated memory files ---
    cleanup_memory_files_once()

    # --- Start fresh session (no picker) ---
    store = SessionStore(
        SESSION_DIR,
        workspace=WORKDIR,
        legacy_sessions_dir=LEGACY_SESSION_DIR,
    )
    session_key = f"session_{int(time.time())}"
    history: list[dict] = []
    session_summary: str | None = None

    # --- MCP initialization (optional) ---
    mcp_client = await load_mcp(MCP_CONFIG_PATH)
    all_tools = list(TOOLS)
    all_handlers = dict(TOOL_HANDLERS)
    if mcp_client:
        all_tools.extend(mcp_client.tool_schemas)
        all_handlers.update(mcp_client.tool_handlers)
        _render_mcp_startup(mcp_client)
    set_permission_prompt_handler(_permission_prompt)
    set_batch_permission_prompt_handler(_batch_permission_prompt)
    set_ask_handler(_ask_user_handler)

    async def _run_background_turn(
        prompt: str,
        *,
        run_channel: str,
        run_chat_id: str,
        run_session_key: str,
        emit_output: bool,
        assistant_label: str,
    ) -> str | None:
        state = store.load_state(run_session_key)
        run_history = list(state["messages"])
        run_summary = _resolve_session_summary(state)
        user_msg = {"role": "user", "content": prompt}
        store.update_metadata(run_session_key, pending_user_turn=True)
        run_history.append(user_msg)
        store.append(run_session_key, user_msg)
        return await agent_loop(
            messages=run_history,
            system=build_system_prompt(session_key=run_session_key),
            tools=all_tools,
            tool_handlers=all_handlers,
            todo_mgr=TODO,
            bg_mgr=BG,
            session_store=store,
            session_key=run_session_key,
            channel=run_channel,
            chat_id=run_chat_id,
            session_summary=run_summary,
            emit_output=emit_output,
            assistant_label=assistant_label,
        )

    async def _handle_cron_job(job) -> str | None:
        if job.payload.kind == "system_event" and job.id == _MEMORY_JOB_ID:
            await consolidate_memory([], store=_MEMORY, emit_output=False)
            return None
        await _interactive_notice(f"[cron] running {job.name} ({job.id})")
        response = await _run_background_turn(
            job.payload.message,
            run_channel=job.payload.channel or "cron",
            run_chat_id=job.payload.to or "cron",
            run_session_key=job.payload.session_key or f"cron_{job.id}",
            emit_output=False,
            assistant_label=f"cron:{job.name}",
        )
        if job.payload.deliver and response and not response.isspace():
            await _interactive_notice(f"[cron] completed {job.name}")
            await _interactive_response(f"cron:{job.name}", response)
        return response

    async def _heartbeat_execute(tasks: str) -> str | None:
        await _interactive_notice("[heartbeat] active task detected")
        return await _run_background_turn(
            tasks,
            run_channel="heartbeat",
            run_chat_id="heartbeat",
            run_session_key="heartbeat",
            emit_output=False,
            assistant_label="heartbeat",
        )

    async def _heartbeat_notify(response: str) -> None:
        await _interactive_notice("[heartbeat] result")
        await _interactive_response("heartbeat", response)

    heartbeat = HeartbeatService(
        WORKDIR,
        on_execute=_heartbeat_execute,
        on_notify=_heartbeat_notify,
        interval_s=HEARTBEAT_INTERVAL_SECONDS,
    )
    CRON.set_handler(_handle_cron_job)
    CRON.register_system_job(CronJob(
        id=_MEMORY_JOB_ID,
        name="memory_consolidation",
        schedule=CronSchedule(kind="every", every_ms=MEMORY_CONSOLIDATION_INTERVAL_SECONDS * 1000),
        payload=CronPayload(kind="system_event"),
    ))

    # AutoCompact periodic scan (runs alongside heartbeat interval)
    _AUTOCOMPACT_JOB_ID = "autocompact_scan"

    async def _autocompact_cron_handler(job):
        ac = get_autocompact(store)
        if ac is not None:
            ac.check_expired(
                lambda coro: asyncio.create_task(coro),
                active_session_keys=set(),
            )
        return None

    if IDLE_COMPACT_MINUTES > 0:
        original_handler = CRON._handler

        async def _combined_handler(job):
            if job.id == _AUTOCOMPACT_JOB_ID:
                return await _autocompact_cron_handler(job)
            return await original_handler(job)

        CRON._handler = _combined_handler
        CRON.register_system_job(CronJob(
            id=_AUTOCOMPACT_JOB_ID,
            name="autocompact_idle_scan",
            schedule=CronSchedule(kind="every", every_ms=HEARTBEAT_INTERVAL_SECONDS * 1000),
            payload=CronPayload(kind="system_event"),
        ))

    await CRON.start()
    await heartbeat.start()

    # --- Welcome banner ---
    _print_banner(heartbeat)

    try:
        while True:
            try:
                query = await _ask_user()
            except (EOFError, KeyboardInterrupt):
                break

            if not query:
                continue
            if query.lower() in ("exit", "quit", "/exit", "/quit"):
                break
            agent_query = query

            # ---- REPL commands ----
            if query == "/help":
                console.print(_HELP_TEXT)
                continue

            if query == "/status":
                console.print(f"[dim]  Session : {session_key}[/dim]")
                console.print(f"[dim]  Messages: {len(history)}[/dim]")
                console.print(f"[dim]  Model   : {MODEL}[/dim]")
                console.print(f"[dim]  Summary : {'yes' if session_summary else 'none'}[/dim]")
                for line in _format_cron_snapshot():
                    console.print(f"[dim]{line}[/dim]")
                hb = heartbeat.status()
                console.print(
                    f"[dim]  Heartbeat: {'running' if hb['running'] else 'stopped'}, "
                    f"every {hb['interval_s']}s, file={'yes' if hb['present'] else 'no'}[/dim]"
                )
                if hb.get("last_action"):
                    console.print(
                        f"[dim]  Heartbeat last: {hb['last_action']} "
                        f"({hb.get('last_reason') or 'n/a'})[/dim]"
                    )
                if mcp_client and mcp_client.connected_servers:
                    console.print(
                        f"[dim]  MCP     : {len(mcp_client.connected_servers)} server(s), "
                        f"{len(mcp_client.tool_schemas)} capabilities[/dim]"
                    )
                    for line in mcp_client.startup_summary_lines():
                        console.print(f"[dim]  {line}[/dim]")
                else:
                    console.print("[dim]  MCP     : none[/dim]")
                continue

            if query == "/mcp":
                if mcp_client and mcp_client.connected_servers:
                    _render_mcp_details(mcp_client)
                else:
                    console.print("[dim]  No MCP servers connected.[/dim]")
                continue

            if query.startswith("/cron"):
                await _handle_cron_command(query)
                continue

            if query == "/heartbeat":
                console.print("[dim]  Triggering heartbeat...[/dim]")
                result = await heartbeat.trigger_now()
                if result is None:
                    console.print("[dim]  Heartbeat skipped.[/dim]")
                continue

            if query == "/new":
                session_key = f"session_{int(time.time())}"
                history.clear()
                session_summary = None
                console.clear()
                _print_banner(heartbeat)
                continue

            if query == "/sessions":
                sessions = store.list_sessions()
                if not sessions:
                    console.print("[dim]  No saved sessions.[/dim]")
                else:
                    console.print(f"[dim]  Workspace sessions: {WORKDIR}[/dim]")
                    for i, s in enumerate(sessions[:15], 1):
                        ago = _time_ago(s["updated_at"])
                        cur = " [bold cyan]<-[/bold cyan]" if s["key"] == session_key else ""
                        console.print(
                            f"  [dim]{i:>2}.[/dim] {s['key']}  "
                            f"[dim]({s['message_count']} msgs, {ago})[/dim]{cur}"
                        )
                continue

            if query.startswith("/resume"):
                arg = query[len("/resume"):].strip()
                sessions = store.list_sessions()
                target = None
                if not arg:
                    if not sessions:
                        console.print("[dim]  No saved sessions.[/dim]")
                        continue
                    target = await _pick_session_interactive(
                        sessions,
                        current_session_key=session_key,
                    )
                    if target is None:
                        console.print("[dim]  Resume cancelled.[/dim]")
                        continue
                else:
                    try:
                        idx = int(arg) - 1
                        if 0 <= idx < len(sessions):
                            target = sessions[idx]
                    except ValueError:
                        for s in sessions:
                            if s["key"] == arg:
                                target = s
                                break
                if target:
                    session_key = target["key"]
                    state = store.load_state(session_key)
                    history[:] = state["messages"]
                    session_summary = _resolve_session_summary(state)

                    idle_minutes = (time.time() - target["updated_at"].timestamp()) / 60
                    if history and idle_minutes > 60 and len(history) > 10:
                        console.print(
                            f"[dim]  Idle {int(idle_minutes)}m — compressing older history...[/dim]"
                        )
                        history[:] = await auto_compact(
                            history,
                            is_idle=True,
                            idle_minutes=idle_minutes,
                            memory_store=_MEMORY,
                        )
                        store.save_all(session_key, history)
                        session_summary = extract_session_summary(history)
                        store.update_metadata(session_key, session_summary=session_summary or "")

                    if history:
                        _render_history(history)
                    if session_summary:
                        console.print("[dim]  Resume summary loaded into runtime context.[/dim]")
                    console.print(
                        f"[dim]  Resumed '{session_key}' ({len(history)} messages).[/dim]"
                    )
                else:
                    console.print("[dim]  Session not found. Use /resume (bare) for picker, /resume <#> for index, or /resume <key>.[/dim]")
                continue

            if query == "/compact":
                if history:
                    console.print("[dim]  Compressing...[/dim]")
                    history[:] = await auto_compact(history, is_idle=False, memory_store=_MEMORY)
                    store.save_all(session_key, history)
                    session_summary = extract_session_summary(history)
                    store.update_metadata(session_key, session_summary=session_summary or "")
                    console.print("[dim]  Done.[/dim]")
                continue

            if query == "/memory":
                console.print("[dim]  Running memory consolidation...[/dim]")
                await consolidate_memory(history, store=_MEMORY)
                continue

            if query.startswith("/dream-log"):
                _handle_dream_log_command(query)
                continue

            if query.startswith("/dream-restore"):
                _handle_dream_restore_command(query)
                continue

            if query == "/tasks":
                console.print(TASK_MGR.list_all())
                continue

            if query.startswith("/bg"):
                parts = shlex.split(query)
                if len(parts) == 1:
                    console.print(BG.check())
                    continue
                if len(parts) == 2:
                    console.print(BG.check(parts[1]))
                    continue
                if len(parts) >= 3 and parts[1].lower() == "output":
                    console.print(
                        json.dumps(
                            BG.task_output(parts[2], block=False, timeout_ms=0),
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                    continue
                console.print("[dim]  Usage: /bg | /bg <task_id> | /bg output <task_id>[/dim]")
                continue

            if query.startswith("/subagents"):
                parts = shlex.split(query)
                if len(parts) == 1:
                    _render_subagent_list()
                    continue

                if len(parts) == 2:
                    _render_subagent_detail(SUBAGENT.status(parts[1]))
                    continue

                sub = parts[1].lower()
                if sub in {"output", "transcript"}:
                    if len(parts) < 3:
                        console.print(f"[dim]  Usage: /subagents {sub} <task_id>[/dim]")
                        continue
                    detail = SUBAGENT.detail(parts[2])
                    if detail.get("error"):
                        console.print(f"[dim]  {detail['error']}[/dim]")
                        continue
                    body = detail.get(sub, "")
                    _print_subagent_blob(f"{sub} {parts[2]}", str(body or ""))
                    console.print()
                    continue

                if sub in {"fg", "foreground", "wait"}:
                    if len(parts) < 3:
                        console.print("[dim]  Usage: /subagents fg <task_id>[/dim]")
                        continue
                    result = await SUBAGENT.wait(
                        parts[2],
                        timeout_ms=None,
                        foreground=True,
                        include_output=True,
                    )
                    if result.get("retrieval_status") == "not_found":
                        console.print(f"[dim]  Unknown task_id: {parts[2]}[/dim]")
                        continue
                    task = result.get("task") or {}
                    console.print(f"[dim]  Foreground wait finished: {task.get('status', 'unknown')}[/dim]")
                    _render_subagent_detail(SUBAGENT.status(parts[2]))
                    continue

                if sub in {"bg", "background"}:
                    if len(parts) < 3:
                        console.print("[dim]  Usage: /subagents bg <task_id>[/dim]")
                        continue
                    result = SUBAGENT.set_backgrounded(parts[2], True)
                    if result.get("error"):
                        console.print(f"[dim]  {result['error']}[/dim]")
                    else:
                        console.print(f"[dim]  {parts[2]} moved to background.[/dim]")
                    continue

                if sub in {"stop", "interrupt", "kill"}:
                    if len(parts) < 3:
                        console.print("[dim]  Usage: /subagents stop <task_id> [reason][/dim]")
                        continue
                    reason = " ".join(parts[3:]).strip() or "stopped by user"
                    result = SUBAGENT.stop(parts[2], reason=reason)
                    if result.get("error"):
                        console.print(f"[dim]  {result['error']}[/dim]")
                    else:
                        console.print(f"[dim]  stop requested for {parts[2]}.[/dim]")
                    continue

                console.print(
                    "[dim]  Unknown subcommand. Try: /subagents output|transcript|fg|bg|stop <id>[/dim]"
                )
                continue

            if query == "/permissions":
                from edgebot.tools.registry import PERMISSIONS
                console.print(json.dumps(PERMISSIONS.list_rules(), indent=2, ensure_ascii=False))
                continue

            skill_prompt = resolve_skill_slash_prompt(query, SKILLS)
            if skill_prompt:
                agent_query = skill_prompt
            elif query.startswith("/"):
                console.print(f"[dim]  Unknown command: {query}[/dim]")
                console.print("[dim]  Commands: /new /sessions /resume /compact /memory /dream-log /dream-restore /cron /heartbeat /mcp /tasks /bg /subagents /permissions /status /help[/dim]")
                continue

            # ---- Normal message ----
            system = build_system_prompt(session_key=session_key)
            user_msg = {"role": "user", "content": agent_query}
            store.update_metadata(session_key, pending_user_turn=True)
            history.append(user_msg)
            store.append(session_key, user_msg)

            try:
                await agent_loop(
                    messages=history,
                    system=system,
                    tools=all_tools,
                    tool_handlers=all_handlers,
                    todo_mgr=TODO,
                    bg_mgr=BG,
                    session_store=store,
                    session_key=session_key,
                    channel="cli",
                    chat_id="direct",
                    session_summary=session_summary,
                )
            except Exception as e:
                console.print(f"[red]  Error in agent loop: {e}[/red]")
                console.print(
                    "[dim yellow]  Tip: use /compact to shrink context or /new to start fresh.[/dim yellow]"
                )
            print()

    finally:
        set_permission_prompt_handler(None)
        set_batch_permission_prompt_handler(None)
        set_ask_handler(None)
        await heartbeat.stop()
        await CRON.stop()
        if mcp_client:
            await mcp_client.close()
        console.print("\n[dim]Goodbye![/dim]")
