"""
edgebot/cli/repl.py - Interactive REPL (async).

REPL commands: /compact  /tasks  /team  /inbox  /new  /sessions  /memory
"""

import json
import time

from edgebot.agent.compression import auto_compact
from edgebot.agent.context import build_system_prompt, seed_workspace_templates
from edgebot.agent.loop import agent_loop
from edgebot.agent.memory import consolidate_memory
from edgebot.config import MCP_CONFIG_PATH, SESSION_DIR
from edgebot.mcp.loader import load_mcp
from edgebot.session.store import SessionStore
from edgebot.tools.registry import BG, BUS, SKILLS, TASK_MGR, TEAM, TODO, TOOL_HANDLERS, TOOLS


def _time_ago(dt) -> str:
    """Human-readable relative time."""
    delta = time.time() - dt.timestamp()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _select_session(store: SessionStore) -> tuple[str, list[dict]]:
    """
    Interactive session picker at startup.
    Returns (session_key, messages).
    """
    sessions = store.list_sessions()

    if not sessions:
        print("[session] No previous sessions. Starting fresh.")
        return "default", []

    print(f"\n[session] Found {len(sessions)} previous session(s):\n")
    for i, s in enumerate(sessions[:10], 1):
        ago = _time_ago(s["updated_at"])
        print(f"  {i}. {s['key']}  ({s['message_count']} messages, {ago})")

    print()
    print("  c  Continue most recent session")
    print("  n  New session")
    print("  d  Delete a session")
    print()

    while True:
        try:
            choice = input("edgebot session> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "default", []

        if choice == "c" or choice == "":
            key = sessions[0]["key"]
            msgs = store.load(key)
            print(f"[session] Restored {len(msgs)} messages from '{key}'.")
            return key, msgs

        if choice == "n":
            key = f"session_{int(time.time())}"
            print(f"[session] New session '{key}'.")
            return key, []

        if choice == "d":
            idx = input("  Delete which #? ").strip()
            try:
                s = sessions[int(idx) - 1]
                store.delete(s["key"])
                sessions.remove(s)
                print(f"  Deleted '{s['key']}'.")
            except (ValueError, IndexError):
                print("  Invalid choice.")
            continue

        # Numeric selection
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(sessions):
                key = sessions[idx]["key"]
                msgs = store.load(key)
                print(f"[session] Restored {len(msgs)} messages from '{key}'.")
                return key, msgs
        except ValueError:
            pass

        print("  Invalid choice. Enter c, n, d, or a number.")


async def main():
    # --- Seed workspace templates (first run) ---
    seed_workspace_templates()

    # --- Session selection ---
    store = SessionStore(SESSION_DIR)
    session_key, history = _select_session(store)

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
                    store.save_all(session_key, history)
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
            if query == "/new":
                session_key = f"session_{int(time.time())}"
                history.clear()
                print(f"[session] New session '{session_key}'.")
                continue
            if query == "/sessions":
                for s in store.list_sessions()[:10]:
                    ago = _time_ago(s["updated_at"])
                    marker = " <-" if s["key"] == session_key else ""
                    print(f"  {s['key']}  ({s['message_count']} msgs, {ago}){marker}")
                continue
            if query == "/memory":
                print("[memory] Running consolidation...")
                await consolidate_memory(history)
                continue

            user_msg = {"role": "user", "content": query}
            history.append(user_msg)
            store.append(session_key, user_msg)

            await agent_loop(
                messages=history,
                system=system,
                tools=all_tools,
                tool_handlers=all_handlers,
                todo_mgr=TODO,
                bg_mgr=BG,
                bus=BUS,
                session_store=store,
                session_key=session_key,
            )
            print()
    finally:
        # Final memory consolidation on exit
        if len(history) >= 4:
            print("[memory] Consolidating before exit...")
            try:
                await consolidate_memory(history)
            except Exception as e:
                print(f"[memory] Error: {e}")
        if mcp_client:
            await mcp_client.close()
