"""
edgebot/tools/builtin/team_manager.py - Teammate tools ported to BaseTool.
"""
from typing import Any
import json
from edgebot.tools.base import BaseTool
from edgebot.tools.registry import TEAM, BUS, TASK_MGR, TODO, BG, SKILLS
from edgebot.agent.subagent import run_subagent
from edgebot.team.protocols import handle_shutdown_request, handle_plan_review

class BroadcastTool(BaseTool):
    @property
    def name(self) -> str: return "broadcast"
    @property
    def description(self) -> str: return "Broadcast to teammates."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}
    def execute(self, **kwargs: Any) -> Any: return BUS.broadcast("lead", kwargs["content"], TEAM.member_names())

class SendMessageTool(BaseTool):
    @property
    def name(self) -> str: return "send_message"
    @property
    def description(self) -> str: return "Send message to a teammate."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string"}}, "required": ["to", "content"]}
    def execute(self, **kwargs: Any) -> Any: return BUS.send("lead", kwargs["to"], kwargs["content"], kwargs.get("msg_type", "message"))

class ReadInboxTool(BaseTool):
    @property
    def name(self) -> str: return "read_inbox"
    @property
    def description(self) -> str: return "Read inbox of the lead."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {}}
    def execute(self, **kwargs: Any) -> Any: return json.dumps(BUS.read_inbox("lead"), indent=2)

class SpawnTeammateTool(BaseTool):
    @property
    def name(self) -> str: return "spawn_teammate"
    @property
    def description(self) -> str: return "Spawn a new teammate agent."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}
    def execute(self, **kwargs: Any) -> Any: return TEAM.spawn(kwargs["name"], kwargs["role"], kwargs["prompt"])

class ListTeammatesTool(BaseTool):
    @property
    def name(self) -> str: return "list_teammates"
    @property
    def description(self) -> str: return "List active teammates."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {}}
    def execute(self, **kwargs: Any) -> Any: return TEAM.list_all()

class TaskTool(BaseTool):
    @property
    def name(self) -> str: return "task"
    @property
    def description(self) -> str: return "Spawn a subagent for isolated exploration or work."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"prompt": {"type": "string"}, "agent_type": {"type": "string", "enum": ["Explore", "general-purpose"]}}, "required": ["prompt"]}
    def execute(self, **kwargs: Any) -> Any: return run_subagent(kwargs["prompt"], kwargs.get("agent_type", "Explore"))

class TodoWriteTool(BaseTool):
    @property
    def name(self) -> str: return "TodoWrite"
    @property
    def description(self) -> str: return "Update task tracking list."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"]}}}, "required": ["items"]}
    def execute(self, **kwargs: Any) -> Any: return TODO.update(kwargs["items"])

class LoadSkillTool(BaseTool):
    @property
    def name(self) -> str: return "load_skill"
    @property
    def description(self) -> str: return "Load specialized knowledge by name."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    def execute(self, **kwargs: Any) -> Any: return SKILLS.load(kwargs["name"])

class IdleTool(BaseTool):
    @property
    def name(self) -> str: return "idle"
    @property
    def description(self) -> str: return "Idle waiting for teammate response"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {}}
    def execute(self, **kwargs: Any) -> Any: return "Lead does not idle."

class ShutdownRequestTool(BaseTool):
    @property
    def name(self) -> str: return "shutdown_request"
    @property
    def description(self) -> str: return "Shutdown teammate"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}
    def execute(self, **kwargs: Any) -> Any: return handle_shutdown_request(kwargs["teammate"], BUS)

class PlanApprovalTool(BaseTool):
    @property
    def name(self) -> str: return "plan_approval"
    @property
    def description(self) -> str: return "Approve plan"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}
    def execute(self, **kwargs: Any) -> Any: return handle_plan_review(kwargs["request_id"], kwargs["approve"], kwargs.get("feedback", ""), BUS)

class BackgroundRunTool(BaseTool):
    @property
    def name(self) -> str: return "background_run"
    @property
    def description(self) -> str: return "Run process in background"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}
    def execute(self, **kwargs: Any) -> Any: return BG.run(kwargs["command"], kwargs.get("timeout", 120))

class CheckBackgroundTool(BaseTool):
    @property
    def name(self) -> str: return "check_background"
    @property
    def description(self) -> str: return "Check background process"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"task_id": {"type": "string"}}}
    def execute(self, **kwargs: Any) -> Any: return BG.check(kwargs.get("task_id"))

class CompressTool(BaseTool):
    @property
    def name(self) -> str: return "compress"
    @property
    def description(self) -> str: return "Manually compress conversation context."
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {}}
    def execute(self, **kwargs: Any) -> Any: return "Compressing..."
