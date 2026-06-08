"""
edgebot/agent/token_budget.py - Model-aware context budget helpers.
"""

from __future__ import annotations

from functools import lru_cache

DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
CONTEXT_SAFETY_MARGIN_TOKENS = 1_024
DEFAULT_CONSOLIDATION_RATIO = 0.6
MIN_INPUT_BUDGET_TOKENS = 1_024

_KNOWN_CONTEXT_WINDOWS = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4.1-nano": 1_047_576,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-sonnet-4": 200_000,
    "gemini-1.5-pro": 1_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
}


def _model_aliases(model: str | None) -> list[str]:
    raw = (model or "").strip().lower()
    if not raw:
        return []
    aliases = [raw]
    if "/" in raw:
        aliases.append(raw.rsplit("/", 1)[-1])
    return aliases


@lru_cache(maxsize=256)
def model_context_window_tokens(model: str | None) -> int:
    """Return the best available context-window size for *model*."""
    for alias in _model_aliases(model):
        if alias in _KNOWN_CONTEXT_WINDOWS:
            return _KNOWN_CONTEXT_WINDOWS[alias]

    litellm_window = _litellm_context_window(model)
    if litellm_window is not None:
        return litellm_window

    raw = (model or "").strip().lower()
    if "claude" in raw:
        return 200_000
    if "gemini" in raw:
        return 1_000_000
    if "o3" in raw or "o4" in raw:
        return 200_000
    return DEFAULT_CONTEXT_WINDOW_TOKENS


def input_token_budget(
    model: str | None,
    *,
    max_completion_tokens: int,
    safety_margin_tokens: int = CONTEXT_SAFETY_MARGIN_TOKENS,
) -> int:
    """Return prompt/input tokens available after reserving output and margin."""
    window = model_context_window_tokens(model)
    reserved = max(0, int(max_completion_tokens)) + max(0, int(safety_margin_tokens))
    return max(MIN_INPUT_BUDGET_TOKENS, window - reserved)


def consolidation_token_target(
    model: str | None,
    *,
    max_completion_tokens: int,
    consolidation_ratio: float = DEFAULT_CONSOLIDATION_RATIO,
    safety_margin_tokens: int = CONTEXT_SAFETY_MARGIN_TOKENS,
) -> int:
    """Return the unconsolidated-history target used by the archiver."""
    ratio = min(1.0, max(0.05, float(consolidation_ratio)))
    return int(input_token_budget(
        model,
        max_completion_tokens=max_completion_tokens,
        safety_margin_tokens=safety_margin_tokens,
    ) * ratio)


def _litellm_context_window(model: str | None) -> int | None:
    if not model:
        return None
    try:
        import litellm
    except Exception:
        return None

    for alias in _model_aliases(model):
        try:
            info = litellm.get_model_info(alias)
        except Exception:
            continue
        for key in ("max_input_tokens", "max_tokens"):
            value = info.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return None
