"""Built-in web fetch/search tools."""

from __future__ import annotations

from typing import Any

from edgebot.tools.base import BaseTool
from edgebot.tools.web import run_web_fetch, run_web_search


class WebFetchTool(BaseTool):
    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return "Fetch a public URL and return visible text. Requires user approval for network access."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
            },
            "required": ["url"],
        }

    def is_read_only(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def execute(self, **kwargs: Any) -> Any:
        return run_web_fetch(kwargs["url"])


class WebSearchTool(BaseTool):
    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "Search the web and return result snippets. Requires user approval for network access."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        }

    def is_read_only(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def execute(self, **kwargs: Any) -> Any:
        return run_web_search(kwargs["query"], kwargs.get("max_results", 5))
