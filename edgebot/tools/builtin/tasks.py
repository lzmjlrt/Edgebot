"""
edgebot/tools/builtin/tasks.py - Task management tools.
"""
from typing import Any
from edgebot.tools.base import BaseTool
from edgebot.tools.registry import TASK_MGR

class TaskCreateTool(BaseTool):
    @property
    def name(self) -> str: return "task_create"
    @property
    def description(self) -> str: return "Create a new task"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}
    def execute(self, **kwargs: Any) -> Any: return TASK_MGR.create(kwargs["subject"], kwargs.get("description", ""))

class TaskGetTool(BaseTool):
    @property
    def name(self) -> str: return "task_get"
    @property
    def description(self) -> str: return "Get a task by id"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}
    def execute(self, **kwargs: Any) -> Any: return TASK_MGR.get(kwargs["task_id"])

class TaskUpdateTool(BaseTool):
    @property
    def name(self) -> str: return "task_update"
    @property
    def description(self) -> str: return "Update a task"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"task_id": {"type": "string"}, "status": {"type": "string"}, "add_blocked_by": {"type": "string"}, "add_blocks": {"type": "string"}}, "required": ["task_id"]}
    def execute(self, **kwargs: Any) -> Any: return TASK_MGR.update(kwargs["task_id"], kwargs.get("status"), kwargs.get("add_blocked_by"), kwargs.get("add_blocks"))

class TaskListTool(BaseTool):
    @property
    def name(self) -> str: return "task_list"
    @property
    def description(self) -> str: return "List all tasks"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {}}
    def execute(self, **kwargs: Any) -> Any: return TASK_MGR.list_all()

class ClaimTaskTool(BaseTool):
    @property
    def name(self) -> str: return "claim_task"
    @property
    def description(self) -> str: return "Claim a task to work on"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}
    def execute(self, **kwargs: Any) -> Any: return TASK_MGR.claim(kwargs["task_id"], "lead")