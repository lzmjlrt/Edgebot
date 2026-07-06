"""
edgebot/cli/tool_hints.py - compatibility facade.

The implementation moved to edgebot/agent/tool_hints.py so the agent core
no longer depends on the cli package.
"""

from edgebot.agent.tool_hints import _format_mcp_hint, _trunc, format_tool_hint  # noqa: F401
