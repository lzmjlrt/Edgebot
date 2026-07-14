import asyncio
import json

from edgebot.tools.builtin.ask import (
    AskUserTool,
    ask_handler_context,
    build_ask_user_result,
    normalize_ask_payload,
    set_ask_handler,
)


def test_ask_user_schema_accepts_structured_questions_and_legacy_question() -> None:
    schema = AskUserTool().parameters

    assert "questions" in schema["properties"]
    assert "question" in schema["properties"]
    option_schema = schema["properties"]["questions"]["items"]["properties"]["options"]["items"]
    object_option_schema = option_schema["anyOf"][1]
    assert "description" in object_option_schema["properties"]
    assert "preview" in object_option_schema["properties"]
    assert "multiSelect" in schema["properties"]["questions"]["items"]["properties"]


def test_legacy_options_schema_matches_normalized_object_options() -> None:
    schema = AskUserTool().parameters
    legacy_option_schema = schema["properties"]["options"]["items"]

    assert legacy_option_schema["anyOf"][0] == {"type": "string"}
    object_schema = legacy_option_schema["anyOf"][1]
    assert object_schema["type"] == "object"
    assert set(object_schema["properties"]) == {"label", "description", "preview"}
    assert object_schema["required"] == ["label"]

    questions = normalize_ask_payload({
        "question": "Choose implementation?",
        "options": [
            {
                "label": "Function (Recommended)",
                "description": "Use a function component",
                "preview": "function Button() {}",
            },
            {
                "label": "Class",
                "description": "Use a class component",
                "preview": "class Button {}",
            },
        ],
    })

    assert questions[0].options[0].label == "Function (Recommended)"
    assert questions[0].options[0].description == "Use a function component"
    assert questions[0].options[0].preview == "function Button() {}"


def test_structured_options_schema_matches_normalized_string_options() -> None:
    schema = AskUserTool().parameters
    structured_option_schema = (
        schema["properties"]["questions"]["items"]["properties"]["options"]["items"]
    )

    assert structured_option_schema["anyOf"][0] == {"type": "string"}

    questions = normalize_ask_payload({
        "questions": [{
            "question": "Choose auth?",
            "header": "Auth",
            "options": ["JWT", "Session"],
        }]
    })

    assert [opt.label for opt in questions[0].options] == ["JWT", "Session", "Other"]


def test_normalize_ask_payload_adds_other_and_drops_preview_for_multiselect() -> None:
    questions = normalize_ask_payload({
        "questions": [
            {
                "question": "Which features?",
                "header": "Features",
                "multiSelect": True,
                "options": [
                    {
                        "label": "Search",
                        "description": "Add search",
                        "preview": "should not be shown",
                    },
                    {"label": "Export", "description": "Add export"},
                ],
            }
        ]
    })

    assert len(questions) == 1
    question = questions[0]
    assert question.question == "Which features?"
    assert question.header == "Features"
    assert question.multi_select is True
    assert [opt.label for opt in question.options] == ["Search", "Export", "Other"]
    assert question.options[0].preview is None
    assert question.options[-1].is_other is True


def test_ask_user_execute_returns_structured_json_from_handler() -> None:
    async def handler(questions):
        return json.dumps({
            "status": "answered",
            "answers": {questions[0].question: questions[0].options[0].label},
            "annotations": {},
        })

    set_ask_handler(handler)
    try:
        result = asyncio.run(AskUserTool().execute(
            questions=[{
                "question": "Choose auth?",
                "header": "Auth",
                "options": [
                    {"label": "JWT (Recommended)", "description": "Stateless token"},
                    {"label": "Session", "description": "Server session"},
                ],
            }]
        ))
    finally:
        set_ask_handler(None)

    payload = json.loads(result)
    assert payload == {
        "status": "answered",
        "answers": {"Choose auth?": "JWT (Recommended)"},
        "annotations": {},
    }


def test_ask_user_is_read_only_but_not_concurrency_safe() -> None:
    tool = AskUserTool()

    assert tool.is_read_only({}) is True
    assert tool.concurrency_safe({}) is False


def test_ask_user_validates_question_or_questions_and_limits() -> None:
    tool = AskUserTool()

    assert "either 'questions' or 'question'" in "; ".join(tool.validate_params({}))
    assert "must contain 1-4 questions" in "; ".join(tool.validate_params({
        "questions": [
            {"question": "Q?", "header": "H", "options": [
                {"label": "A", "description": "a"},
                {"label": "B", "description": "b"},
            ]}
            for _ in range(5)
        ]
    }))
    assert "must have 2-4 options" in "; ".join(tool.validate_params({
        "questions": [{
            "question": "Q?",
            "header": "H",
            "options": [{"label": "A", "description": "a"}],
        }]
    }))


def test_ask_user_rejects_ambiguous_or_duplicate_structured_questions() -> None:
    tool = AskUserTool()
    valid_options = [
        {"label": "First", "description": "First choice"},
        {"label": "Second", "description": "Second choice"},
    ]

    mixed_errors = tool.validate_params({
        "question": "Legacy?",
        "questions": [{
            "question": "Structured?",
            "header": "Choice",
            "options": valid_options,
        }],
    })
    duplicate_question_errors = tool.validate_params({
        "questions": [
            {"question": "Choose?", "header": "One", "options": valid_options},
            {"question": "Choose?", "header": "Two", "options": valid_options},
        ],
    })
    duplicate_option_errors = tool.validate_params({
        "questions": [{
            "question": "Choose?",
            "header": "Choice",
            "options": [
                {"label": "Same", "description": "First choice"},
                {"label": "Same", "description": "Second choice"},
            ],
        }],
    })

    assert "not both" in "; ".join(mixed_errors)
    assert "must be unique" in "; ".join(duplicate_question_errors)
    assert "must be unique" in "; ".join(duplicate_option_errors)


def test_ask_user_returns_unavailable_instead_of_empty_answers_without_handler() -> None:
    set_ask_handler(None)

    result = json.loads(asyncio.run(AskUserTool().execute(
        questions=[{
            "question": "Choose auth?",
            "header": "Auth",
            "options": [
                {"label": "JWT", "description": "Token auth"},
                {"label": "Session", "description": "Cookie auth"},
            ],
        }]
    )))

    assert result == {
        "status": "unavailable",
        "answers": {},
        "annotations": {},
    }


def test_ask_user_handlers_are_isolated_between_concurrent_tasks() -> None:
    async def ask(answer: str) -> str:
        async def handler(questions):
            return build_ask_user_result(
                questions,
                {questions[0].question: answer},
            )

        set_ask_handler(handler)
        await asyncio.sleep(0)
        return await AskUserTool().execute(
            questions=[{
                "question": "Choose auth?",
                "header": "Auth",
                "options": [
                    {"label": "JWT", "description": "Token auth"},
                    {"label": "Session", "description": "Cookie auth"},
                ],
            }]
        )

    async def run_both() -> tuple[str, str]:
        return await asyncio.gather(ask("JWT"), ask("Session"))

    try:
        first, second = asyncio.run(run_both())
    finally:
        set_ask_handler(None)

    assert json.loads(first)["answers"] == {"Choose auth?": "JWT"}
    assert json.loads(second)["answers"] == {"Choose auth?": "Session"}


def test_ask_handler_context_makes_background_execution_unavailable() -> None:
    async def handler(questions):
        return build_ask_user_result(questions, {questions[0].question: "JWT"})

    set_ask_handler(handler)
    payload = {
        "questions": [{
            "question": "Choose auth?",
            "header": "Auth",
            "options": [
                {"label": "JWT", "description": "Token auth"},
                {"label": "Session", "description": "Cookie auth"},
            ],
        }]
    }
    try:
        with ask_handler_context(None):
            unavailable = json.loads(asyncio.run(AskUserTool().execute(**payload)))
        restored = json.loads(asyncio.run(AskUserTool().execute(**payload)))
    finally:
        set_ask_handler(None)

    assert unavailable["status"] == "unavailable"
    assert restored["answers"] == {"Choose auth?": "JWT"}


def test_build_ask_user_result_includes_selected_preview_annotation() -> None:
    questions = normalize_ask_payload({
        "questions": [{
            "question": "Choose component?",
            "header": "Component",
            "options": [
                {
                    "label": "Function",
                    "description": "Use a function component",
                    "preview": "function Button() {}",
                },
                {
                    "label": "Class",
                    "description": "Use a class component",
                    "preview": "class Button {}",
                },
            ],
        }]
    })

    result = json.loads(build_ask_user_result(
        questions,
        {"Choose component?": "Function"},
    ))

    assert result == {
        "status": "answered",
        "answers": {"Choose component?": "Function"},
        "annotations": {
            "Choose component?": {"preview": "function Button() {}"}
        },
    }
