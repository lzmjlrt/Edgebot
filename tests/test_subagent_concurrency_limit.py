import asyncio
from pathlib import Path

from edgebot.subagent.runner import SubagentRunner


async def _park_until_cancelled(_task_id: str) -> None:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise


def test_spawn_rejects_second_running_subagent_without_allocating_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _scenario() -> None:
        runner = SubagentRunner(
            root_dir=tmp_path / "subagents",
            max_concurrent_subagents=1,
        )
        monkeypatch.setattr(runner, "_run", _park_until_cancelled)

        first = runner.spawn("explore", "first task", name="first")
        second = runner.spawn("explore", "second task", name="second")

        assert first["task_id"] == "first"
        assert second == {
            "error": "subagent concurrency limit reached",
            "running": 1,
            "limit": 1,
            "message": "Wait for a running subagent to complete before spawning a new one.",
        }
        assert set(runner._tasks) == {"first"}
        assert (tmp_path / "subagents" / "transcripts" / "first.jsonl").exists()
        assert not (tmp_path / "subagents" / "transcripts" / "second.jsonl").exists()
        assert not (tmp_path / "subagents" / "outputs" / "second.txt").exists()

        runner.stop("first")
        await asyncio.sleep(0)

    asyncio.run(_scenario())


def test_running_count_ignores_terminal_and_done_records(tmp_path: Path) -> None:
    runner = SubagentRunner(
        root_dir=tmp_path / "subagents",
        max_concurrent_subagents=1,
    )

    class DoneTask:
        def done(self) -> bool:
            return True

    runner._tasks = {
        "completed": {
            "status": "completed",
            "runner_task": None,
        },
        "failed": {
            "status": "failed",
            "runner_task": None,
        },
        "stopped": {
            "status": "stopped",
            "runner_task": None,
        },
        "done_but_not_terminal": {
            "status": "running",
            "runner_task": DoneTask(),
        }
    }

    assert runner.get_running_count() == 0


def test_duplicate_running_task_id_takes_precedence_over_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _scenario() -> None:
        runner = SubagentRunner(
            root_dir=tmp_path / "subagents",
            max_concurrent_subagents=1,
        )
        monkeypatch.setattr(runner, "_run", _park_until_cancelled)

        first = runner.spawn("explore", "first task", name="same")
        duplicate = runner.spawn("explore", "duplicate task", name="same")

        assert first["task_id"] == "same"
        assert duplicate == {"error": "task_id 'same' already running"}

        runner.stop("same")
        await asyncio.sleep(0)

    asyncio.run(_scenario())


def test_run_and_wait_returns_limit_error_when_runner_is_full(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def _scenario() -> None:
        runner = SubagentRunner(
            root_dir=tmp_path / "subagents",
            max_concurrent_subagents=1,
        )
        monkeypatch.setattr(runner, "_run", _park_until_cancelled)

        first = runner.spawn("explore", "first task", name="first")
        result = await runner.run_and_wait(
            "explore",
            "second task",
            name="second",
            timeout_ms=1,
        )

        assert first["task_id"] == "first"
        assert result["error"] == "subagent concurrency limit reached"
        assert result["running"] == 1
        assert result["limit"] == 1
        assert set(runner._tasks) == {"first"}

        runner.stop("first")
        await asyncio.sleep(0)

    asyncio.run(_scenario())
