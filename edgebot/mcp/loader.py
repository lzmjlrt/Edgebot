"""
edgebot/mcp/loader.py - Load MCP servers from .edgebot/mcp.json.

Returns an initialized MCPClient (already started) or None if no config.
"""

import json
from pathlib import Path

from edgebot.mcp.client import MCPClient


async def load_mcp(config_path: Path) -> MCPClient | None:
    """
    Read *config_path*, connect to all configured MCP servers, and return
    a started MCPClient.  Returns None if the config file doesn't exist.

    Config format (.edgebot/mcp.json):
    {
      "servers": {
        "filesystem": {
          "type": "stdio",
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
        },
        "brave": {
          "type": "stdio",
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-brave-search"],
          "env": {"BRAVE_API_KEY": "sk-xxx"}
        }
      }
    }
    """
    if not config_path.exists():
        return None

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[mcp] Invalid JSON in {config_path}: {e}")
        return None

    servers = data.get("servers", {})
    if not servers:
        return None

    client = MCPClient(servers)
    await client.start()
    return client
