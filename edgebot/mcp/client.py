"""
edgebot/mcp/client.py - MCP (Model Context Protocol) client.

Connects to MCP servers via stdio, SSE, or streamable HTTP.
Registers tools, resources, and prompts using OpenAI-compatible schemas.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import httpx

from edgebot.mcp.config import MCPServerConfig


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    """Return the single non-null branch for nullable unions."""
    if not isinstance(options, list):
        return None

    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)

    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _normalize_schema(schema: Any) -> dict[str, Any]:
    """Normalize MCP JSON schema to be OpenAI-compatible."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}, "required": []}

    normalized = dict(schema)

    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    if "properties" in normalized and isinstance(normalized["properties"], dict):
        normalized["properties"] = {
            name: _normalize_schema(prop) if isinstance(prop, dict) else prop
            for name, prop in normalized["properties"].items()
        }

    if "items" in normalized and isinstance(normalized["items"], dict):
        normalized["items"] = _normalize_schema(normalized["items"])

    if normalized.get("type") == "object":
        normalized.setdefault("properties", {})
        normalized.setdefault("required", [])

    return normalized


class MCPClient:
    """
    Manages connections to multiple MCP servers and exposes their capabilities.

    Public fields:
      - tool_schemas: OpenAI tool definitions
      - tool_handlers: async callables keyed by tool name
      - connected_servers: server names that connected successfully
    """

    def __init__(self, servers_config: dict[str, MCPServerConfig]):
        self._servers_config = servers_config
        self._server_stacks: dict[str, AsyncExitStack] = {}
        self.tool_schemas: list[dict[str, Any]] = []
        self.tool_handlers: dict[str, Any] = {}
        self.connected_servers: list[str] = []
        self.server_capabilities: dict[str, dict[str, list[str]]] = {}

    async def start(self) -> None:
        """Connect to all configured MCP servers in parallel."""
        self.connected_servers = []
        self.server_capabilities = {}
        tasks = [
            asyncio.create_task(self._connect_single_server(name, cfg))
            for name, cfg in self._servers_config.items()
        ]
        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        names = list(self._servers_config.keys())
        for idx, result in enumerate(results):
            server_name = names[idx]
            if isinstance(result, BaseException):
                if not isinstance(result, asyncio.CancelledError):
                    print(f"[mcp] Failed to connect '{server_name}': {result}")
                continue
            if result is None:
                continue
            stack, capability_summary = result
            self._server_stacks[server_name] = stack
            self.connected_servers.append(server_name)
            self.server_capabilities[server_name] = capability_summary

    async def _connect_single_server(
        self, server_name: str, cfg: MCPServerConfig
    ) -> tuple[AsyncExitStack, dict[str, list[str]]] | None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        server_stack = AsyncExitStack()
        await server_stack.__aenter__()

        try:
            transport_type = cfg.type
            if not transport_type:
                if cfg.command:
                    transport_type = "stdio"
                elif cfg.url:
                    transport_type = "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                else:
                    print(f"[mcp] Skipping '{server_name}': no command or url configured")
                    await server_stack.aclose()
                    return None

            if transport_type == "stdio":
                params = StdioServerParameters(
                    command=cfg.command,
                    args=cfg.args,
                    env=cfg.env or None,
                )
                read, write = await server_stack.enter_async_context(stdio_client(params))
            elif transport_type == "sse":
                def httpx_client_factory(
                    headers: dict[str, Any] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    merged_headers = {
                        "Accept": "application/json, text/event-stream",
                        **cfg.headers,
                        **(headers or {}),
                    }
                    return httpx.AsyncClient(
                        headers=merged_headers or None,
                        follow_redirects=True,
                        timeout=timeout,
                        auth=auth,
                    )

                read, write = await server_stack.enter_async_context(
                    sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                )
            elif transport_type == "streamableHttp":
                http_client = await server_stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await server_stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                print(f"[mcp] Skipping '{server_name}': unknown transport type '{transport_type}'")
                await server_stack.aclose()
                return None

            session = await server_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            registered_count = 0
            capability_summary: dict[str, list[str]] = {
                "tools": [],
                "resources": [],
                "prompts": [],
            }
            tools_result = await session.list_tools()
            enabled_tools = set(cfg.enabled_tools)
            allow_all_tools = "*" in enabled_tools
            matched_enabled_tools: set[str] = set()
            available_raw_names = [tool_def.name for tool_def in tools_result.tools]
            available_wrapped_names = [
                f"mcp_{server_name}_{tool_def.name}" for tool_def in tools_result.tools
            ]

            for tool_def in tools_result.tools:
                wrapped_name = f"mcp_{server_name}_{tool_def.name}"
                if (
                    not allow_all_tools
                    and tool_def.name not in enabled_tools
                    and wrapped_name not in enabled_tools
                ):
                    continue

                schema = _normalize_schema(
                    tool_def.inputSchema or {"type": "object", "properties": {}, "required": []}
                )
                self.tool_schemas.append({
                    "type": "function",
                    "function": {
                        "name": wrapped_name,
                        "description": tool_def.description or tool_def.name,
                        "parameters": schema,
                    },
                })
                self.tool_handlers[wrapped_name] = self._make_tool_handler(
                    session,
                    tool_name=tool_def.name,
                    timeout=cfg.tool_timeout,
                )
                registered_count += 1
                capability_summary["tools"].append(wrapped_name)

                if tool_def.name in enabled_tools:
                    matched_enabled_tools.add(tool_def.name)
                if wrapped_name in enabled_tools:
                    matched_enabled_tools.add(wrapped_name)

            if enabled_tools and not allow_all_tools:
                unmatched = sorted(enabled_tools - matched_enabled_tools)
                if unmatched:
                    print(
                        f"[mcp] Server '{server_name}': enabled_tools not found: "
                        f"{', '.join(unmatched)}. Available raw names: "
                        f"{', '.join(available_raw_names) or '(none)'}. Available wrapped names: "
                        f"{', '.join(available_wrapped_names) or '(none)'}"
                    )

            try:
                resources_result = await session.list_resources()
                for resource in resources_result.resources:
                    wrapped_name = f"mcp_{server_name}_resource_{resource.name}"
                    self.tool_schemas.append({
                        "type": "function",
                        "function": {
                            "name": wrapped_name,
                            "description": (
                                f"[MCP Resource] {resource.description or resource.name}\n"
                                f"URI: {resource.uri}"
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {},
                                "required": [],
                            },
                        },
                    })
                    self.tool_handlers[wrapped_name] = self._make_resource_handler(
                        session,
                        uri=resource.uri,
                        timeout=cfg.tool_timeout,
                    )
                    registered_count += 1
                    capability_summary["resources"].append(wrapped_name)
            except Exception as exc:
                print(f"[mcp] Server '{server_name}': resources unavailable: {exc}")

            try:
                prompts_result = await session.list_prompts()
                for prompt in prompts_result.prompts:
                    properties: dict[str, Any] = {}
                    required: list[str] = []
                    for arg in prompt.arguments or []:
                        prop: dict[str, Any] = {"type": "string"}
                        if getattr(arg, "description", None):
                            prop["description"] = arg.description
                        properties[arg.name] = prop
                        if getattr(arg, "required", False):
                            required.append(arg.name)

                    wrapped_name = f"mcp_{server_name}_prompt_{prompt.name}"
                    self.tool_schemas.append({
                        "type": "function",
                        "function": {
                            "name": wrapped_name,
                            "description": (
                                f"[MCP Prompt] {prompt.description or prompt.name}\n"
                                "Returns a filled prompt template that can be used as a workflow guide."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": properties,
                                "required": required,
                            },
                        },
                    })
                    self.tool_handlers[wrapped_name] = self._make_prompt_handler(
                        session,
                        prompt_name=prompt.name,
                        timeout=cfg.tool_timeout,
                    )
                    registered_count += 1
                    capability_summary["prompts"].append(wrapped_name)
            except Exception as exc:
                print(f"[mcp] Server '{server_name}': prompts unavailable: {exc}")

            print(f"[mcp] Connected '{server_name}': {registered_count} capabilities")
            return server_stack, capability_summary
        except Exception as exc:
            hint = self._build_error_hint(exc)
            print(f"[mcp] Failed to connect '{server_name}': {exc}{hint}")
            try:
                await server_stack.aclose()
            except Exception:
                pass
            return None

    @staticmethod
    def _build_error_hint(exc: Exception) -> str:
        text = str(exc).lower()
        if any(
            marker in text
            for marker in ("parse error", "invalid json", "unexpected token", "jsonrpc", "content-length")
        ):
            return (
                " Hint: this looks like stdio protocol pollution. Make sure the MCP server writes "
                "only JSON-RPC to stdout and sends logs/debug output to stderr instead."
            )
        return ""

    @staticmethod
    def _stringify_content_block(content: Any) -> str:
        text = getattr(content, "text", None)
        if text is not None:
            return str(text)
        return str(content)

    def _make_tool_handler(self, session: Any, tool_name: str, timeout: int):
        async def handler(**kwargs: Any) -> str:
            try:
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments=kwargs),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return f"Error: MCP tool '{tool_name}' timed out after {timeout}s"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                return "Error: MCP tool call was cancelled"
            except Exception as exc:
                return f"Error: MCP tool '{tool_name}' failed - {type(exc).__name__}: {exc}"

            parts = [self._stringify_content_block(block) for block in getattr(result, "content", [])]
            return "\n".join(parts) if parts else "(no output)"

        return handler

    def _make_resource_handler(self, session: Any, uri: str, timeout: int):
        async def handler(**_: Any) -> str:
            try:
                result = await asyncio.wait_for(
                    session.read_resource(uri),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return f"Error: MCP resource '{uri}' timed out after {timeout}s"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                return "Error: MCP resource read was cancelled"
            except Exception as exc:
                return f"Error: MCP resource '{uri}' failed - {type(exc).__name__}: {exc}"

            parts: list[str] = []
            for block in getattr(result, "contents", []):
                text = getattr(block, "text", None)
                if text is not None:
                    parts.append(str(text))
                    continue
                blob = getattr(block, "blob", None)
                if blob is not None:
                    parts.append(f"[Binary resource: {len(blob)} bytes]")
                    continue
                parts.append(str(block))
            return "\n".join(parts) if parts else "(no output)"

        return handler

    def _make_prompt_handler(self, session: Any, prompt_name: str, timeout: int):
        async def handler(**kwargs: Any) -> str:
            try:
                result = await asyncio.wait_for(
                    session.get_prompt(prompt_name, arguments=kwargs),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return f"Error: MCP prompt '{prompt_name}' timed out after {timeout}s"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                return "Error: MCP prompt call was cancelled"
            except Exception as exc:
                return f"Error: MCP prompt '{prompt_name}' failed - {type(exc).__name__}: {exc}"

            parts: list[str] = []
            for message in getattr(result, "messages", []):
                content = getattr(message, "content", None)
                if isinstance(content, list):
                    parts.extend(self._stringify_content_block(block) for block in content)
                elif content is not None:
                    parts.append(self._stringify_content_block(content))
            return "\n".join(parts) if parts else "(no output)"

        return handler

    async def close(self) -> None:
        """Shut down all MCP connections."""
        for stack in self._server_stacks.values():
            try:
                await stack.aclose()
            except Exception:
                pass
        self._server_stacks.clear()
        self.connected_servers = []
        self.server_capabilities = {}

    def startup_summary_lines(self) -> list[str]:
        """Return concise startup summary lines for connected servers."""
        if not self.connected_servers:
            return ["[mcp] no MCP servers connected"]

        lines: list[str] = []
        for server_name in self.connected_servers:
            caps = self.server_capabilities.get(
                server_name,
                {"tools": [], "resources": [], "prompts": []},
            )
            tool_count = len(caps.get("tools", []))
            resource_count = len(caps.get("resources", []))
            prompt_count = len(caps.get("prompts", []))
            total = tool_count + resource_count + prompt_count
            lines.append(
                f"[mcp] {server_name}: {total} capabilities "
                f"({tool_count} tools, {resource_count} resources, {prompt_count} prompts)"
            )
        return lines

    def detailed_summary_lines(self) -> list[str]:
        """Return detailed summary lines including capability names."""
        lines = self.startup_summary_lines()
        if not self.connected_servers:
            return lines

        detailed: list[str] = list(lines)
        for server_name in self.connected_servers:
            caps = self.server_capabilities.get(
                server_name,
                {"tools": [], "resources": [], "prompts": []},
            )
            for key, label in (("tools", "tools"), ("resources", "resources"), ("prompts", "prompts")):
                names = caps.get(key, [])
                if names:
                    pretty = ", ".join(self._display_name(name) for name in names)
                    detailed.append(f"      {label}: {pretty}")
        return detailed

    @staticmethod
    def _display_name(wrapped_name: str) -> str:
        if not wrapped_name.startswith("mcp_"):
            return wrapped_name
        rest = wrapped_name[4:]
        if "_resource_" in rest:
            server, name = rest.split("_resource_", 1)
            return f"{server}::resource::{name}"
        if "_prompt_" in rest:
            server, name = rest.split("_prompt_", 1)
            return f"{server}::prompt::{name}"
        server, _, name = rest.partition("_")
        if not name:
            return rest
        return f"{server}::{name}"
