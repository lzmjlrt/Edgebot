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

🤖 **Edgebot** is an **ultra-lightweight** workspace agent referenced and inspired by [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code). 

⚡️ Delivers multi-agent collaboration, subagent delegation, native MCP support, and robust tool-calling—all right from your terminal without heavy boilerplate frameworks.

> 🐈 Edgebot supports **any LLM provider** out of the box through [LiteLLM](https://github.com/BerriAI/litellm) (OpenAI, Anthropic, DeepSeek, Ollama, etc.). Simply swap an environment variable and you're good to go!

  
## ✨ Key Features

🧰 **22 Built-in Tools**: Execute shells, manipulate file systems (read/write/edit), manage persistent task boards, and run silent background jobs. <br>
🤝 **Multi-Agent Team**: Spawn persistent autonomous teammates that communicate and collaborate seamlessly via a file-based message bus. <br>
⚡ **Subagent Spawning**: Delegate isolated code exploration or complex research to one-shot background subagents. <br>
🗜️ **Context Compression**: Uses advanced microcompacting + idle-time intelligent conversation summarization to always stay within token limits cleanly. <br>
🧩 **Skill System**: Extend Edgebot with `SKILL.md` files dynamically, teaching it unique domain knowledge without ever touching Python code. <br>
🔌 **Native MCP Support**: Direct integration with the Model Context Protocol (MCP) servers via `mcp_servers.json`. <br>
🚀 **Background Execution**: Spawn heavy terminal commands in non-blocking background threads and get notified upon completion. <br>
🎯 **Modular Tool Architecture**: Add new capabilities in minutes by simply inheriting from `BaseTool`. 

## 🏗️ Architecture

```text
edgebot/
├── config.py                # Environment, paths, global constants
├── agent/
│   ├── loop.py              # Main agent loop & runtime routing
│   ├── subagent.py          # One-shot subagent execution engine
│   ├── context.py           # Auto-seeds templates & bootstrap config
│   └── compression.py       # Intelligent token estimation & idle compaction
├── tools/
│   ├── base.py              # BaseTool API & execution sandbox structure
│   ├── builtin/             # Modularized core tools (BashTool, FileTool, etc.)
│   └── registry.py          # Dynamic tool schema builder & handler registration
├── mcp/
│   └── client.py            # External MCP server communication protocols
├── tasks/
│   ├── todo.py              # Fast in-memory checklist tracker (TodoWrite)
│   └── manager.py           # On-disk persistent task board manager
├── team/
│   ├── bus.py               # File-based IPC (Inter-Process) message bus
│   ├── teammate.py          # Autonomous teammate lifecycle manager
│   └── protocols.py         # Handshakes, shutdown, and plan approval routines
├── background/
│   └── manager.py           # Thread-safe background task runner
├── skills/
│   └── loader.py            # Automatic SKILL.md discovery & extraction
└── cli/
    └── repl.py              # Interactive REPL, prompt UI, queue injection
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
python -m edgebot
```

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
| `/tasks` | Show the active multi-agent task board | 
| `/team` | List currently spawned teammate agents |
| `/inbox` | Read the lead agent's inter-process inbox |
| `/status` | Show current session, model, and token info |
| `/help` | Print out the CLI manual overlay |
| `exit` | Gracefully quit the REPL |

## 🛠️ Tooling & Extensibility

Edgebot ships with **22 powerful tools** categorized logically:

<table align="center">
  <tr align="center">
    <th>Category</th>
    <th>Tools Available</th>
  </tr>
  <tr>
    <td><b>🐚 Shell</b></td>
    <td><code>bash</code>, <code>background_run</code>, <code>check_background</code></td>
  </tr>
  <tr>
    <td><b>📂 Filesystem</b></td>
    <td><code>read_file</code>, <code>write_file</code>, <code>edit_file</code></td>
  </tr>
  <tr>
    <td><b>📋 Tasks</b></td>
    <td><code>task_create</code>, <code>task_get</code>, <code>task_update</code>, <code>task_list</code>, <code>claim_task</code>, <code>TodoWrite</code></td>
  </tr>
  <tr>
    <td><b>🤖 Agent Core</b></td>
    <td><code>task</code> (launch subagent), <code>load_skill</code>, <code>compress</code></td>
  </tr>
  <tr>
    <td><b>👥 Teamwork</b></td>
    <td><code>spawn_teammate</code>, <code>list_teammates</code>, <code>send_message</code>, <code>read_inbox</code>, <code>broadcast</code>, <code>shutdown_request</code>, <code>plan_approval</code>, <code>idle</code></td>
  </tr>
</table>

### 🔌 Model Context Protocol (MCP) Support

Edgebot natively bridges with external MCP servers. A default configuration template `mcp_servers.json` drops directly into your workspace. 

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

