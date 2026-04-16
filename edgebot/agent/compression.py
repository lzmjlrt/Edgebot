"""
edgebot/agent/compression.py - Context compression utilities.
"""

import json
import time

import litellm

from edgebot.config import API_BASE, API_KEY, MODEL, TRANSCRIPT_DIR


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


async def auto_compact(messages: list) -> list:
    """Summarize the full conversation and replace with a compact seed."""
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    conv_text = json.dumps(messages, default=str)[:80000]
    resp = await litellm.acompletion(
        model=MODEL,
        messages=[{"role": "user", "content": f"Summarize for continuity:\n{conv_text}"}],
        max_tokens=2000,
        api_key=API_KEY, api_base=API_BASE,
    )
    summary = resp.choices[0].message.content
    return [
        {"role": "user", "content": f"[Compressed. Transcript: {path}]\n{summary}"},
        {"role": "assistant", "content": "Understood. Continuing with summary context."},
    ]
