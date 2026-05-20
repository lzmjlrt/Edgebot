# EdgeBot
<div align="center">
  <img width="648" height="187" alt="image" src="https://github.com/user-attachments/assets/811c787b-ccf4-45df-8645-0526fd3b9be2" />
  <br/>
  <img width="578" height="161" alt="image" src="https://github.com/user-attachments/assets/9e517ed5-03ec-431d-a241-6548e8e7e2d2" />
  <br/>
  <br/>
 


  <p>
    <b>A minimal, modular, and extensible autonomous coding agent framework built with Python.</b>
  </p>
</div>

🤖 **Edgebot** is an **ultra-lightweight** workspace agent referenced and inspired by [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) and [Nanobot](https://github.com/obot-platform/nanobot).

⚡️ Delivers isolated subagent delegation, native MCP support, layered permission control, and robust tool-calling—all right from your terminal without heavy boilerplate frameworks.

> 🐈 Edgebot supports **any LLM provider** out of the box through [LiteLLM](https://github.com/BerriAI/litellm) (OpenAI, Anthropic, DeepSeek, Ollama, etc.). Simply swap an environment variable and you're good to go!


## ✨ Key Features

🧰 **20+ Built-in Tools**: Execute shells, manipulate file systems (read/write/edit), manage persistent task boards, and run silent background jobs. <br>
🧠 **Unified Agent Loop**: Both the main agent and isolated subagents share one `AgentRunner` execution path; subagent results are injected back into the parent turn via a notification queue. <br>
⚡ **Subagent Spawning**: Delegate isolated code exploration, building, or review to capability-scoped one-shot background subagents. <br>
🛡️ **Layered Permission Control**: Workspace-boundary auto-allow + bash program allowlist + regex deny patterns; chain-aware (`cd X && cmd` is inspected per-segment). Hard-blocks `rm -rf /`, fork bombs, and similar before the shell ever sees them. <br>
🗜️ **Context Compression**: Microcompacting + LLM summary compression + idle-session archival to always stay within token limits. <br>
🌙 **Dream Memory Consolidation**: Two-phase analyze-then-edit pass that extracts durable facts across conversations into persistent memory. <br>
🧩 **Skill System**: Extend Edgebot with `SKILL.md` files dynamically, teaching it unique domain knowledge without ever touching Python code. <br>
🔌 **Native MCP Support**: Direct integration with the Model Context Protocol (MCP) servers via `mcp_servers.json`. <br>
🚀 **Background Execution**: Spawn heavy terminal commands in non-blocking background threads and get notified upon completion. <br>
🎯 **Modular Tool Architecture**: Add new capabilities in minutes by simply inheriting from `BaseTool`.

## 🏗️ Architecture

```text
edgebot/
├── config.py                # Environment, paths, global constants
├── agent/
│   ├── loop.py              # Main turn orchestration & notification draining
│   ├── runner.py            # Shared LLM-call / tool-execution loop (AgentRunner)
│   ├── context.py           # Auto-seeds templates & bootstrap config
│   ├── compression.py       # Token estimation, microcompact, LLM summary
│   └── memory.py            # Dream-style memory consolidation
├── subagent/
│   ├── runner.py            # Isolated subagent task manager (delegates to AgentRunner)
│   └── capabilities.py      # explore / builder / reviewer tool whitelists
├── permissions/
│   ├── manager.py           # Authorize gate, chain-aware bash parsing, rules I/O
│   └── defaults.py          # Seed allowlist + deny patterns
├── tools/
│   ├── base.py              # BaseTool API
│   ├── orchestration.py     # Batched parallel/serial dispatch + permission hook
│   ├── builtin/             # Modularized core tools (bash, filesystem, etc.)
│   └── registry.py          # Global tool registry & singleton wiring
├── mcp/
│   └── client.py            # External MCP server communication
├── tasks/
│   ├── todo.py              # In-memory checklist tracker (TodoWrite)
│   └── manager.py           # On-disk persistent task board
├── background/
│   └── manager.py           # Thread-safe background task runner
├── skills/
│   └── loader.py            # Automatic SKILL.md discovery & extraction
└── cli/
    └── repl.py              # Interactive REPL, prompt UI, approval handler
```

## 📦 Install & Quick Start

> [!IMPORTANT]
> Edgebot relies on LiteLLM to unify API calls. You will need your provider's API key.

**1. Install dependencies**

```bash
pip install litellm python-dotenv prompt_toolkit rich
```

**2. Configure environment**

Create a `.env` file in your root tracking directory:

```env
MODEL_ID=deepseek/deepseek-chat
API_KEY=your-api-key-here
API_BASE=https://api.deepseek.com   # optional: for proxy routers or custom endpoints
```

<details>
<summary><b>Supported Model IDs (Expand)</b></summary>

| Provider | MODEL_ID Example |
|----------|----------|
| **DeepSeek** | `deepseek/deepseek-chat` |
| **OpenAI** | `openai/gpt-4o` |
| **Anthropic** | `anthropic/claude-sonnet-4-20250514` |
| **Ollama (local)** | `ollama/llama3` |

*See [LiteLLM Supported Providers](https://docs.litellm.ai/docs/providers) for the full list.*
</details>

**3. Run**

```bash
edgebot
```

If you are running from source, install the CLI once:

```bash
pip install -e .
```

`python -m edgebot` also continues to work.

> [!TIP]
> **Magic Bootstrap**: On its very first run, Edgebot will automatically generate `AGENTS.md`, `SOUL.md`, `USER.md`, `TOOLS.md`, a sample `skills/` directory, and an `mcp_servers.json` template!

## 💻 CLI Reference

Edgebot offers several powerful control commands right from the prompt:

| Command | Description |
|---------|-------------|
| `/new` | Start a fresh blank conversation session |
| `/sessions` | List all saved history sessions |
| `/resume <#\|key>` | Swiftly resume a previous disconnected session |
| `/compact` | Manually compress context & update memory |
| `/memory` | Run persistent memory consolidation now |
| `/tasks` | Show the active task board |
| `/bg` / `/bg <id>` / `/bg output <id>` | Inspect background tasks |
| `/subagents` / `/subagents <id>` | List or inspect isolated subagents |
| `/subagents output\|transcript\|fg\|bg\|stop <id>` | Control a subagent |
| `/permissions` | Show persisted + session permission rules |
| `/cron`, `/heartbeat`, `/mcp` | Inspect scheduler, heartbeat, and MCP state |
| `/status` | Show current session, model, and token info |
| `/help` | Print out the CLI manual overlay |
| `/exit` | Gracefully quit the REPL |

## 🛠️ Tooling & Extensibility

Edgebot ships with **20+ tools** categorized logically:

<table align="center">
  <tr align="center">
    <th>Category</th>
    <th>Tools Available</th>
  </tr>
  <tr>
    <td><b>🐚 Shell</b></td>
    <td><code>bash</code>, <code>background_run</code>, <code>check_background</code>, <code>task_output</code></td>
  </tr>
  <tr>
    <td><b>📂 Filesystem</b></td>
    <td><code>read_file</code>, <code>write_file</code>, <code>edit_file</code>, <code>list_dir</code></td>
  </tr>
  <tr>
    <td><b>📋 Tasks</b></td>
    <td><code>task_create</code>, <code>task_get</code>, <code>task_update</code>, <code>task_list</code>, <code>claim_task</code>, <code>TodoWrite</code></td>
  </tr>
  <tr>
    <td><b>🤖 Subagents</b></td>
    <td><code>task</code> (spawn), <code>check_subagent</code>, <code>list_subagents</code>, <code>control_subagent</code>, <code>wait_subagent</code></td>
  </tr>
  <tr>
    <td><b>🧩 Other</b></td>
    <td><code>load_skill</code>, <code>compress</code>, <code>cron</code>, <code>ask_user</code></td>
  </tr>
</table>

### 🛡️ Permission Model

Sensitive tools (`bash`, `write_file`, `edit_file`, `background_run`) go through `PermissionManager` before execution. The default policy ships with `.edgebot/permissions.json` auto-seeded on first run:

- **`bash_programs`**: program-name allowlist (`git`, `python`, `rg`, `find`, …). Granting `git` covers `git status`, `git log`, `git diff`, etc.—no need to re-approve every subcommand.
- **`bash_deny_patterns`**: regex blacklist (`rm -rf /`, fork bombs, `dd if=`, …). Matches are hard-denied with a no-retry hint sent back to the model.
- **`workspace_write_auto_allow`**: writes/edits whose path resolves under the current workspace are auto-allowed; writes outside still prompt.
- **Chain-aware bash parsing**: `cd X && cmd` / `cmd1 | cmd2` / Windows `for %f in (...) do cmd` are split into segments; every segment's program must be allowed (so `cd X && rm -rf .` is still blocked even though `cd` is transparent).

When a prompt is needed, the REPL offers four choices:

| Key | Effect |
|-----|--------|
| `y` | Allow this one call |
| `s` | Allow for this session (adds to in-memory rules) |
| `a` | Allow and persist to `.edgebot/permissions.json` |
| `n` | Deny |

Inspect the current ruleset anytime with `/permissions`.

### 🔌 Model Context Protocol (MCP) Support

Edgebot natively bridges with external MCP servers. A default configuration template `mcp_servers.json` drops directly into your workspace. Use `mcpServers` as the canonical key (legacy `servers` is also accepted for compatibility).

```json
{
  "mcpServers": {
    "everything": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-everything"],
      "env": {},
      "toolTimeout": 30,
      "enabledTools": ["*"]
    }
  }
}
```

### 🧠 Inject Custom Skills

Simply place Markdown files (e.g., `SKILL.md`) inside any subfolder under the auto-generated `skills/` directory. Edgebot absorbs this knowledge immediately without a single line of python code changes!

```markdown
---
name: summarize-skill
description: Teaches Edgebot standard summarization reporting patterns
---

# Summarization Instructions...
```

### ⚙️ Build Your Own Python Tools

Edgebot maps simple Python subclasses into complex schemas instantly. 

```python
# edgebot/tools/builtin/my_tool.py
from edgebot.tools.base import BaseTool

class SuperTool(BaseTool):
    name = "super_tool"
    description = "A custom tool doing awesome stuff."
    parameters = {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        return "Awesome stuff done!"
```
Then register it in `edgebot/tools/registry.py`: `register_tool(SuperTool())`

## 📝 License

[MIT License](./LICENSE)

