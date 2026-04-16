"""
edgebot/cli/repl.py - Interactive REPL (async).

REPL commands: /compact  /tasks  /team  /inbox
"""

import json

from edgebot.agent.compression import auto_compact
from edgebot.agent.loop import agent_loop, build_system_prompt
from edgebot.config import MCP_CONFIG_PATH, SESSION_DIR
from edgebot.mcp.loader import load_mcp
from edgebot.session.store import SessionStore
from edgebot.tools.registry import BG, BUS, SKILLS, TASK_MGR, TEAM, TODO, TOOL_HANDLERS, TOOLS

SESSION_KEY = "default"


async def main():
    # --- Session persistence ---
    store = SessionStore(SESSION_DIR)
    history = store.load(SESSION_KEY)
    if history:
        print(f"[session] Restored {len(history)} messages from previous session.")

    # --- MCP initialization (optional) ---
    mcp_client = await load_mcp(MCP_CONFIG_PATH)
    all_tools = list(TOOLS)
    all_handlers = dict(TOOL_HANDLERS)
    if mcp_client:
        all_tools.extend(mcp_client.tool_schemas)
        all_handlers.update(mcp_client.tool_handlers)
        print(f"[mcp] {len(mcp_client.tool_schemas)} MCP tools loaded.")

    system = build_system_prompt(SKILLS.descriptions())

    try:
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
                    history[:] = await auto_compact(history)
                    store.save_all(SESSION_KEY, history)
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

            user_msg = {"role": "user", "content": query}
            history.append(user_msg)
            store.append(SESSION_KEY, user_msg)

            await agent_loop(
                messages=history,
                system=system,
                tools=all_tools,
                tool_handlers=all_handlers,
                todo_mgr=TODO,
                bg_mgr=BG,
                bus=BUS,
                session_store=store,
                session_key=SESSION_KEY,
            )
            print()
    finally:
        if mcp_client:
            await mcp_client.close()
