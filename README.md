# Edgebot
<img width="648" height="187" alt="image" src="https://github.com/user-attachments/assets/811c787b-ccf4-45df-8645-0526fd3b9be2" />

A minimal, modular coding agent framework built with Python. Reference [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code).Supports any LLM provider through [LiteLLM](https://github.com/BerriAI/litellm) (OpenAI, Anthropic, DeepSeek, Ollama, etc.).

## Features

- **22 built-in tools** - shell execution, file read/write/edit, task management, background jobs, and more
- **Multi-provider LLM** - switch between any LLM by changing one env var (`deepseek/deepseek-chat`, `anthropic/claude-sonnet-4-20250514`, `openai/gpt-4o`, `ollama/llama3`, ...)
- **Subagent spawning** - delegate isolated exploration or work to one-shot subagents
- **Multi-agent team** - spawn persistent autonomous teammates that collaborate via file-based message bus
- **Task board** - file-backed persistent task management with dependencies and ownership
- **Skill system** - extend the agent with `SKILL.md` files for domain-specific knowledge
- **MCP Support** - native support for Model Context Protocol (MCP) servers via `mcp_servers.json`
- **Modular Tool Architecture** - easily extend capabilities by inheriting from `BaseTool`
- **Context compression** - automatic microcompact + full conversation summarization to stay within token limits
- **Background execution** - run long commands in background threads, get notified on completion

## Project Structure

```
edgebot/
├── config.py                # Environment, paths, constants
├── agent/
│   ├── loop.py              # Main agent loop
│   ├── subagent.py          # One-shot subagent spawning
│   ├── context.py           # Auto-seeds templates & bootstrap config
│   └── compression.py       # Token estimation & context compaction
├── tools/
│   ├── base.py              # BaseTool API & sandbox safety
│   ├── builtin/             # Modularized built-in tools (BashTool, FileTool, etc.)
│   └── registry.py          # Dynamic tool schema & handler registration
├── mcp/
│   └── client.py            # External MCP server communication
├── tasks/
│   ├── todo.py              # In-memory checklist (TodoWrite)
│   └── manager.py           # File-backed persistent task board
├── team/
│   ├── bus.py               # File-based inter-agent message bus
│   ├── teammate.py          # Autonomous teammate lifecycle
│   └── protocols.py         # Shutdown handshake & plan approval
├── background/
│   └── manager.py           # Background thread task runner
├── skills/
│   └── loader.py            # SKILL.md discovery & loading
└── cli/
    └── repl.py              # Interactive REPL
```

## Quick Start

### 1. Install dependencies

```bash
pip install litellm python-dotenv
```

### 2. Configure environment

Create a `.env` file in your working directory:

```env
MODEL_ID=deepseek/deepseek-chat
API_KEY=your-api-key-here
API_BASE=https://api.deepseek.com   # optional, for proxies or custom endpoints
```

Some common `MODEL_ID` values:

| Provider | MODEL_ID |
|----------|----------|
| DeepSeek | `deepseek/deepseek-chat` |
| OpenAI | `openai/gpt-4o` |
| Anthropic | `anthropic/claude-sonnet-4-20250514` |
| Ollama (local) | `ollama/llama3` |

See [LiteLLM supported providers](https://docs.litellm.ai/docs/providers) for the full list.

### 3. Run

```bash
python -m edgebot
```

Upon first run, Edgebot will auto-generate base configurations in your workspace if they do not exist:
- `AGENTS.md` / `SOUL.md` / `USER.md` / `TOOLS.md` - Core prompts and identity configuration.
- `skills/` - A sample directory with an instructional `summarize` skill.
- `mcp_servers.json` - A template config for routing capabilities to external MCP servers.

## REPL Commands

| Command | Description |
|---------|-------------|
| `/compact` | Manually compress conversation context |
| `/tasks` | Show all tasks on the board |
| `/team` | List all teammate agents |
| `/inbox` | Read the lead agent's inbox |
| `q` / `exit` | Quit |

## Tools

The agent has access to 22 tools:

| Category | Tools |
|----------|-------|
| **Shell** | `bash`, `background_run`, `check_background` |
| **Filesystem** | `read_file`, `write_file`, `edit_file` |
| **Tasks** | `task_create`, `task_get`, `task_update`, `task_list`, `claim_task`, `TodoWrite` |
| **Agent** | `task` (subagent), `load_skill`, `compress` |
| **Team** | `spawn_teammate`, `list_teammates`, `send_message`, `read_inbox`, `broadcast`, `shutdown_request`, `plan_approval`, `idle` |

## Adding Built-in Tools

Edgebot leverages an easy-to-extend `BaseTool` class in `edgebot/tools/base.py`. To plug in a new capability:

1. Create a class inheriting from `BaseTool` with `name`, `description`, `parameters`, and `execute`.
2. Register it in `edgebot/tools/registry.py` (e.g. `register_tool(MyCustomTool())`).

## Adding Skills

Place `SKILL.md` files under a `skills/` directory in your workspace to teach the agent specific domain knowledge without changing code:

```
skills/
└── my-skill/
    └── SKILL.md
```

Format:

```markdown
---
name: my-skill
description: What this skill does
---

Skill content and instructions here...
```

## Adding MCP Servers

Edgebot natively scales with external MCP servers. A default config `mcp_servers.json` is generated in your workspace holding your command routes:

```json
{
  "mcpServers": {
    "everything": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-everything"],
      "env": {}
    }
  }
}
```

## License

MIT
