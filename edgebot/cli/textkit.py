"""
edgebot/cli/textkit.py - Display-width text helpers for terminal UIs.

Pure functions: width-aware padding/clipping, ASCII preview boxes, and
ask_user answer formatting.
"""

from prompt_toolkit.utils import get_cwidth


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
