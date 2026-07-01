"""Permission approval and persistence helpers."""

__all__ = ["PermissionManager"]


def __getattr__(name: str):
    if name == "PermissionManager":
        from .manager import PermissionManager

        return PermissionManager
    raise AttributeError(name)
