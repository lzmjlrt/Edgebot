"""Non-interactive one-shot execution for batch runners."""

from __future__ import annotations

import contextlib
import io
import os
import time
from typing import Awaitable, Callable

from edgebot.agent.context import build_system_prompt, seed_workspace_templates
from edgebot.agent.loop import agent_loop
from edgebot.agent.memory import cleanup_memory_files_once
from edgebot.config import LEGACY_SESSION_DIR, MCP_CONFIG_PATH, SESSION_DIR, WORKDIR
from edgebot.mcp.loader import load_mcp
from edgebot.session.store import SessionStore
from edgebot.tools.builtin.ask import build_ask_user_result, set_ask_handler
from edgebot.tools.registry import (
    BG,
    SKILLS,
    TODO,
    TOOL_HANDLERS,
    TOOLS,
    set_batch_permission_prompt_handler,
    set_permission_prompt_handler,
)


async def _allow_permission(_request: dict) -> dict:
    return {"action": "allow"}


async def _allow_batch_permissions(_requests: list[dict]) -> dict:
    return {"action": "allow_all"}


async def _answer_ask_user(questions) -> str:
    if not isinstance(questions, list):
        return ""
    return build_ask_user_result(questions, {question.question: "" for question in questions})


def _install_noninteractive_handlers(*, eval_mode: bool) -> None:
    permission_handler: Callable[[dict], Awaitable[dict]] | None = None
    batch_handler: Callable[[list[dict]], Awaitable[dict]] | None = None
    if eval_mode:
        permission_handler = _allow_permission
        batch_handler = _allow_batch_permissions
    set_permission_prompt_handler(permission_handler)
    set_batch_permission_prompt_handler(batch_handler)
    set_ask_handler(_answer_ask_user)


def _clear_noninteractive_handlers() -> None:
    set_permission_prompt_handler(None)
    set_batch_permission_prompt_handler(None)
    set_ask_handler(None)


@contextlib.contextmanager
def _suppress_startup_output():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield


async def exec_main(instruction: str) -> str:
    """Run Edgebot once for *instruction* and return the final answer."""
    if not instruction or not instruction.strip():
        raise ValueError("instruction is required")

    with _suppress_startup_output():
        seed_workspace_templates()
        SKILLS.reload()
        cleanup_memory_files_once()

    session_key = f"exec_{int(time.time())}"
    store = SessionStore(
        SESSION_DIR,
        workspace=WORKDIR,
        legacy_sessions_dir=LEGACY_SESSION_DIR,
    )
    history = [{"role": "user", "content": instruction}]
    store.update_metadata(session_key, pending_user_turn=True)
    store.append(session_key, history[0])

    mcp_client = None
    _install_noninteractive_handlers(eval_mode=os.getenv("EDGEBOT_EVAL_MODE") == "1")
    try:
        all_tools = list(TOOLS)
        all_handlers = dict(TOOL_HANDLERS)
        with _suppress_startup_output():
            mcp_client = await load_mcp(MCP_CONFIG_PATH)
        if mcp_client:
            all_tools.extend(mcp_client.tool_schemas)
            all_handlers.update(mcp_client.tool_handlers)

        response = await agent_loop(
            messages=history,
            system=build_system_prompt(session_key=session_key),
            tools=all_tools,
            tool_handlers=all_handlers,
            todo_mgr=TODO,
            bg_mgr=BG,
            session_store=store,
            session_key=session_key,
            channel="exec",
            chat_id="direct",
            emit_output=False,
            assistant_label="Edgebot",
        )
        return (response or "").strip()
    finally:
        _clear_noninteractive_handlers()
        if mcp_client:
            await mcp_client.close()
