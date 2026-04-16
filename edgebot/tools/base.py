"""
edgebot/tools/base.py - Path safety utility and Tool base class.
"""

import abc
from pathlib import Path
from typing import Any

from edgebot.config import WORKDIR


def safe_path(p: str) -> Path:
    """Resolve a path and ensure it stays inside WORKDIR."""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


class BaseTool(abc.ABC):
    """Base class for all tools."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Name of the tool."""
        pass

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """Description of the tool."""
        pass

    @property
    @abc.abstractmethod
    def parameters(self) -> dict:
        """JSON Schema for tool parameters."""
        pass

    @abc.abstractmethod
    def execute(self, **kwargs: Any) -> Any:
        """Run the tool."""
        pass

    def to_openai(self) -> dict:
        """Build an OpenAI function-calling tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
