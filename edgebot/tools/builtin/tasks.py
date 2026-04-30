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
    def parameters(self) -> dict: return {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}, "active_form": {"type": "string"}, "owner": {"type": "string"}, "metadata": {"type": "object"}}, "required": ["subject"]}
    def execute(self, **kwargs: Any) -> Any: return TASK_MGR.create(kwargs["subject"], kwargs.get("description", ""), active_form=kwargs.get("active_form"), owner=kwargs.get("owner"), metadata=kwargs.get("metadata"))

class TaskGetTool(BaseTool):
    @property
    def name(self) -> str: return "task_get"
    @property
    def description(self) -> str: return "Get a task by id"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}
    def is_read_only(self, params: dict[str, Any] | None = None) -> bool: return True
    def execute(self, **kwargs: Any) -> Any: return TASK_MGR.get(int(kwargs["task_id"]))

class TaskUpdateTool(BaseTool):
    @property
    def name(self) -> str: return "task_update"
    @property
    def description(self) -> str: return "Update a task"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"task_id": {"type": "string"}, "subject": {"type": "string"}, "description": {"type": "string"}, "active_form": {"type": "string"}, "owner": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "blocked", "deleted"]}, "add_blocked_by": {"type": "array", "items": {"type": "integer"}}, "add_blocks": {"type": "array", "items": {"type": "integer"}}, "metadata": {"type": "object"}}, "required": ["task_id"]}
    def execute(self, **kwargs: Any) -> Any: return TASK_MGR.update(int(kwargs["task_id"]), kwargs.get("status"), kwargs.get("add_blocked_by"), kwargs.get("add_blocks"), subject=kwargs.get("subject"), description=kwargs.get("description"), active_form=kwargs.get("active_form"), owner=kwargs.get("owner"), metadata=kwargs.get("metadata"))

class TaskListTool(BaseTool):
    @property
    def name(self) -> str: return "task_list"
    @property
    def description(self) -> str: return "List all tasks"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {}}
    def is_read_only(self, params: dict[str, Any] | None = None) -> bool: return True
    def execute(self, **kwargs: Any) -> Any: return TASK_MGR.list_all()

class ClaimTaskTool(BaseTool):
    @property
    def name(self) -> str: return "claim_task"
    @property
    def description(self) -> str: return "Claim a task to work on"
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}
    def execute(self, **kwargs: Any) -> Any: return TASK_MGR.claim(int(kwargs["task_id"]), "lead")
