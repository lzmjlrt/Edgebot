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

    _TYPE_CASTERS = {
        "string": str,
        "integer": int,
        "boolean": bool,
        "number": float,
    }

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

    @property
    def concurrency_safe(self) -> bool:
        return False

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Best-effort JSON-schema-based parameter coercion."""
        if not isinstance(params, dict):
            return params
        schema = self.parameters or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        casted = dict(params)
        for key, value in list(casted.items()):
            prop = properties.get(key)
            if not isinstance(prop, dict):
                continue
            target_type = prop.get("type")
            caster = self._TYPE_CASTERS.get(target_type)
            if caster is None or value is None:
                continue
            if target_type == "boolean":
                if isinstance(value, str):
                    lowered = value.strip().lower()
                    if lowered in {"true", "1", "yes", "on"}:
                        casted[key] = True
                    elif lowered in {"false", "0", "no", "off"}:
                        casted[key] = False
                continue
            if target_type == "integer" and isinstance(value, bool):
                continue
            if not isinstance(value, caster):
                try:
                    casted[key] = caster(value)
                except (TypeError, ValueError):
                    continue
        return casted

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Minimal JSON-schema validation for tool-call arguments."""
        if not isinstance(params, dict):
            return [f"parameters must be an object, got {type(params).__name__}"]
        schema = self.parameters or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        errors: list[str] = []

        for key in required:
            if key not in params:
                errors.append(f"missing required parameter '{key}'")

        for key, value in params.items():
            prop = properties.get(key)
            if not isinstance(prop, dict):
                continue
            expected = prop.get("type")
            if expected == "string" and not isinstance(value, str):
                errors.append(f"'{key}' must be a string")
                continue
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                errors.append(f"'{key}' must be an integer")
                continue
            if expected == "boolean" and not isinstance(value, bool):
                errors.append(f"'{key}' must be a boolean")
                continue
            if expected == "number" and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
            ):
                errors.append(f"'{key}' must be a number")
                continue
            if expected == "array" and not isinstance(value, list):
                errors.append(f"'{key}' must be an array")
                continue

            enum = prop.get("enum")
            if isinstance(enum, list) and value not in enum:
                errors.append(f"'{key}' must be one of {enum}")
            minimum = prop.get("minimum")
            if isinstance(minimum, (int, float)) and isinstance(value, (int, float)) and value < minimum:
                errors.append(f"'{key}' must be >= {minimum}")
            maximum = prop.get("maximum")
            if isinstance(maximum, (int, float)) and isinstance(value, (int, float)) and value > maximum:
                errors.append(f"'{key}' must be <= {maximum}")
        return errors

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
