"""
edgebot/mcp/loader.py - Load MCP servers from mcp_servers.json.

Returns an initialized MCPClient (already started) or None if no config.
"""

from __future__ import annotations

import json
from pathlib import Path

from edgebot.mcp.client import MCPClient
from edgebot.mcp.config import MCPServerConfig


async def load_mcp(config_path: Path) -> MCPClient | None:
    """
    Read *config_path*, connect to all configured MCP servers, and return
    a started MCPClient. Returns None if the config file doesn't exist or
    contains no valid server definitions.

    Accepted config formats:

    {
      "mcpServers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
        }
      }
    }

    {
      "servers": {
        "filesystem": {
          "type": "stdio",
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
        }
      }
    }
    """
    if not config_path.exists():
        return None

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[mcp] Invalid JSON in {config_path}: {exc}")
        return None

    if not isinstance(data, dict):
        print(f"[mcp] Invalid config root in {config_path}: expected JSON object")
        return None

    raw_servers = data.get("mcpServers")
    if raw_servers is None:
        raw_servers = data.get("servers", {})

    if not isinstance(raw_servers, dict) or not raw_servers:
        return None

    servers: dict[str, MCPServerConfig] = {}
    for server_name, raw_cfg in raw_servers.items():
        if not isinstance(raw_cfg, dict):
            print(f"[mcp] Skipping '{server_name}': expected object config")
            continue
        servers[str(server_name)] = MCPServerConfig.from_dict(raw_cfg)

    if not servers:
        return None

    client = MCPClient(servers)
    await client.start()
    return client
