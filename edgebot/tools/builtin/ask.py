"""
edgebot/tools/builtin/ask.py - Interactive ask_user tool with arrow-key picker.

When the agent needs the user's decision, it calls ask_user. The tool blocks
until the user picks an option or types free text, then returns the answer
as a normal tool result -- no interrupt/resume needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from edgebot.tools.base import BaseTool


@dataclass(frozen=True)
class AskOption:
    label: str
    description: str = ""
    preview: str | None = None
    is_other: bool = False


@dataclass(frozen=True)
class AskQuestion:
    question: str
    header: str
    options: list[AskOption]
    multi_select: bool = False


AskHandler = Callable[[list[AskQuestion]], Awaitable[str]]

_handler: AskHandler | None = None


def _option_item_schema(*, description_required: bool) -> dict[str, Any]:
    required = ["label", "description"] if description_required else ["label"]
    return {
        "anyOf": [
            {"type": "string"},
            {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Short option label.",
                    },
                    "description": {
                        "type": "string",
                        "description": "What selecting this option means.",
                    },
                    "preview": {
                        "type": "string",
                        "description": "Optional markdown/code preview for single-select questions.",
                    },
                },
                "required": required,
            },
        ],
    }


def set_ask_handler(handler: AskHandler | None) -> None:
    global _handler
    _handler = handler


def _option_from_value(value: Any, *, multi_select: bool = False) -> AskOption:
    if isinstance(value, dict):
        label = str(value.get("label", "")).strip()
        description = str(value.get("description", "") or "").strip()
        preview_value = value.get("preview")
        preview = None if preview_value is None or multi_select else str(preview_value)
        return AskOption(
            label=label,
            description=description,
            preview=preview,
        )
    return AskOption(label=str(value).strip())


def _with_other(options: list[AskOption], *, multi_select: bool) -> list[AskOption]:
    cleaned = [opt for opt in options if opt.label]
    if not any(opt.label.strip().lower() == "other" for opt in cleaned):
        cleaned.append(AskOption(
            label="Other",
            description="Type a custom answer.",
            preview=None,
            is_other=True,
        ))
    else:
        cleaned = [
            AskOption(
                label=opt.label,
                description=opt.description,
                preview=None if multi_select else opt.preview,
                is_other=opt.label.strip().lower() == "other",
            )
            for opt in cleaned
        ]
    if multi_select:
        cleaned = [
            AskOption(
                label=opt.label,
                description=opt.description,
                preview=None,
                is_other=opt.is_other,
            )
            for opt in cleaned
        ]
    return cleaned


def normalize_ask_payload(payload: dict[str, Any]) -> list[AskQuestion]:
    """Normalize legacy and structured ask_user arguments for the UI layer."""
    raw_questions = payload.get("questions")
    normalized: list[AskQuestion] = []

    if isinstance(raw_questions, list):
        for index, raw_question in enumerate(raw_questions[:4]):
            if not isinstance(raw_question, dict):
                continue
            text = str(raw_question.get("question", "")).strip()
            if not text:
                continue
            header = str(raw_question.get("header") or f"Question {index + 1}").strip()
            header = header[:12] if header else f"Question {index + 1}"
            multi_select = bool(raw_question.get("multiSelect", False))
            raw_options = raw_question.get("options")
            options = [
                _option_from_value(value, multi_select=multi_select)
                for value in raw_options[:4]
            ] if isinstance(raw_options, list) else []
            normalized.append(AskQuestion(
                question=text,
                header=header,
                options=_with_other(options, multi_select=multi_select),
                multi_select=multi_select,
            ))

    if normalized:
        return normalized

    question = str(payload.get("question", "")).strip()
    raw_options = payload.get("options")
    options = [
        _option_from_value(value)
        for value in raw_options[:4]
    ] if isinstance(raw_options, list) else []
    return [AskQuestion(
        question=question,
        header="Question",
        options=_with_other(options, multi_select=False) if options else [],
        multi_select=False,
    )]


def build_ask_user_result(
    questions: list[AskQuestion],
    answers: dict[str, Any],
    notes: dict[str, str] | None = None,
) -> str:
    annotations: dict[str, dict[str, str]] = {}
    notes = notes or {}
    for question in questions:
        answer = answers.get(question.question)
        selected_labels = answer if isinstance(answer, list) else [answer]
        selected = [
            opt
            for opt in question.options
            if opt.label in selected_labels and opt.preview
        ]
        annotation: dict[str, str] = {}
        if len(selected) == 1 and selected[0].preview:
            annotation["preview"] = selected[0].preview
        note = notes.get(question.question, "").strip()
        if note:
            annotation["notes"] = note
        if annotation:
            annotations[question.question] = annotation

    return json.dumps({
        "answers": answers,
        "annotations": annotations,
    }, ensure_ascii=False)


class AskUserTool(BaseTool):
    """Ask the user a question and wait for their response."""

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return (
            "Ask the user a question and wait for their response. "
            "Use this ONLY when you genuinely need the user's input before proceeding "
            "(e.g., choosing between approaches, confirming a destructive action, clarifying intent). "
            "Prefer 'questions' for structured decisions: 1-4 questions, each with 2-4 options. "
            "Each option can include a short label, description, and for single-select questions an optional preview. "
            "Put the recommended option first and suffix its label with '(Recommended)'. "
            "Do not add an Other option yourself; Edgebot appends it automatically. "
            "Legacy 'question' plus string 'options' is still accepted for simple prompts. "
            "Do NOT use for status updates or rhetorical questions."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "description": "Structured questions to ask the user.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "Complete question text.",
                            },
                            "header": {
                                "type": "string",
                                "description": "Short label, 12 characters or fewer.",
                            },
                            "options": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 4,
                                "items": _option_item_schema(description_required=True),
                            },
                            "multiSelect": {
                                "type": "boolean",
                                "description": "Allow multiple options to be selected. Preview is ignored when true.",
                            },
                        },
                        "required": ["question", "header", "options"],
                    },
                },
                "question": {
                    "type": "string",
                    "description": "Legacy single question to ask the user.",
                },
                "options": {
                    "type": "array",
                    "items": _option_item_schema(description_required=False),
                    "minItems": 2,
                    "maxItems": 4,
                    "description": (
                        "Legacy suggested answers the user can pick from. "
                        "Items may be strings or option objects with label, "
                        "description, and optional preview."
                    ),
                },
            },
        }

    def is_read_only(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def concurrency_safe(self, params: dict[str, Any] | None = None) -> bool:
        return False

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if errors or not isinstance(params, dict):
            return errors

        has_structured = "questions" in params
        has_legacy = "question" in params
        if not has_structured and not has_legacy:
            errors.append("provide either 'questions' or 'question'")

        questions = params.get("questions")
        if questions is not None:
            if not isinstance(questions, list):
                errors.append("'questions' must be an array")
            else:
                if not 1 <= len(questions) <= 4:
                    errors.append("'questions' must contain at most 4 questions")
                for idx, question in enumerate(questions):
                    if not isinstance(question, dict):
                        errors.append(f"'questions[{idx}]' must be an object")
                        continue
                    options = question.get("options")
                    if not isinstance(options, list) or not 2 <= len(options) <= 4:
                        errors.append(f"'questions[{idx}].options' must have 2-4 options")

        legacy_options = params.get("options")
        if legacy_options is not None and (
            not isinstance(legacy_options, list) or not 2 <= len(legacy_options) <= 4
        ):
            errors.append("'options' must have 2-4 options")
        return errors

    async def execute(self, **kwargs: Any) -> Any:
        questions = normalize_ask_payload(kwargs)
        if _handler is not None:
            return await _handler(questions)
        return build_ask_user_result(questions, {q.question: "" for q in questions})


def pending_ask_user_id(messages: list[dict]) -> str | None:
    """Find a pending ask_user tool call without a result."""
    pending: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and isinstance(tc.get("id"), str):
                    fn = tc.get("function") or {}
                    name = fn.get("name") if isinstance(fn, dict) else tc.get("name")
                    if isinstance(name, str):
                        pending[tc["id"]] = name
        elif msg.get("role") == "tool":
            tid = msg.get("tool_call_id")
            if isinstance(tid, str):
                pending.pop(tid, None)
    for tid, name in reversed(pending.items()):
        if name == "ask_user":
            return tid
    return None
