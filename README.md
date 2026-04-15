# Edgebot

A minimal, modular coding agent framework built with Python. Reference [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code).Supports any LLM provider through [LiteLLM](https://github.com/BerriAI/litellm) (OpenAI, Anthropic, DeepSeek, Ollama, etc.).

## Features

- **22 built-in tools** - shell execution, file read/write/edit, task management, background jobs, and more
- **Multi-provider LLM** - switch between any LLM by changing one env var (`deepseek/deepseek-chat`, `anthropic/claude-sonnet-4-20250514`, `openai/gpt-4o`, `ollama/llama3`, ...)
- **Subagent spawning** - delegate isolated exploration or work to one-shot subagents
- **Multi-agent team** - spawn persistent autonomous teammates that collaborate via file-based message bus
- **Task board** - file-backed persistent task management with dependencies and ownership
- **Skill system** - extend the agent with `SKILL.md` files for domain-specific knowledge
- **Context compression** - automatic microcompact + full conversation summarization to stay within token limits
- **Background execution** - run long commands in background threads, get notified on completion

## Project Structure

```
edgebot/
├── config.py                # Environment, paths, constants
├── agent/
│   ├── loop.py              # Main agent loop
│   ├── subagent.py          # One-shot subagent spawning
│   └── compression.py       # Token estimation & context compaction
├── tools/
│   ├── base.py              # Path safety (sandbox)
│   ├── shell.py             # Shell command execution
│   ├── filesystem.py        # File read / write / edit
│   └── registry.py          # Tool schemas + handler dispatch + global instances
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

## Skills

Place `SKILL.md` files under a `skills/` directory in your workspace to extend the agent with domain knowledge:

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

## License

MIT
