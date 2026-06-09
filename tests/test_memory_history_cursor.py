import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from edgebot.agent.memory import MemoryStore
from edgebot.providers.base import LLMResponse
from edgebot.session.store import SessionStore


def _records(history_file: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in history_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_memory_store_and_consolidator_share_atomic_history_cursor(tmp_path: Path) -> None:
    from edgebot.agent.consolidator import Consolidator

    class FakeProvider:
        async def chat_with_retry(self, **kwargs):
            return LLMResponse(content="- archived work", finish_reason="stop")

    memory_store = MemoryStore(tmp_path, memory_dir=tmp_path / "memory")
    session_store = SessionStore(tmp_path / "sessions")
    for index in range(8):
        session_store.save_state(
            f"s{index}",
            {
                "messages": [
                    {"role": "user", "content": f"old request {index}"},
                    {"role": "assistant", "content": f"old answer {index}"},
                    {"role": "user", "content": f"recent request {index}"},
                    {"role": "assistant", "content": f"recent answer {index}"},
                ]
            },
        )

    def append_turn(index: int) -> None:
        memory_store.append_history(f"turn summary {index}", session_key=f"direct-{index}")

    def archive_session(index: int) -> None:
        consolidator = Consolidator(
            session_store=session_store,
            provider=FakeProvider(),
            model="test-model",
            memory_dir=tmp_path / "memory",
            keep_recent_messages=2,
        )
        changed = asyncio.run(
            consolidator.maybe_consolidate_by_tokens(
                f"s{index}",
                max_unconsolidated_tokens=1,
            )
        )
        assert changed is True

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for index in range(8):
            futures.append(pool.submit(append_turn, index))
            futures.append(pool.submit(archive_session, index))
        for future in futures:
            future.result()

    records = _records(tmp_path / "memory" / "history.jsonl")
    cursors = [record["cursor"] for record in records]

    assert len(records) == 16
    assert sorted(cursors) == list(range(1, 17))
    assert len(cursors) == len(set(cursors))
    assert (tmp_path / "memory" / ".cursor").read_text(encoding="utf-8") == str(max(cursors))
    assert any(record.get("archived_message_count") == 2 for record in records)


def test_memory_store_instances_for_same_memory_dir_share_append_lock(tmp_path: Path) -> None:
    first = MemoryStore(tmp_path, memory_dir=tmp_path / "memory")
    second = MemoryStore(tmp_path, memory_dir=tmp_path / "memory")

    assert first._append_lock is second._append_lock
