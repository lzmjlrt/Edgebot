"""
edgebot/mcp/config.py - MCP configuration normalization helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MCPServerConfig:
    """
    Normalized MCP server configuration.

    The loader accepts both Edgebot's legacy ``servers`` shape and the more
    common ``mcpServers`` shape used by Claude Desktop/OpenCode style configs.
    """

    type: str | None = None
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    tool_timeout: int = 30
    enabled_tools: list[str] = field(default_factory=lambda: ["*"])

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MCPServerConfig":
        def _as_str_map(value: Any) -> dict[str, str]:
            if not isinstance(value, dict):
                return {}
            return {str(k): str(v) for k, v in value.items()}

        def _as_str_list(value: Any) -> list[str]:
            if not isinstance(value, list):
                return []
            return [str(item) for item in value]

        tool_timeout = raw.get("tool_timeout", raw.get("toolTimeout", 30))
        try:
            timeout_value = int(tool_timeout)
        except (TypeError, ValueError):
            timeout_value = 30

        enabled_tools = raw.get("enabled_tools", raw.get("enabledTools"))
        if enabled_tools is None:
            enabled_tools = raw.get("tools", ["*"])
        enabled_list = _as_str_list(enabled_tools)
        if not enabled_list:
            enabled_list = ["*"]

        transport_type = raw.get("type")
        if transport_type is not None:
            transport_type = str(transport_type)
            if transport_type == "http":
                url = str(raw.get("url", ""))
                transport_type = "sse" if url.rstrip("/").endswith("/sse") else "streamableHttp"

        return cls(
            type=transport_type,
            command=str(raw.get("command", "")),
            args=_as_str_list(raw.get("args", [])),
            env=_as_str_map(raw.get("env", {})),
            url=str(raw.get("url", "")),
            headers=_as_str_map(raw.get("headers", {})),
            tool_timeout=max(timeout_value, 1),
            enabled_tools=enabled_list,
        )
