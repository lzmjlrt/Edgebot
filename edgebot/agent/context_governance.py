"""
Model-facing transcript governance for AgentRunner.

This module shapes a copy of persisted messages before a provider call. It does
not mutate or decide what belongs in session history.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from edgebot.agent.compression import estimate_tokens
from edgebot.agent.token_budget import input_token_budget
from edgebot.providers.base import is_valid_tool_name
from edgebot.session.store import find_legal_start

DEFAULT_MAX_TOOL_RESULT_TOKENS = 24_000

_MICROCOMPACT_KEEP_RECENT = 10
_MICROCOMPACT_MIN_CHARS = 500
_COMPACTABLE_TOOLS = frozenset({
    "read_file", "bash", "grep", "glob",
    "web_search", "web_fetch", "list_dir",
})
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"


class RequestBudgetError(ValueError):
    """Raised when the non-reducible provider request exceeds its budget."""


@dataclass(slots=True)
class ContextGovernanceConfig:
    """Configuration for preparing a model-facing message copy."""

    model: str
    max_tokens: int = 8192
    max_input_tokens: int | None = None
    max_tool_result_tokens: int | None = DEFAULT_MAX_TOOL_RESULT_TOKENS
    tool_definitions: list[dict[str, Any]] | None = None


def estimate_request_tokens(
    messages: list[dict[str, Any]],
    tool_definitions: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate the complete provider request, including serialized tool schemas."""
    if not tool_definitions:
        return estimate_tokens(messages)
    return estimate_tokens(messages + [{
        "role": "tool_schema",
        "content": tool_definitions,
    }])


def prepare_messages_for_model(
    messages: list[dict[str, Any]],
    config: ContextGovernanceConfig,
) -> list[dict[str, Any]]:
    """Return a governed request copy without changing persisted messages."""
    model_messages = [dict(message) for message in messages]
    model_messages = _strip_malformed_tool_calls(model_messages)
    model_messages = _drop_orphan_tool_results(model_messages)
    model_messages = _backfill_missing_tool_results(model_messages)
    model_messages = _microcompact(model_messages)
    governed_messages = _apply_tool_result_budget(model_messages, config)
    return apply_input_token_budget(governed_messages, config)


def apply_input_token_budget(
    messages: list[dict[str, Any]],
    config: ContextGovernanceConfig,
) -> list[dict[str, Any]]:
    """Return a legal suffix that fits the model-aware input budget."""
    max_input_tokens = config.max_input_tokens
    if max_input_tokens is None:
        max_input_tokens = input_token_budget(
            config.model,
            max_completion_tokens=config.max_tokens,
        )
    max_input_tokens = max(0, int(max_input_tokens))
    system_prefix: list[dict[str, Any]] = []
    body_start = 0
    for idx, message in enumerate(messages):
        if message.get("role") != "system":
            body_start = idx
            break
        system_prefix.append(dict(message))
    else:
        body_start = len(messages)

    if estimate_request_tokens(system_prefix, config.tool_definitions) > max_input_tokens:
        raise RequestBudgetError(
            "System prompt and tool definitions exceed the available request budget."
        )

    if estimate_request_tokens(messages, config.tool_definitions) <= max_input_tokens:
        return _drop_incomplete_tool_call_groups(messages)

    selected: list[dict[str, Any]] = []
    body = messages[body_start:]
    for message in reversed(body):
        candidate = [message] + selected
        if estimate_request_tokens(
            system_prefix + candidate,
            config.tool_definitions,
        ) > max_input_tokens:
            if not selected:
                raise RequestBudgetError(
                    "The most recent message cannot fit within the available request budget."
                )
            break
        selected = candidate

    start = find_legal_start(selected)
    selected = selected[start:]
    for idx, message in enumerate(selected):
        if message.get("role") == "user" or (
            message.get("role") == "assistant" and message.get("tool_calls")
        ):
            selected = selected[idx:]
            break

    repaired = _drop_incomplete_tool_call_groups(selected)
    prepared = system_prefix + [dict(message) for message in repaired]
    if estimate_request_tokens(prepared, config.tool_definitions) > max_input_tokens:
        raise RequestBudgetError("Prepared request exceeds the available request budget.")
    return prepared


def _tool_call_name(tool_call: dict[str, Any]) -> Any:
    function = tool_call.get("function")
    if isinstance(function, dict):
        return function.get("name")
    return tool_call.get("name")


def _strip_malformed_tool_calls(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove malformed assistant tool_calls from the model-facing transcript."""
    updated: list[dict[str, Any]] | None = None
    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            if updated is not None:
                updated.append(dict(msg))
            continue

        valid_tool_calls = [
            dict(tc)
            for tc in msg.get("tool_calls") or []
            if isinstance(tc, dict) and is_valid_tool_name(_tool_call_name(tc))
        ]
        if len(valid_tool_calls) == len(msg.get("tool_calls") or []):
            if updated is not None:
                updated.append(dict(msg))
            continue

        if updated is None:
            updated = [dict(m) for m in messages[:idx]]

        content = msg.get("content")
        has_content = (
            bool(content.strip()) if isinstance(content, str) else content is not None
        )
        if not valid_tool_calls and not has_content:
            continue

        cleaned = dict(msg)
        if valid_tool_calls:
            cleaned["tool_calls"] = valid_tool_calls
        else:
            cleaned.pop("tool_calls", None)
        updated.append(cleaned)

    return updated if updated is not None else messages


def _drop_orphan_tool_results(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop tool results whose tool_call_id has no matching assistant tool_call."""
    declared: set[str] = set()
    updated: list[dict[str, Any]] | None = None
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    declared.add(str(tc["id"]))
        if role == "tool":
            tid = msg.get("tool_call_id")
            if tid and str(tid) not in declared:
                if updated is None:
                    updated = [dict(m) for m in messages[:idx]]
                continue
        if updated is not None:
            updated.append(dict(msg))
    return updated if updated is not None else messages


def _backfill_missing_tool_results(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Insert synthetic error results for assistant tool_calls that lack a result."""
    declared: list[tuple[int, str, str]] = []
    fulfilled: set[str] = set()
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    name = ""
                    func = tc.get("function")
                    if isinstance(func, dict):
                        name = func.get("name", "")
                    declared.append((idx, str(tc["id"]), name))
        elif role == "tool":
            tid = msg.get("tool_call_id")
            if tid:
                fulfilled.add(str(tid))

    missing = [(ai, cid, n) for ai, cid, n in declared if cid not in fulfilled]
    if not missing:
        return messages

    updated = list(messages)
    offset = 0
    for assistant_idx, call_id, name in missing:
        insert_at = assistant_idx + 1 + offset
        while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
            insert_at += 1
        updated.insert(insert_at, {
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": _BACKFILL_CONTENT,
        })
        offset += 1
    return updated


def _microcompact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace old compactable tool results with one-line summaries."""
    compactable_indices: list[int] = []
    for idx, msg in enumerate(messages):
        name = msg.get("name")
        if msg.get("role") == "tool" and name in _COMPACTABLE_TOOLS:
            compactable_indices.append(idx)

    if len(compactable_indices) <= _MICROCOMPACT_KEEP_RECENT:
        return messages

    stale = compactable_indices[:len(compactable_indices) - _MICROCOMPACT_KEEP_RECENT]
    updated: list[dict[str, Any]] | None = None
    for idx in stale:
        content = messages[idx].get("content")
        if not isinstance(content, str) or len(content) < _MICROCOMPACT_MIN_CHARS:
            continue
        name = messages[idx].get("name", "tool")
        summary = f"[{name} result omitted from context]"
        if updated is None:
            updated = [dict(m) for m in messages]
        updated[idx]["content"] = summary

    return updated if updated is not None else messages


def _apply_tool_result_budget(
    messages: list[dict[str, Any]],
    config: ContextGovernanceConfig,
) -> list[dict[str, Any]]:
    """Replace old tool results when aggregate tool output exceeds budget."""
    max_tool_result_tokens = config.max_tool_result_tokens
    if max_tool_result_tokens is None:
        return messages
    max_tool_result_tokens = max(0, int(max_tool_result_tokens))
    tool_indices = [
        idx for idx, msg in enumerate(messages)
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str)
    ]
    if not tool_indices:
        return messages

    tool_messages = [messages[idx] for idx in tool_indices]
    if estimate_tokens(tool_messages) <= max_tool_result_tokens:
        return messages

    keep: set[int] = set()
    kept_messages: list[dict[str, Any]] = []
    for idx in reversed(tool_indices):
        msg = messages[idx]
        candidate = [msg] + kept_messages
        if kept_messages and estimate_tokens(candidate) > max_tool_result_tokens:
            continue
        keep.add(idx)
        kept_messages = candidate
        if estimate_tokens(kept_messages) > max_tool_result_tokens:
            break

    updated = [dict(message) for message in messages]
    for idx in tool_indices:
        if idx in keep:
            continue
        name = updated[idx].get("name") or "tool"
        updated[idx]["content"] = (
            f"[{name} result omitted from context due to tool-result budget]"
        )
    return updated


def _drop_incomplete_tool_call_groups(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove orphan tools and assistant tool_call groups without full results."""
    updated: list[dict[str, Any]] = []
    idx = 0
    changed = False
    while idx < len(messages):
        msg = messages[idx]
        role = msg.get("role")
        if role == "tool":
            changed = True
            idx += 1
            continue
        if role != "assistant" or not msg.get("tool_calls"):
            updated.append(dict(msg))
            idx += 1
            continue

        call_ids = {
            str(tc.get("id"))
            for tc in msg.get("tool_calls") or []
            if isinstance(tc, dict) and tc.get("id")
        }
        group = [dict(msg)]
        found: set[str] = set()
        cursor = idx + 1
        while cursor < len(messages) and messages[cursor].get("role") == "tool":
            tool_msg = messages[cursor]
            group.append(dict(tool_msg))
            tool_call_id = tool_msg.get("tool_call_id")
            if tool_call_id:
                found.add(str(tool_call_id))
            cursor += 1

        if call_ids and call_ids.issubset(found):
            updated.extend(group)
        else:
            changed = True
        idx = cursor

    return updated if changed else messages
