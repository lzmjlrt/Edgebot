"""
edgebot/tools/registry.py - Global singleton instances, tool schema list,
and tool handler dispatch dict.
"""

from __future__ import annotations

from edgebot.background.manager import BackgroundManager
from edgebot.config import CRON_STORE_PATH, LEGACY_SKILLS_DIR, SKILLS_DIR
from edgebot.cron.service import CronService
from edgebot.skills.loader import SkillLoader
from edgebot.subagent.runner import SubagentRunner
from edgebot.tasks.manager import TaskManager
from edgebot.tasks.todo import TodoManager
from edgebot.team.bus import MessageBus
from edgebot.team.teammate import TeammateManager

TODO = TodoManager()
SKILLS = SkillLoader(SKILLS_DIR, extra_workspace_dirs=[LEGACY_SKILLS_DIR] if LEGACY_SKILLS_DIR.exists() else None)
TASK_MGR = TaskManager()
BG = BackgroundManager()
BUS = MessageBus()
TEAM = TeammateManager(BUS, TASK_MGR)
SUBAGENT = SubagentRunner()
CRON = CronService(CRON_STORE_PATH)

TOOL_HANDLERS: dict[str, object] = {}
TOOLS: list[dict] = []
_TOOL_INSTANCES: dict[str, object] = {}


def register_tool(tool_instance) -> None:
    """Register a BaseTool instance."""
    TOOLS.append(tool_instance.to_openai())
    TOOL_HANDLERS[tool_instance.name] = tool_instance.execute
    _TOOL_INSTANCES[tool_instance.name] = tool_instance


def get_tool_instance(name: str):
    """Return a registered tool instance by name."""
    return _TOOL_INSTANCES.get(name)


def set_tool_runtime_context(
    *,
    channel: str = "cli",
    chat_id: str = "direct",
    session_key: str | None = None,
) -> None:
    """Push per-turn runtime context into tools that need it."""
    for tool in _TOOL_INSTANCES.values():
        setter = getattr(tool, "set_runtime_context", None)
        if callable(setter):
            setter(channel=channel, chat_id=chat_id, session_key=session_key)


def init_builtin_tools() -> None:
    from edgebot.tools.builtin.cron import CronTool
    from edgebot.tools.builtin.filesystem import EditFileTool, ReadFileTool, WriteFileTool
    from edgebot.tools.builtin.shell import BashTool
    from edgebot.tools.builtin.tasks import (
        ClaimTaskTool,
        TaskCreateTool,
        TaskGetTool,
        TaskListTool,
        TaskUpdateTool,
    )
    from edgebot.tools.builtin.team_manager import (
        BackgroundRunTool,
        BroadcastTool,
        CheckBackgroundTool,
        CompressTool,
        IdleTool,
        ListTeammatesTool,
        LoadSkillTool,
        PlanApprovalTool,
        ReadInboxTool,
        SendMessageTool,
        ShutdownRequestTool,
        SpawnTeammateTool,
        TaskTool,
        TodoWriteTool,
    )

    for tool in [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        BashTool(),
        TaskCreateTool(),
        TaskGetTool(),
        TaskUpdateTool(),
        TaskListTool(),
        ClaimTaskTool(),
        BroadcastTool(),
        SendMessageTool(),
        ReadInboxTool(),
        SpawnTeammateTool(),
        ListTeammatesTool(),
        TaskTool(),
        TodoWriteTool(),
        LoadSkillTool(),
        IdleTool(),
        ShutdownRequestTool(),
        PlanApprovalTool(),
        BackgroundRunTool(),
        CheckBackgroundTool(),
        CompressTool(),
        CronTool(CRON),
    ]:
        register_tool(tool)


init_builtin_tools()
