"""
edgebot/team/teammate.py - Persistent autonomous teammate agents.
"""

import json
import threading
import time

import litellm

from edgebot.config import API_BASE, API_KEY, IDLE_TIMEOUT, MODEL, POLL_INTERVAL, TASKS_DIR, TEAM_DIR
from edgebot.tasks.manager import TaskManager
from edgebot.team.bus import MessageBus
from edgebot.tools.filesystem import run_edit, run_read, run_write
from edgebot.tools.shell import run_bash


def _tool(name: str, description: str, parameters: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


class TeammateManager:
    def __init__(self, bus: MessageBus, task_mgr: TaskManager):
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = bus
        self.task_mgr = task_mgr
        self.config_path = TEAM_DIR / "config.json"
        self.config = self._load()
        self.threads = {}

    def _load(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save(self):
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save()
        threading.Thread(
            target=self._loop, args=(name, role, prompt), daemon=True
        ).start()
        return f"Spawned '{name}' (role: {role})"

    def _set_status(self, name: str, status: str):
        member = self._find(name)
        if member:
            member["status"] = status
            self._save()

    def _loop(self, name: str, role: str, prompt: str):
        team_name = self.config["team_name"]
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at "
            f"{TASKS_DIR.parent}. "
            "Use idle when done with current work. You may auto-claim tasks."
        )
        messages = [{"role": "user", "content": prompt}]
        tools = [
            _tool("bash", "Run command.",
                  {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}),
            _tool("read_file", "Read file.",
                  {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
            _tool("write_file", "Write file.",
                  {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}),
            _tool("edit_file", "Edit file.",
                  {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}),
            _tool("send_message", "Send message.",
                  {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}}, "required": ["to", "content"]}),
            _tool("idle", "Signal no more work.",
                  {"type": "object", "properties": {}}),
            _tool("claim_task", "Claim task by ID.",
                  {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}),
        ]

        while True:
            # -- WORK PHASE --
            for _ in range(50):
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg)})
                try:
                    call_messages = [{"role": "system", "content": sys_prompt}] + messages
                    response = litellm.completion(
                        model=MODEL, messages=call_messages,
                        tools=tools, max_tokens=8000,
                        api_key=API_KEY, api_base=API_BASE,
                    )
                except Exception:
                    self._set_status(name, "shutdown")
                    return
                choice = response.choices[0]
                # Store assistant message
                asst_msg = {"role": "assistant", "content": choice.message.content}
                if choice.message.tool_calls:
                    asst_msg["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in choice.message.tool_calls
                    ]
                messages.append(asst_msg)
                if choice.finish_reason != "tool_calls":
                    break
                idle_requested = False
                for tc in choice.message.tool_calls or []:
                    fn_name = tc.function.name
                    args = json.loads(tc.function.arguments)
                    if fn_name == "idle":
                        idle_requested = True
                        output = "Entering idle phase."
                    elif fn_name == "claim_task":
                        output = self.task_mgr.claim(args["task_id"], name)
                    elif fn_name == "send_message":
                        output = self.bus.send(name, args["to"], args["content"])
                    else:
                        dispatch = {
                            "bash": lambda **kw: run_bash(kw["command"]),
                            "read_file": lambda **kw: run_read(kw["path"]),
                            "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
                            "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
                        }
                        output = dispatch.get(fn_name, lambda **kw: "Unknown")(**args)
                    print(f"  [{name}] {fn_name}: {str(output)[:120]}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(output),
                    })
                if idle_requested:
                    break

            # -- IDLE PHASE: poll for messages and unclaimed tasks --
            self._set_status(name, "idle")
            resume = False
            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                time.sleep(POLL_INTERVAL)
                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break
                unclaimed = []
                for f in sorted(TASKS_DIR.glob("task_*.json")):
                    t = json.loads(f.read_text())
                    if t.get("status") == "pending" and not t.get("owner") and not t.get("blockedBy"):
                        unclaimed.append(t)
                if unclaimed:
                    task = unclaimed[0]
                    self.task_mgr.claim(task["id"], name)
                    if len(messages) <= 3:
                        messages.insert(0, {"role": "user", "content":
                            f"<identity>You are '{name}', role: {role}, team: {team_name}.</identity>"})
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                    messages.append({"role": "user", "content":
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n{task.get('description', '')}</auto-claimed>"})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def list_all(self) -> str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]
