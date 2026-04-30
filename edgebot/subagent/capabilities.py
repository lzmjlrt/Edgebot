"""
edgebot/subagent/capabilities.py - Capability matrix for isolated subagents.

Privileges are explicit per capability. The runner only exposes the listed tool
names to the subagent and rejects everything else.
"""

CAPABILITIES = {
    "explore": {
        "system": (
            "You are a read-only exploration subagent. Investigate the task and report findings "
            "concisely. Never write files; never modify state. When done, reply with a final "
            "summary (no tool call)."
        ),
        "allowed_tools": ["read_file", "list_dir", "bash"],
    },
    "builder": {
        "system": (
            "You are a builder subagent. Implement the requested change, then return a short "
            "summary of exactly what you changed. Keep the diff minimal and focused."
        ),
        "allowed_tools": ["read_file", "list_dir", "bash", "write_file", "edit_file"],
    },
    "reviewer": {
        "system": (
            "You are a code-review subagent. Read only. Output a structured review with these "
            "sections: **Issues**, **Suggestions**, **Verdict** (approve|reject)."
        ),
        "allowed_tools": ["read_file", "list_dir", "bash"],
    },
}
