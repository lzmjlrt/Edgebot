"""
edgebot/agent/subagent.py - One-shot subagent spawning.
"""

import json

import litellm

from edgebot.config import API_BASE, API_KEY, MODEL
from edgebot.tools.filesystem import run_edit, run_read, run_write
from edgebot.tools.shell import run_bash


def _tool(name: str, description: str, parameters: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    sub_tools = [
        _tool("bash", "Run command.",
              {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}),
        _tool("read_file", "Read file.",
              {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
    ]
    if agent_type != "Explore":
        sub_tools += [
            _tool("write_file", "Write file.",
                  {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}),
            _tool("edit_file", "Edit file.",
                  {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}),
        ]
    sub_handlers = {
        "bash": lambda **kw: run_bash(kw["command"]),
        "read_file": lambda **kw: run_read(kw["path"]),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    }
    sub_msgs = [{"role": "user", "content": prompt}]
    choice = None
    for _ in range(30):
        resp = litellm.completion(
            model=MODEL, messages=sub_msgs, tools=sub_tools, max_tokens=8000,
            api_key=API_KEY, api_base=API_BASE,
        )
        choice = resp.choices[0]
        # Store assistant message
        msg = {"role": "assistant", "content": choice.message.content}
        if choice.message.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in choice.message.tool_calls
            ]
        sub_msgs.append(msg)
        if choice.finish_reason != "tool_calls":
            break
        for tc in choice.message.tool_calls or []:
            args = json.loads(tc.function.arguments)
            h = sub_handlers.get(tc.function.name, lambda **kw: "Unknown tool")
            sub_msgs.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(h(**args))[:50000],
            })
    if choice:
        return choice.message.content or "(no summary)"
    return "(subagent failed)"
