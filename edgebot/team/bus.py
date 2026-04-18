"""
edgebot/team/bus.py - File-backed inter-agent message bus.
"""

import json
import time

from edgebot.config import INBOX_DIR


class MessageBus:
    def __init__(self):
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

    def send(
        self,
        sender: str,
        to: str,
        content: str,
        msg_type: str = "message",
        extra: dict = None,
    ) -> str:
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        with open(INBOX_DIR / f"{to}.jsonl", "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        path = INBOX_DIR / f"{name}.jsonl"
        if not path.exists():
            return []
        # Atomic drain: rename the file aside so concurrent send() opens a fresh one.
        tmp = INBOX_DIR / f"{name}.jsonl.reading.{int(time.time() * 1000)}"
        try:
            path.rename(tmp)
        except FileNotFoundError:
            return []
        try:
            lines = tmp.read_text().strip().splitlines()
            msgs = []
            for l in lines:
                if not l:
                    continue
                try:
                    msgs.append(json.loads(l))
                except json.JSONDecodeError:
                    continue
        finally:
            tmp.unlink(missing_ok=True)
        return msgs

    def broadcast(self, sender: str, content: str, names: list) -> str:
        count = 0
        for n in names:
            if n != sender:
                self.send(sender, n, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"
