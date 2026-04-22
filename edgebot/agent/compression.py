"""
edgebot/agent/compression.py - Context compression utilities.
"""

import json
import time

import litellm

from edgebot.config import API_BASE, API_KEY, MODEL, TRANSCRIPT_DIR
from edgebot.session.store import find_legal_start

_COMPACT_PREFIX = "[System: Context auto-compressed"
_IDLE_PREFIX = "[System: User was idle"


def estimate_tokens(messages: list) -> int:
    return len(json.dumps(messages, default=str)) // 4


def microcompact(messages: list):
    """Clear old tool-result messages in-place to free context space."""
    tool_msgs = [msg for msg in messages if msg.get("role") == "tool"]
    if len(tool_msgs) <= 3:
        return
    for msg in tool_msgs[:-3]:
        if isinstance(msg.get("content"), str) and len(msg["content"]) > 100:
            msg["content"] = "[cleared]"


def extract_session_summary(messages: list[dict]) -> str | None:
    """
    Extract the synthetic compaction summary from the leading system-like user message.
    """
    if not messages:
        return None
    first = messages[0]
    if first.get("role") != "user":
        return None
    content = first.get("content")
    if not isinstance(content, str):
        return None
    if not (content.startswith(_COMPACT_PREFIX) or content.startswith(_IDLE_PREFIX)):
        return None
    head, sep, tail = content.partition("]\n")
    if not sep:
        return None
    summary = tail.strip()
    return summary or None


async def auto_compact(
    messages: list,
    is_idle: bool = False,
    idle_minutes: int = 0,
    memory_store=None,
) -> list:
    """
    Summarize the prefix of the conversation while keeping the recent suffix intact.
    If is_idle, prefixes the summary with an idle notification.
    """
    if len(messages) <= 10:
        return messages

    # Safely slice the suffix using find_legal_start to avoid orphans
    suffix_candidate = messages[-8:]
    start_idx = find_legal_start(suffix_candidate)
    suffix = suffix_candidate[start_idx:]
    
    # The remainder is the prefix to summarize
    prefix_len = len(messages) - len(suffix)
    prefix = messages[:prefix_len]
    
    if not prefix:
        return messages

    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in prefix:
            f.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")
            
    conv_text = json.dumps(prefix, ensure_ascii=False, default=str)[:80000]
    prompt = f"Summarize the earlier parts of this conversation for continuity:\n{conv_text}"
    
    resp = await litellm.acompletion(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
        api_key=API_KEY, api_base=API_BASE,
    )
    summary = resp.choices[0].message.content

    if memory_store and summary:
        archived_label = (
            f"Idle for {int(idle_minutes)} minutes. "
            if is_idle else
            "Context auto-compressed. "
        )
        memory_store.append_history(archived_label + summary)
    
    if is_idle:
        system_msg = f"[System: User was idle for {int(idle_minutes)} minutes. Previous context summary (log {path.name}):]\n{summary}"
    else:
        system_msg = f"[System: Context auto-compressed to save tokens (log {path.name}). Previous summary:]\n{summary}"

    return [
        {"role": "user", "content": system_msg},
        {"role": "assistant", "content": "Understood. We will continue from the retained recent messages seamlessly."},
    ] + suffix
