"""
edgebot/cli/repl.py - Interactive REPL.

REPL commands: /compact  /tasks  /team  /inbox
"""

import json

from edgebot.agent.compression import auto_compact
from edgebot.agent.loop import agent_loop, build_system_prompt
from edgebot.tools.registry import BG, BUS, SKILLS, TASK_MGR, TEAM, TODO, TOOL_HANDLERS, TOOLS


def main():
    system = build_system_prompt(SKILLS.descriptions())
    history = []

    while True:
        try:
            query = input("\033[36medgebot >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        query = query.strip()
        if query.lower() in ("q", "exit", ""):
            break

        # Built-in REPL commands
        if query == "/compact":
            if history:
                print("[manual compact via /compact]")
                history[:] = auto_compact(history)
            continue
        if query == "/tasks":
            print(TASK_MGR.list_all())
            continue
        if query == "/team":
            print(TEAM.list_all())
            continue
        if query == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue

        history.append({"role": "user", "content": query})
        agent_loop(
            messages=history,
            system=system,
            tools=TOOLS,
            tool_handlers=TOOL_HANDLERS,
            todo_mgr=TODO,
            bg_mgr=BG,
            bus=BUS,
        )
        print()
