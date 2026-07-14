"""
edgebot/agent/memory/dream.py - DreamProcessor: two-phase memory consolidation.

Phase 1: LLM analyzes conversation history + archived entries, extracts
         structured facts tagged [USER|SOUL|MEMORY|SKILL].

Phase 2: AgentRunner with read_file / edit_file / write_file tools performs
         targeted, incremental edits to memory files and skills instead of
         fragile text-parsing.
"""

from __future__ import annotations

import re
from datetime import datetime

from rich.console import Console

from edgebot.config import MODEL
from edgebot.providers.base import LLMProvider

from edgebot.agent.memory.dream_tools import (
    _DreamEditTool,
    _DreamReadTool,
    _DreamWriteTool,
)
from edgebot.agent.memory.heuristics import (
    _CONVERSATION_MAX_CHARS,
    _extract_actionable_findings,
    _filter_dedup,
    _format_history_entry_for_dream,
    _format_messages,
    _is_dream_visible_history,
    _truncate_text,
)
from edgebot.agent.memory.prompts import PHASE1_PROMPT, PHASE2_SYSTEM_PROMPT
from edgebot.agent.memory.store import MemoryStore, _SKILLS_CONTEXT_MAX_CHARS

_console = Console()

_MAX_MESSAGES = 30
_MAX_ARCHIVED_BATCH = 20
_MEMORY_FILE_MAX_CHARS = 32_000
_SOUL_FILE_MAX_CHARS = 16_000
_USER_FILE_MAX_CHARS = 16_000
_STALE_THRESHOLD_DAYS = 14


class DreamProcessor:
    """Two-phase memory processor using the provider abstraction.

    Phase 1: LLM analyzes conversation + archived history → structured facts.
    Phase 2: AgentRunner with read_file / edit_file tools makes targeted edits.
    """

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        *,
        model: str = MODEL,
        max_live_messages: int = _MAX_MESSAGES,
        max_archived_batch: int = _MAX_ARCHIVED_BATCH,
        emit_output: bool = True,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.max_live_messages = max_live_messages
        self.max_archived_batch = max_archived_batch
        self.emit_output = emit_output

    # ---- input preparation ----

    def _select_archived_batch(self) -> list[dict]:
        entries = self.store.read_unprocessed_history(
            self.store.get_last_dream_cursor()
        )
        return entries[:self.max_archived_batch]

    def _select_live_messages(self, messages: list[dict]) -> list[dict]:
        return messages[-self.max_live_messages:]

    def _build_conversation_context(
        self,
        archived_batch: list[dict],
        live_messages: list[dict],
    ) -> str:
        visible_archived_entries = [
            entry for entry in archived_batch
            if _is_dream_visible_history(entry) and entry.get("content")
        ]
        archived_history = "\n".join(
            _format_history_entry_for_dream(entry)
            for entry in visible_archived_entries
        )
        recent_conversation = _format_messages(live_messages)
        parts: list[str] = []
        if archived_history:
            parts.append(f"## Archived History\n{archived_history}")
        if recent_conversation:
            parts.append(f"## Live Conversation\n{recent_conversation}")
        return _truncate_text("\n\n".join(parts), _CONVERSATION_MAX_CHARS)

    def _annotate_memory_with_ages(self, content: str) -> str:
        """Append per-line git age hints for stale-memory review."""
        try:
            ages = self.store.git.line_ages("memory/MEMORY.md")
        except Exception:
            return content
        if not ages:
            return content

        had_trailing = content.endswith("\n")
        lines = content.splitlines()
        if len(lines) != len(ages):
            return content

        annotated: list[str] = []
        for line, age in zip(lines, ages):
            if line.strip() and age.age_days > _STALE_THRESHOLD_DAYS:
                annotated.append(f"{line}  ← {age.age_days}d")
            else:
                annotated.append(line)
        result = "\n".join(annotated)
        if had_trailing:
            result += "\n"
        return result

    # ---- Phase 1: analysis (plain LLM call, no tools) ----

    async def _phase1_analyze(
        self,
        conversation: str,
        user_content: str,
        soul_content: str,
        memory_content: str,
        skills_content: str,
    ) -> str | None:
        prompt = PHASE1_PROMPT.format(
            user_content=user_content,
            soul_content=soul_content,
            memory_content=memory_content,
            skills_content=skills_content,
            conversation=conversation,
            stale_threshold_days=_STALE_THRESHOLD_DAYS,
        )
        try:
            response = await self.provider.chat_with_retry(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                model=self.model,
                max_tokens=2000,
                temperature=0.3,
            )
            if response.finish_reason == "error":
                return None
            return response.content or ""
        except Exception as exc:
            if self.emit_output:
                _console.print(f"[dim red]  [memory] phase 1 failed: {exc}[/dim red]")
            return None

    # ---- Phase 2: agent-runner with read_file / edit_file ----

    @staticmethod
    def _strip_age_suffix(content: str) -> str:
        return re.sub(r"\s+← \d+d(?=\n|$)", "", content)

    async def _phase2_execute(
        self,
        analysis: str,
        user_content: str,
        soul_content: str,
        memory_content: str,
    ) -> list[dict[str, str]]:
        """Run Phase 2 via AgentRunner with read_file and edit_file tools."""
        from edgebot.agent.runner import AgentRunner, AgentRunSpec

        # Build a minimal registry for the dream agent.
        tool_registry = self._build_dream_tools()

        system_prompt = PHASE2_SYSTEM_PROMPT.format(
            user_path=str(self.store.user_file),
            soul_path=str(self.store.soul_file),
            memory_path=str(self.store.memory_file),
            skills_path=str(self.store.skills_dir),
        )
        skills_content = self.store.read_skills_context()
        user_prompt = (
            f"## Analysis Result\n{analysis}\n\n"
            f"## Current File Contents\n\n"
            f"### USER.md\n{user_content}\n\n"
            f"### SOUL.md\n{soul_content}\n\n"
            f"### MEMORY.md\n{self._strip_age_suffix(memory_content)}\n\n"
            f"### Skills\n{skills_content}"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        runner = AgentRunner(self.provider)
        result = await runner.run(AgentRunSpec(
            initial_messages=messages,
            provider=self.provider,
            tools=[],
            tool_handlers={},
            tool_registry=tool_registry,
            model=self.model,
            max_iterations=15,
            max_tokens=4000,
            max_tool_result_chars=16_000,
            session_key=f"dream:{self.store.workspace.resolve()}",
            emit_output=self.emit_output,
            assistant_label="Dream",
        ))
        if result.stop_reason not in ("completed",):
            raise RuntimeError(f"Dream phase 2 stopped: {result.stop_reason}")

        call_names: dict[str, str] = {}
        changelog: list[dict[str, str]] = []
        for msg in result.messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    func = tc.get("function") or {}
                    if tc.get("id") and isinstance(func, dict):
                        call_names[str(tc["id"])] = str(func.get("name") or "")
            if msg.get("role") != "tool":
                continue
            name = call_names.get(str(msg.get("tool_call_id") or ""))
            content = str(msg.get("content") or "")
            if name in {"edit_file", "write_file"} and content.startswith("Successfully"):
                changelog.append({"name": name, "status": "ok", "detail": content[:200]})
        return changelog

    def _build_dream_tools(self):
        """Build a registry scoped to Dream memory-maintenance tools."""
        from edgebot.tools.registry import ToolRegistry

        read_tool = _DreamReadTool(
            self.store.workspace,
            allowed_files=(
                self.store.user_file,
                self.store.soul_file,
                self.store.memory_file,
            ),
            allowed_skill_dir=self.store.skills_dir,
            allowed_topics_dir=self.store.topics_dir,
        )
        edit_tool = _DreamEditTool(
            self.store.workspace,
            allowed_files=(
                self.store.user_file,
                self.store.soul_file,
                self.store.memory_file,
            ),
            allowed_skill_dir=self.store.skills_dir,
            allowed_topics_dir=self.store.topics_dir,
        )
        write_tool = _DreamWriteTool(
            self.store.workspace,
            skills_dir=self.store.skills_dir,
            topics_dir=self.store.topics_dir,
        )

        registry = ToolRegistry()
        registry.register(read_tool)
        registry.register(edit_tool)
        registry.register(write_tool)
        return registry

    # ---- cursor management ----

    def _advance_cursor(self, archived_batch: list[dict]) -> None:
        if archived_batch:
            self.store.set_last_dream_cursor(archived_batch[-1]["cursor"])
            self.store.compact_history()

    # ---- main entry ----

    async def run(self, messages: list[dict]) -> bool:
        """Run one Dream cycle. Returns True if memory files changed."""
        live_messages = self._select_live_messages(messages)
        archived_batch = self._select_archived_batch()

        substantive = [
            m for m in live_messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        has_signal = bool(archived_batch) or len(substantive) >= 6
        if not has_signal:
            return False

        conversation = self._build_conversation_context(archived_batch, live_messages)
        if not conversation.strip():
            self._advance_cursor(archived_batch)
            return False

        self.store.ensure_git_initialized()
        raw_user_content = self.store.read_user()
        raw_soul_content = self.store.read_soul()
        raw_memory_content = self.store.read_memory()
        raw_skills_content = self.store.read_skills_context()
        user_content = _truncate_text(raw_user_content, _USER_FILE_MAX_CHARS)
        soul_content = _truncate_text(raw_soul_content, _SOUL_FILE_MAX_CHARS)
        memory_content = _truncate_text(
            self._annotate_memory_with_ages(raw_memory_content),
            _MEMORY_FILE_MAX_CHARS,
        )
        skills_content = _truncate_text(raw_skills_content, _SKILLS_CONTEXT_MAX_CHARS)

        # Phase 1: extract structured facts
        analysis = await self._phase1_analyze(
            conversation, user_content, soul_content, memory_content, skills_content,
        )
        if analysis is None:
            return False
        if not analysis.strip():
            self._advance_cursor(archived_batch)
            return False

        # Dedup against existing content
        existing_blob = "\n".join([
            raw_user_content,
            raw_soul_content,
            raw_memory_content,
            raw_skills_content,
        ])
        filtered = _filter_dedup(analysis, existing_blob)
        actionable = _extract_actionable_findings(filtered)
        if not actionable:
            self._advance_cursor(archived_batch)
            return False

        # Phase 2: agent edits files via tools
        try:
            changelog = await self._phase2_execute(
                actionable, user_content, soul_content, memory_content,
            )
        except Exception as exc:
            if self.emit_output:
                _console.print(f"[dim red]  [memory] phase 2 failed: {exc}[/dim red]")
            return False

        if changelog:
            ts = archived_batch[-1]["timestamp"] if archived_batch else datetime.now().strftime("%Y-%m-%d %H:%M")
            commit_msg = f"dream: {ts}, {len(changelog)} change(s)\n\n{actionable.strip()}"
            self._advance_cursor(archived_batch)
            self.store.git.auto_commit(commit_msg)
            if self.emit_output:
                files = [ev["name"] for ev in changelog]
                _console.print(
                    f"[dim]  [memory] Dream updated: {', '.join(files)}[/dim]"
                )
            return True
        self._advance_cursor(archived_batch)
        return False
