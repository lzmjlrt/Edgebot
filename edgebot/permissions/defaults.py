"""
edgebot/permissions/defaults.py - Seed allowlist + deny patterns.

Used by PermissionManager when the persisted rules file does not yet exist.
"""

from __future__ import annotations

DEFAULT_BASH_PROGRAMS: list[str] = [
    "ls", "cat", "pwd", "echo", "rg", "grep", "find",
    "head", "tail", "wc", "which", "where", "type", "file",
    "git", "diff",
    "python", "python3", "pip", "pip3",
    "node", "npm", "npx", "yarn", "pnpm",
    "pytest", "ruff", "black", "mypy",
    "go", "cargo", "rustc",
    "make",
    "true", "false",
    # Shell chain helpers — safe because chained commands are validated
    # individually against this allowlist (see _bash_programs_in_chain).
    "for",
]

DEFAULT_BASH_DENY_PATTERNS: list[str] = [
    r"\brm\s+-rf?\s+/",
    r"\brm\s+-rf?\s+~",
    r"\bdd\s+if=",
    r"\bmkfs\b",
    r":\(\)\s*\{.*\};:",
    r">\s*/dev/sd",
    r"\bchmod\s+-R\s+777\s+/",
]
