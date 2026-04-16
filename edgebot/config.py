"""
edgebot/config.py - Global configuration and constants.

All environment variables, paths, thresholds are defined here.
LLM calls go through litellm (stateless, no client instance needed).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

# --- LLM settings ---
# litellm uses provider-prefixed model names, e.g.:
#   anthropic/claude-3-5-sonnet  openai/gpt-4o  ollama/llama3  deepseek/deepseek-chat
MODEL = os.environ["MODEL_ID"]
API_KEY = os.environ["API_KEY"]
API_BASE = os.getenv("API_BASE")  # optional, for proxies or custom endpoints

# --- Workspace paths ---
WORKDIR = Path.cwd()
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
MEMORY_DIR = WORKDIR / "memory"
MCP_CONFIG_PATH = WORKDIR / ".edgebot" / "mcp.json"
SESSION_DIR = Path.home() / ".edgebot" / "sessions"

# --- Tuning constants ---
TOKEN_THRESHOLD = 100_000
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

# --- Messaging protocol ---
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}
