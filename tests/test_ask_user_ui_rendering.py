import asyncio
import json
from contextlib import nullcontext

from prompt_toolkit.keys import Keys

from edgebot.cli import repl
from edgebot.cli.repl import _clip_display, _preview_box
from edgebot.tools.builtin.ask import AskOption, AskQuestion


def _press(captured, key, data=""):
    binding = next(
        item
        for item in captured["key_bindings"].bindings
        if any(bound_key == key for bound_key in item.keys)
    )

    class FakeApp:
        def __init__(self):
            self.result = None

        def invalidate(self):
            pass

        def exit(self, result=None):
            self.result = result

    class FakeEvent:
        def __init__(self):
            self.app = FakeApp()
            self.data = data

    event = FakeEvent()
    binding.handler(event)
    return event.app.result


def test_preview_box_preserves_multiline_content_and_reports_hidden_lines() -> None:
    box = _preview_box("line1\nline2\nline3", width=14, height=2)

    assert box == [
        "+------------+",
        "|line1       |",
        "|... 1 lin...|",
        "+------------+",
    ]


def test_preview_box_uses_placeholder_when_preview_is_missing() -> None:
    box = _preview_box(None, width=24, height=1)

    assert box == [
        "+----------------------+",
        "|No preview available  |",
        "+----------------------+",
    ]


def test_clip_display_handles_wide_characters_without_exceeding_width() -> None:
    clipped = _clip_display("你觉得 Preview 的代码展示效果怎么样?", 12)

    assert clipped.endswith("...")
    assert len(clipped) < len("你觉得 Preview 的代码展示效果怎么样?")


def test_run_ask_question_uses_prompt_toolkit_compatible_styles(monkeypatch) -> None:
    class FakeApplication:
        def __init__(self, **_kwargs):
            pass

        async def run_async(self):
            return "JWT"

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)
    question = AskQuestion(
        question="Choose auth?",
        header="Auth",
        options=[
            AskOption(label="JWT", description="Use tokens", preview="token code"),
            AskOption(label="Session", description="Use cookies", preview="session code"),
        ],
    )

    assert asyncio.run(repl._run_ask_question(question)) == "JWT"


def test_ask_user_handler_replaces_multiselect_other_with_custom_text(monkeypatch) -> None:
    question = AskQuestion(
        question="Which features?",
        header="Features",
        options=[
            AskOption(label="Search"),
            AskOption(label="Other", is_other=True),
        ],
        multi_select=True,
    )

    async def fake_run_ask_questions(_questions):
        return {"Which features?": ["Search", "Audit logs"]}

    monkeypatch.setattr(repl, "_run_ask_questions", fake_run_ask_questions)

    result = json.loads(asyncio.run(repl._ask_user_handler([question])))

    assert result["answers"] == {"Which features?": ["Search", "Audit logs"]}


def test_ask_user_handler_uses_single_flow_for_multiple_questions(monkeypatch) -> None:
    questions = [
        AskQuestion(
            question="Which task type?",
            header="Task",
            options=[AskOption(label="Write code"), AskOption(label="Review code")],
        ),
        AskQuestion(
            question="How should preview work?",
            header="Preview",
            options=[AskOption(label="Good"), AskOption(label="Needs work")],
        ),
    ]

    calls = []

    async def fake_run_ask_questions(received_questions):
        calls.append(received_questions)
        return {
            "Which task type?": "Write code",
            "How should preview work?": "Good",
        }

    async def fail_run_ask_question(_question):
        raise AssertionError("multiple questions should use the combined ask flow")

    monkeypatch.setattr(repl, "_run_ask_questions", fake_run_ask_questions)
    monkeypatch.setattr(repl, "_run_ask_question", fail_run_ask_question)

    result = json.loads(asyncio.run(repl._ask_user_handler(questions)))

    assert calls == [questions]
    assert result["answers"] == {
        "Which task type?": "Write code",
        "How should preview work?": "Good",
    }


def test_combined_ask_flow_requires_submit_review_before_returning(monkeypatch) -> None:
    captured = {}
    initial_render = ""
    review_render = ""

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            nonlocal initial_render
            nonlocal review_render
            body = captured["layout"].container.children[0].content
            fragments = body.text()
            initial_render = "".join(text for _style, text in fragments)
            _press(captured, Keys.ControlM)
            _press(captured, Keys.ControlM)
            fragments = body.text()
            review_render = "".join(text for _style, text in fragments)
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    questions = [
        AskQuestion(
            question="Q1?",
            header="One",
            options=[AskOption(label="A"), AskOption(label="B")],
        ),
        AskQuestion(
            question="Q2?",
            header="Two",
            options=[AskOption(label="C"), AskOption(label="Other", is_other=True)],
        ),
    ]

    assert asyncio.run(repl._run_ask_questions(questions)) == {"Q1?": "A", "Q2?": "C"}

    assert "[ ] One" in initial_render
    assert "[ ] Two" in initial_render
    assert "Submit" in initial_render
    assert "Review your answers" in review_render
    assert "Submit answers" in review_render


def test_type_something_option_opens_editable_custom_answer(monkeypatch) -> None:
    captured = {}
    custom_render = ""

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            nonlocal custom_render
            _press(captured, Keys.Down)
            body = captured["layout"].container.children[0].content
            fragments = body.text()
            custom_render = "".join(text for _style, text in fragments)
            _press(captured, Keys.Any, data="t")
            _press(captured, Keys.Any, data="y")
            _press(captured, Keys.Any, data="p")
            _press(captured, Keys.Any, data="e")
            _press(captured, Keys.ControlH)
            _press(captured, Keys.Any, data="d")
            _press(captured, Keys.ControlM)
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    question = AskQuestion(
        question="Q?",
        header="Question",
        options=[AskOption(label="A"), AskOption(label="Other", is_other=True)],
    )

    assert asyncio.run(repl._run_ask_questions([question])) == {"Q?": "typd"}

    assert "Type something" in custom_render
    assert "Custom answer:" in custom_render


def test_type_something_accepts_j_and_k_as_text(monkeypatch) -> None:
    captured = {}

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            _press(captured, Keys.Down)
            _press(captured, "j", data="j")
            _press(captured, "k", data="k")
            _press(captured, Keys.ControlM)
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    question = AskQuestion(
        question="Q?",
        header="Question",
        options=[AskOption(label="A"), AskOption(label="Other", is_other=True)],
    )

    assert asyncio.run(repl._run_ask_questions([question])) == {"Q?": "jk"}


def test_multiselect_type_something_is_included_without_space_toggle(monkeypatch) -> None:
    captured = {}

    class FakeApplication:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            _press(captured, Keys.Down)
            _press(captured, Keys.Any, data="a")
            _press(captured, Keys.Any, data="u")
            _press(captured, Keys.Any, data="d")
            _press(captured, Keys.Any, data="i")
            _press(captured, Keys.Any, data="t")
            _press(captured, Keys.ControlM)
            return _press(captured, Keys.ControlM)

    monkeypatch.setattr(repl, "Application", FakeApplication)
    monkeypatch.setattr(repl, "patch_stdout", nullcontext)

    question = AskQuestion(
        question="Which features?",
        header="Features",
        options=[AskOption(label="Search"), AskOption(label="Other", is_other=True)],
        multi_select=True,
    )

    assert asyncio.run(repl._run_ask_questions([question])) == {
        "Which features?": ["audit"]
    }


def test_ask_user_handler_marks_cancelled_questions(monkeypatch) -> None:
    async def fake_run_ask_questions(_questions):
        return {}

    monkeypatch.setattr(repl, "_run_ask_questions", fake_run_ask_questions)
    question = AskQuestion(
        question="Choose auth?",
        header="Auth",
        options=[AskOption(label="JWT"), AskOption(label="Session")],
    )

    result = json.loads(asyncio.run(repl._ask_user_handler([question])))

    assert result == {
        "status": "cancelled",
        "answers": {},
        "annotations": {},
    }
