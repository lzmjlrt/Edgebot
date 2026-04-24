"""
edgebot/config.py - Global configuration and constants.

All environment variables, paths, thresholds are defined here.
LLM calls go through the provider abstraction layer
(edgebot.providers), which wraps litellm underneath.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required Edgebot runtime configuration is missing."""


WORKDIR = Path.cwd()
WORKDIR_ENV = WORKDIR / ".env"
if WORKDIR_ENV.exists():
    load_dotenv(dotenv_path=WORKDIR_ENV, override=True)

# --- LLM settings ---
# litellm uses provider-prefixed model names, e.g.:
#   anthropic/claude-3-5-sonnet  openai/gpt-4o  ollama/llama3  deepseek/deepseek-chat
try:
    MODEL = os.environ["MODEL_ID"]
    API_KEY = os.environ["API_KEY"]
except KeyError as exc:
    missing = exc.args[0]
    env_hint = str(WORKDIR_ENV)
    raise ConfigError(
        f"Missing required config: {missing}. "
        f"Set it in the environment or create {env_hint} with MODEL_ID and API_KEY."
    ) from exc
API_BASE = os.getenv("API_BASE")  # optional, for proxies or custom endpoints

# --- Workspace paths ---
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
RUNTIME_DIR = WORKDIR / ".edgebot"
SKILLS_DIR = RUNTIME_DIR / "skills"
MEMORY_DIR = RUNTIME_DIR / "memory"
CRON_DIR = RUNTIME_DIR / "cron"
SESSION_DIR = RUNTIME_DIR / "sessions"
BACKGROUND_DIR = RUNTIME_DIR / "background"
SUBAGENT_DIR = RUNTIME_DIR / "subagents"
PERMISSIONS_FILE = RUNTIME_DIR / "permissions.json"
LEGACY_SESSION_DIR = Path.home() / ".edgebot" / "sessions"
CRON_STORE_PATH = CRON_DIR / "jobs.json"
MCP_CONFIG_PATH = RUNTIME_DIR / "mcp_servers.json"
TASK_HOOKS_PATH = RUNTIME_DIR / "task_hooks.json"
AGENTS_MD_PATH = RUNTIME_DIR / "AGENTS.md"
SOUL_MD_PATH = RUNTIME_DIR / "SOUL.md"
USER_MD_PATH = RUNTIME_DIR / "USER.md"
TOOLS_MD_PATH = RUNTIME_DIR / "TOOLS.md"
HEARTBEAT_MD_PATH = RUNTIME_DIR / "HEARTBEAT.md"
LEGACY_SKILLS_DIR = WORKDIR / "skills"

# --- Tuning constants ---
TOKEN_THRESHOLD = 100_000
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "1800"))
MEMORY_CONSOLIDATION_INTERVAL_SECONDS = int(os.getenv("MEMORY_CONSOLIDATION_INTERVAL_SECONDS", "21600"))
IDLE_COMPACT_MINUTES = int(os.getenv("IDLE_COMPACT_MINUTES", "0"))

# --- Messaging protocol ---
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# --- Provider singleton ---
_PROVIDER = None


def create_provider():
    """Create and return the shared LLMProvider instance (lazy singleton)."""
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    from edgebot.providers.litellm_provider import LiteLLMProvider
    _PROVIDER = LiteLLMProvider(
        api_key=API_KEY,
        model=MODEL,
        api_base=API_BASE,
    )
    return _PROVIDER
