"""
edgebot/tools/registry.py - Global singleton instances, tool schema list,
and tool handler dispatch dict.

This module is the single assembly point that wires all sub-systems together.
Import TOOLS and TOOL_HANDLERS from here for use in the agent loop.
"""

from edgebot.background.manager import BackgroundManager
from edgebot.config import SKILLS_DIR
from edgebot.skills.loader import SkillLoader
from edgebot.tasks.manager import TaskManager
from edgebot.tasks.todo import TodoManager
from edgebot.team.bus import MessageBus
from edgebot.team.teammate import TeammateManager

# ---------------------------------------------------------------------------
# Global singleton instances
# ---------------------------------------------------------------------------
TODO = TodoManager()
SKILLS = SkillLoader(SKILLS_DIR)
TASK_MGR = TaskManager()
BG = BackgroundManager()
BUS = MessageBus()
TEAM = TeammateManager(BUS, TASK_MGR)

# ---------------------------------------------------------------------------
# Tool registration capabilities
# ---------------------------------------------------------------------------
TOOL_HANDLERS = {}
TOOLS = []

def register_tool(tool_instance):
    """Register a BaseTool instance."""
    TOOLS.append(tool_instance.to_openai())
    TOOL_HANDLERS[tool_instance.name] = tool_instance.execute

# Manually register built-ins to avoid circular imports during setup
def init_builtin_tools():
    from edgebot.tools.builtin.filesystem import ReadFileTool, WriteFileTool, EditFileTool
    from edgebot.tools.builtin.shell import BashTool
    from edgebot.tools.builtin.tasks import TaskCreateTool, TaskGetTool, TaskUpdateTool, TaskListTool, ClaimTaskTool
    from edgebot.tools.builtin.team_manager import (
        BroadcastTool, SendMessageTool, ReadInboxTool, SpawnTeammateTool, 
        ListTeammatesTool, TaskTool, TodoWriteTool, LoadSkillTool, IdleTool, 
        ShutdownRequestTool, PlanApprovalTool, BackgroundRunTool, CheckBackgroundTool, CompressTool
    )
    
    for t in [
        ReadFileTool(), WriteFileTool(), EditFileTool(),
        BashTool(),
        TaskCreateTool(), TaskGetTool(), TaskUpdateTool(), TaskListTool(), ClaimTaskTool(),
        BroadcastTool(), SendMessageTool(), ReadInboxTool(), SpawnTeammateTool(),
        ListTeammatesTool(), TaskTool(), TodoWriteTool(), LoadSkillTool(), IdleTool(),
        ShutdownRequestTool(), PlanApprovalTool(), BackgroundRunTool(), CheckBackgroundTool(), CompressTool()
    ]:
        register_tool(t)

# Initialize them right away so TOOLS mapped properties are available statically
init_builtin_tools()
"""
edgebot/tools/registry.py - Global singleton instances, tool schema list,
and tool handler dispatch dict.

This module is the single assembly point that wires all sub-systems together.
Import TOOLS and TOOL_HANDLERS from here for use in the agent loop.
"""

import json

from edgebot.agent.subagent import run_subagent
from edgebot.background.manager import BackgroundManager
from edgebot.config import SKILLS_DIR, VALID_MSG_TYPES
from edgebot.skills.loader import SkillLoader
from edgebot.subagent.runner import SubagentRunner
from edgebot.tasks.manager import TaskManager
from edgebot.tasks.todo import TodoManager
from edgebot.team.bus import MessageBus
from edgebot.team.protocols import handle_plan_review, handle_shutdown_request
from edgebot.team.teammate import TeammateManager
from edgebot.tools.filesystem import run_edit, run_read, run_write
from edgebot.tools.shell import run_bash


def _make_tool(name: str, description: str, parameters: dict) -> dict:
    """Build an OpenAI function-calling tool schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }

# ---------------------------------------------------------------------------
# Global singleton instances
# ---------------------------------------------------------------------------
TODO = TodoManager()
SKILLS = SkillLoader(SKILLS_DIR)
TASK_MGR = TaskManager()
BG = BackgroundManager()
BUS = MessageBus()
TEAM = TeammateManager(BUS, TASK_MGR)
SUBAGENT = SubagentRunner()

# ---------------------------------------------------------------------------
# Tool handler dispatch
# ---------------------------------------------------------------------------
TOOL_HANDLERS = {
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "TodoWrite":        lambda **kw: TODO.update(kw["items"]),
    "task":             lambda **kw: run_subagent(kw["prompt"], kw.get("agent_type", "Explore")),  # returns coroutine
    "load_skill":       lambda **kw: SKILLS.load(kw["name"]),
    "compress":         lambda **kw: "Compressing...",
    "background_run":   lambda **kw: BG.run(kw["command"], kw.get("timeout", 120)),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
    "task_create":      lambda **kw: TASK_MGR.create(kw["subject"], kw.get("description", "")),
    "task_get":         lambda **kw: TASK_MGR.get(kw["task_id"]),
    "task_update":      lambda **kw: TASK_MGR.update(
                            kw["task_id"], kw.get("status"),
                            kw.get("add_blocked_by"), kw.get("add_blocks")),
    "task_list":        lambda **kw: TASK_MGR.list_all(),
    "spawn_teammate":   lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":   lambda **kw: TEAM.list_all(),
    "send_message":     lambda **kw: BUS.send(
                            "lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":       lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":        lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"], BUS),
    "plan_approval":    lambda **kw: handle_plan_review(
                            kw["request_id"], kw["approve"], kw.get("feedback", ""), BUS),
    "idle":             lambda **kw: "Lead does not idle.",
    "claim_task":       lambda **kw: TASK_MGR.claim(kw["task_id"], "lead"),
    "spawn_subagent":   lambda **kw: json.dumps(SUBAGENT.spawn(
                            kw["capability"], kw["prompt"], kw.get("name", ""))),
    "check_subagent":   lambda **kw: json.dumps(SUBAGENT.status(kw["task_id"]), indent=2),
    "list_subagents":   lambda **kw: json.dumps(SUBAGENT.list_all(), indent=2),
}

# ---------------------------------------------------------------------------
# Tool schema definitions (OpenAI function-calling format for litellm)
# ---------------------------------------------------------------------------
TOOLS = [
    _make_tool("bash", "Run a shell command.",
               {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}),
    _make_tool("read_file", "Read file contents.",
               {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}),
    _make_tool("write_file", "Write content to file.",
               {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}),
    _make_tool("edit_file", "Replace exact text in file.",
               {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}),
    _make_tool("TodoWrite", "Update task tracking list.",
               {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
                   "content": {"type": "string"},
                   "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                   "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"]}}}, "required": ["items"]}),
    _make_tool("task", "Spawn a subagent for isolated exploration or work.",
               {"type": "object", "properties": {"prompt": {"type": "string"}, "agent_type": {"type": "string", "enum": ["Explore", "general-purpose"]}}, "required": ["prompt"]}),
    _make_tool("load_skill", "Load specialized knowledge by name.",
               {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    _make_tool("compress", "Manually compress conversation context.",
               {"type": "object", "properties": {}}),
    _make_tool("background_run", "Run command in background thread.",
               {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}),
    _make_tool("check_background", "Check background task status.",
               {"type": "object", "properties": {"task_id": {"type": "string"}}}),
    _make_tool("task_create", "Create a persistent file task.",
               {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}),
    _make_tool("task_get", "Get task details by ID.",
               {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}),
    _make_tool("task_update", "Update task status or dependencies.",
               {"type": "object", "properties": {
                   "task_id": {"type": "integer"},
                   "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]},
                   "add_blocked_by": {"type": "array", "items": {"type": "integer"}},
                   "add_blocks": {"type": "array", "items": {"type": "integer"}}},
                   "required": ["task_id"]}),
    _make_tool("task_list", "List all tasks.",
               {"type": "object", "properties": {}}),
    _make_tool("spawn_teammate", "Spawn a persistent autonomous teammate.",
               {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}),
    _make_tool("list_teammates", "List all teammates.",
               {"type": "object", "properties": {}}),
    _make_tool("send_message", "Send a message to a teammate.",
               {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}),
    _make_tool("read_inbox", "Read and drain the lead's inbox.",
               {"type": "object", "properties": {}}),
    _make_tool("broadcast", "Send message to all teammates.",
               {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}),
    _make_tool("shutdown_request", "Request a teammate to shut down.",
               {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}),
    _make_tool("plan_approval", "Approve or reject a teammate's plan.",
               {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}),
    _make_tool("idle", "Enter idle state.",
               {"type": "object", "properties": {}}),
    _make_tool("claim_task", "Claim a task from the board.",
               {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}),
    _make_tool("spawn_subagent",
               "Spawn a one-shot subagent for a focused task. Returns a task_id immediately; "
               "poll with check_subagent to retrieve the result. "
               "Capabilities: explore (read-only investigation), builder (implement changes), "
               "reviewer (structured code review).",
               {"type": "object",
                "properties": {
                    "capability": {"type": "string",
                                   "enum": ["explore", "builder", "reviewer"]},
                    "prompt": {"type": "string",
                               "description": "Self-contained task description."},
                    "name": {"type": "string",
                             "description": "Optional human-readable id."}},
                "required": ["capability", "prompt"]}),
    _make_tool("check_subagent", "Check a subagent's status/result by task_id.",
               {"type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"]}),
    _make_tool("list_subagents", "List all subagents in this session.",
               {"type": "object", "properties": {}}),
]
