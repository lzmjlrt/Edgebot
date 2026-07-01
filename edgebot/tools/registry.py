"""
edgebot/tools/registry.py - Tool registry objects plus default runtime tools.
"""

from __future__ import annotations

import json
from typing import Any

from edgebot.background.manager import BackgroundManager
from edgebot.config import CRON_STORE_PATH, LEGACY_SKILLS_DIR, PERMISSIONS_FILE, SKILLS_DIR
from edgebot.cron.service import CronService
from edgebot.permissions import PermissionManager
from edgebot.skills.loader import SkillLoader
from edgebot.subagent.runner import SubagentRunner
from edgebot.tasks.manager import TaskManager
from edgebot.tasks.todo import TodoManager
from edgebot.tools.base import BaseTool


class ToolRegistry:
    """Own a pool of tool instances and the behavior needed to call them."""

    def __init__(self):
        self._tools: dict[str, object] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None

    def register(self, tool: object) -> None:
        """Register a tool instance in this registry."""
        self._tools[getattr(tool, "name")] = tool
        self._cached_definitions = None

    def unregister(self, name: str) -> None:
        """Remove a tool instance from this registry if present."""
        self._tools.pop(name, None)
        self._cached_definitions = None

    def get(self, name: str) -> object | None:
        """Return a registered tool instance by exact name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Return True when a tool is registered by exact name."""
        return name in self._tools

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        fn = schema.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            return fn["name"]
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """Return cached OpenAI-compatible tool definitions."""
        if self._cached_definitions is not None:
            return self._cached_definitions

        definitions = [
            tool.to_openai()
            for tool in self._tools.values()
            if callable(getattr(tool, "to_openai", None))
        ]
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        for schema in definitions:
            name = self._schema_name(schema)
            if name.startswith("mcp_"):
                mcp_tools.append(schema)
            else:
                builtins.append(schema)
        builtins.sort(key=self._schema_name)
        mcp_tools.sort(key=self._schema_name)
        self._cached_definitions = builtins + mcp_tools
        return self._cached_definitions

    @property
    def handlers(self) -> dict[str, object]:
        """Return execute callables keyed by tool name for legacy callers."""
        return {
            name: tool.execute
            for name, tool in self._tools.items()
            if callable(getattr(tool, "execute", None))
        }

    @property
    def tool_names(self) -> list[str]:
        """Return registered tool names."""
        return list(self._tools.keys())

    @staticmethod
    def _lookup_key(name: str) -> str:
        return "".join(ch.lower() for ch in name if ch.isalnum())

    def _suggest_name(self, name: str) -> str | None:
        key = self._lookup_key(str(name or ""))
        if not key:
            return None
        matches = [
            registered
            for registered in self._tools
            if self._lookup_key(registered) == key
        ]
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _coerce_argument_value(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return {}
        if not stripped.startswith(("{", "[")):
            return value

        try:
            return json.loads(stripped)
        except Exception:
            return value

    @classmethod
    def _coerce_params(cls, tool: object, params: Any) -> Any:
        params = cls._coerce_argument_value(params)
        return cls._unwrap_arguments_payload(tool, params)

    @classmethod
    def _unwrap_arguments_payload(cls, tool: object, params: Any) -> Any:
        if not isinstance(params, dict) or set(params) != {"arguments"}:
            return params
        parameters = getattr(tool, "parameters", {}) or {}
        properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
        if isinstance(properties, dict) and "arguments" in properties:
            return params
        return cls._coerce_argument_value(params.get("arguments"))

    def prepare_call(self, name: str, params: Any) -> tuple[object | None, Any, str | None]:
        """Resolve, coerce, cast, and validate one tool call."""
        tool = self.get(name)
        if tool is None:
            suggestion = self._suggest_name(str(name))
            hint = (
                f" Did you mean '{suggestion}'? Tool names must match exactly."
                if suggestion
                else ""
            )
            available = ", ".join(self.tool_names)
            return None, params, f"Error: Tool '{name}' not found.{hint} Available: {available}"

        params = self._coerce_params(tool, params)
        if not isinstance(params, dict):
            return tool, params, (
                f"Error: Tool '{name}' parameters must be a JSON object, "
                f"got {type(params).__name__}"
            )

        cast_params = tool.cast_params(params)
        errors = tool.validate_params(cast_params)
        if errors:
            return tool, cast_params, (
                f"Error: Invalid parameters for tool '{name}': {'; '.join(errors)}"
            )
        return tool, cast_params, None

    async def execute(self, name: str, params: Any) -> object:
        """Execute one registered tool after preparation and validation."""
        tool, cast_params, error = self.prepare_call(name, params)
        if error:
            return error
        assert tool is not None
        result = tool.execute(**cast_params)
        if hasattr(result, "__await__"):
            return await result
        return result

    def set_runtime_context(
        self,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        session_key: str | None = None,
    ) -> None:
        """Push per-turn runtime context into tools that need it."""
        for tool in self._tools.values():
            setter = getattr(tool, "set_runtime_context", None)
            if callable(setter):
                setter(channel=channel, chat_id=chat_id, session_key=session_key)

    def __contains__(self, name: str) -> bool:
        return self.has(name)

    def __len__(self) -> int:
        return len(self._tools)


class SchemaToolAdapter(BaseTool):
    """Adapt an OpenAI schema plus handler callable into a registry tool."""

    def __init__(self, schema: dict[str, Any], handler: object):
        self._schema = schema
        self._handler = handler
        function = schema.get("function") if isinstance(schema, dict) else None
        self._function = function if isinstance(function, dict) else {}

    @property
    def name(self) -> str:
        return str(self._function.get("name") or "")

    @property
    def description(self) -> str:
        return str(self._function.get("description") or self.name)

    @property
    def parameters(self) -> dict:
        params = self._function.get("parameters")
        return params if isinstance(params, dict) else {"type": "object", "properties": {}}

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return super().cast_params(params)

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        return super().validate_params(params)

    def is_read_only(self, params: dict[str, Any] | None = None) -> bool:
        return False

    def concurrency_safe(self, params: dict[str, Any] | None = None) -> bool:
        return False

    def execute(self, **kwargs: Any) -> Any:
        return self._handler(**kwargs)

    def to_openai(self) -> dict:
        return self._schema


def build_runtime_tool_registry(
    base_registry: ToolRegistry,
    tool_schemas: list[dict[str, Any]] | None,
    tool_handlers: dict[str, Any] | None,
) -> ToolRegistry:
    """Build a per-run registry that preserves base tools and adapts extras."""
    registry = ToolRegistry()
    for name in base_registry.tool_names:
        tool = base_registry.get(name)
        if tool is not None:
            registry.register(tool)

    base_names = set(base_registry.tool_names)
    handlers = tool_handlers or {}
    for schema in tool_schemas or []:
        name = ToolRegistry._schema_name(schema)
        if not name or name in base_names or name not in handlers:
            continue
        registry.register(SchemaToolAdapter(schema, handlers[name]))
    return registry


TODO = TodoManager()
SKILLS = SkillLoader(SKILLS_DIR, extra_workspace_dirs=[LEGACY_SKILLS_DIR] if LEGACY_SKILLS_DIR.exists() else None)
TASK_MGR = TaskManager()
BG = BackgroundManager()
SUBAGENT = SubagentRunner()
CRON = CronService(CRON_STORE_PATH)
PERMISSIONS = PermissionManager(PERMISSIONS_FILE)

DEFAULT_TOOL_REGISTRY = ToolRegistry()
TOOL_HANDLERS: dict[str, object] = {}
TOOLS: list[dict] = []
_TOOL_INSTANCES: dict[str, object] = {}


def _sync_legacy_tool_views() -> None:
    """Keep module-level compatibility views aligned with the default registry."""
    TOOLS[:] = DEFAULT_TOOL_REGISTRY.get_definitions()
    TOOL_HANDLERS.clear()
    TOOL_HANDLERS.update(DEFAULT_TOOL_REGISTRY.handlers)
    _TOOL_INSTANCES.clear()
    _TOOL_INSTANCES.update(DEFAULT_TOOL_REGISTRY._tools)


def register_tool(tool_instance) -> None:
    """Register a BaseTool instance."""
    DEFAULT_TOOL_REGISTRY.register(tool_instance)
    _sync_legacy_tool_views()


def get_tool_instance(name: str):
    """Return a registered tool instance by name."""
    return DEFAULT_TOOL_REGISTRY.get(name)


def set_permission_prompt_handler(handler) -> None:
    """Register an async permission prompt handler for interactive sessions."""
    PERMISSIONS.set_prompt_handler(handler)


def set_batch_permission_prompt_handler(handler) -> None:
    """Register an async batch permission prompt handler for interactive sessions."""
    PERMISSIONS.set_batch_prompt_handler(handler)


def prepare_call(name: str, params: dict) -> tuple[object | None, dict, str | None]:
    """Resolve a tool and validate its parameters before execution."""
    return DEFAULT_TOOL_REGISTRY.prepare_call(name, params)


async def execute_registered_tool(name: str, params: dict) -> object:
    """Execute a registered tool with parameter preparation/validation."""
    return await DEFAULT_TOOL_REGISTRY.execute(name, params)


def set_tool_runtime_context(
    *,
    channel: str = "cli",
    chat_id: str = "direct",
    session_key: str | None = None,
) -> None:
    """Push per-turn runtime context into tools that need it."""
    DEFAULT_TOOL_REGISTRY.set_runtime_context(
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
    )


def init_builtin_tools() -> None:
    from edgebot.tools.builtin.cron import CronTool
    from edgebot.tools.builtin.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
    from edgebot.tools.builtin.shell import BashTool
    from edgebot.tools.builtin.tasks import (
        ClaimTaskTool,
        TaskCreateTool,
        TaskGetTool,
        TaskListTool,
        TaskUpdateTool,
    )
    from edgebot.tools.builtin.background import (
        BackgroundRunTool,
        CheckBackgroundTool,
        TaskOutputTool,
    )
    from edgebot.tools.builtin.skills import LoadSkillTool
    from edgebot.tools.builtin.ask import AskUserTool
    from edgebot.tools.builtin.web import WebFetchTool, WebSearchTool
    from edgebot.tools.builtin.subagent import (
        CheckSubagentTool,
        ControlSubagentTool,
        ListSubagentsTool,
        TaskTool,
        WaitSubagentTool,
    )
    from edgebot.tools.builtin.todo import CompressTool, TodoWriteTool

    for tool in [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListDirTool(),
        BashTool(),
        TaskCreateTool(),
        TaskGetTool(),
        TaskUpdateTool(),
        TaskListTool(),
        ClaimTaskTool(),
        TaskTool(),
        TodoWriteTool(),
        LoadSkillTool(),
        WebFetchTool(),
        WebSearchTool(),
        BackgroundRunTool(),
        CheckBackgroundTool(),
        CheckSubagentTool(),
        ListSubagentsTool(),
        ControlSubagentTool(),
        WaitSubagentTool(),
        TaskOutputTool(),
        AskUserTool(),
        CompressTool(),
        CronTool(CRON),
    ]:
        register_tool(tool)


init_builtin_tools()
