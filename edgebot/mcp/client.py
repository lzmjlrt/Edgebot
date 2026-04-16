"""
edgebot/mcp/client.py - MCP (Model Context Protocol) client.

Connects to MCP servers via stdio, SSE, or streamableHttp.
Manages session lifecycle with AsyncExitStack.
Tool names are prefixed: mcp_{server_name}_{tool_name}
"""

import asyncio
from contextlib import AsyncExitStack
from typing import Any


def _normalize_schema(schema: Any) -> dict:
    """
    Normalize an MCP JSON schema to be OpenAI-compatible.
    Handles nullable unions, missing properties, etc.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    result = dict(schema)

    # Normalize type: ["string", "null"] → {"type": "string", "nullable": true}
    if isinstance(result.get("type"), list):
        types = result["type"]
        non_null = [t for t in types if t != "null"]
        if non_null:
            result["type"] = non_null[0] if len(non_null) == 1 else non_null
        if "null" in types:
            result["nullable"] = True

    # Recursively normalize properties
    if "properties" in result and isinstance(result["properties"], dict):
        result["properties"] = {
            k: _normalize_schema(v)
            for k, v in result["properties"].items()
        }

    # Normalize array items
    if "items" in result:
        result["items"] = _normalize_schema(result["items"])

    # Ensure object type has properties key
    if result.get("type") == "object" and "properties" not in result:
        result["properties"] = {}

    return result


class MCPClient:
    """
    Manages connections to multiple MCP servers and exposes their tools.

    Usage:
        client = MCPClient(servers_config)
        await client.start()
        # client.tool_schemas  -> list of OpenAI tool dicts
        # client.tool_handlers -> {prefixed_name: async callable}
        await client.call("mcp_server_tool", {"arg": "value"})
        await client.close()
    """

    def __init__(self, servers_config: dict):
        """
        servers_config: {server_name: {type, command, args, env, url, headers, tool_timeout}}
        """
        self._servers_config = servers_config
        self._stack = AsyncExitStack()
        self._sessions: dict[str, Any] = {}   # server_name → ClientSession
        self._tool_map: dict[str, tuple[str, str]] = {}  # prefixed_name → (server_name, original_name)
        self.tool_schemas: list[dict] = []
        self.tool_handlers: dict = {}

    async def start(self) -> None:
        """Connect to all configured MCP servers and discover their tools."""
        await self._stack.__aenter__()
        for server_name, cfg in self._servers_config.items():
            try:
                await self._connect_server(server_name, cfg)
            except Exception as e:
                print(f"[mcp] Failed to connect '{server_name}': {e}")

    async def _connect_server(self, server_name: str, cfg: dict) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        transport_type = cfg.get("type")

        if transport_type == "stdio" or (not cfg.get("url") and cfg.get("command")):
            # stdio transport
            params = StdioServerParameters(
                command=cfg["command"],
                args=cfg.get("args", []),
                env=cfg.get("env") or None,
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))

        elif transport_type == "sse" or (cfg.get("url", "").rstrip("/").endswith("/sse")):
            # SSE transport
            from mcp.client.sse import sse_client
            import httpx

            headers = cfg.get("headers", {})
            url = cfg["url"]
            read, write = await self._stack.enter_async_context(
                sse_client(url, httpx.AsyncClient(headers=headers, timeout=None))
            )

        else:
            # streamableHttp transport
            from mcp.client.streamable_http import streamablehttp_client
            import httpx

            headers = cfg.get("headers", {})
            url = cfg["url"]
            read, write = await self._stack.enter_async_context(
                streamablehttp_client(url, httpx.AsyncClient(headers=headers, timeout=None))
            )

        session = await self._stack.enter_async_context(
            ClientSession(read, write)
        )
        await session.initialize()
        self._sessions[server_name] = session

        # Discover tools
        tool_timeout = cfg.get("tool_timeout", 30)
        result = await session.list_tools()
        for tool in result.tools:
            prefixed = f"mcp_{server_name}_{tool.name}"
            self._tool_map[prefixed] = (server_name, tool.name, tool_timeout)

            # Build OpenAI-compatible schema
            schema = _normalize_schema(
                tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
            )
            self.tool_schemas.append({
                "type": "function",
                "function": {
                    "name": prefixed,
                    "description": tool.description or "",
                    "parameters": schema,
                },
            })

            # Build async handler (capture variables)
            def _make_handler(sname: str, tname: str, timeout: int):
                async def handler(**kwargs):
                    return await self.call(sname, tname, kwargs, timeout=timeout)
                return handler

            self.tool_handlers[prefixed] = _make_handler(server_name, tool.name, tool_timeout)

        print(f"[mcp] Connected '{server_name}': {len(result.tools)} tools")

    async def call(self, server_name: str, tool_name: str, arguments: dict, timeout: int = 30) -> str:
        """Execute a tool call on the specified MCP server."""
        session = self._sessions.get(server_name)
        if not session:
            return f"Error: MCP server '{server_name}' not connected"
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments=arguments),
                timeout=timeout,
            )
            # Extract text content from result
            parts = []
            for content in result.content:
                if hasattr(content, "text"):
                    parts.append(content.text)
                else:
                    parts.append(str(content))
            return "\n".join(parts) if parts else "(no output)"
        except asyncio.TimeoutError:
            return f"Error: MCP tool '{tool_name}' timed out after {timeout}s"
        except Exception as e:
            return f"Error: MCP tool '{tool_name}' failed — {e}"

    async def close(self) -> None:
        """Shut down all MCP connections."""
        await self._stack.__aexit__(None, None, None)
