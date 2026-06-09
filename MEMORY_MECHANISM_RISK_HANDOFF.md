# 记忆机制风险交接文档

日期：2026-06-09

## 背景

本文件记录 Edgebot 记忆机制与 nanobot (`E:\nanobot\nanobot\nanobot`) 对比后发现的风险点。重点不是上下文管理本身，而是上下文归档与 Dream 长期记忆之间的边界。

当前 Edgebot 的设计是：

- 上下文管理把旧会话片段写入 `.edgebot/memory/history.jsonl`，并用 session metadata 的 `last_consolidated` 控制历史回放。
- Dream 读取 `.edgebot/memory/history.jsonl` 中 `.dream_cursor` 之后的条目，抽取长期事实并更新 `.edgebot/USER.md`、`.edgebot/SOUL.md`、`.edgebot/memory/MEMORY.md`。

也就是说，`history.jsonl` 同时是上下文归档池和 Dream 输入队列。这一点和 nanobot 的整体思路一致，但 Edgebot 的实现边界更松，存在下面这些风险。

## 风险总览

| 优先级 | 风险 | Edgebot 相关文件 | nanobot 参考文件 | 状态 |
|---|---|---|---|---|
| P0 | `history.jsonl` 多写入方绕过统一 cursor/锁 | `edgebot/agent/memory.py`, `edgebot/agent/consolidator.py` | `agent/memory.py` | 已解决 |
| P0 | Dream 编辑工具没有真正限制到记忆文件 | `edgebot/agent/memory.py`, `edgebot/tools/filesystem.py` | `agent/memory.py` | 已解决 |
| P1 | Dream Phase 1 对 `[SKIP]` 的判断会误丢有效发现 | `edgebot/agent/memory.py` | `agent/memory.py`, `templates/agent/dream.md` | 已解决 |
| P1 | Edgebot Dream 缺少 nanobot 的 `SKILL.md` 路由 | `edgebot/agent/memory.py`, `edgebot/templates/skills/memory/SKILL.md` | `templates/agent/dream.md`, `agent/memory.py` | 未对齐 |
| P1 | 每轮摘要和上下文归档都写入同一 history，Dream 输入容易重复/噪声化 | `edgebot/agent/loop.py`, `edgebot/agent/consolidator.py`, `edgebot/agent/memory.py` | `templates/agent/dream.md`, `agent/memory.py` | 未修 |
| P2 | Dream read_file 复用全局文件去重状态，可能读不到当前内容 | `edgebot/agent/memory.py`, `edgebot/tools/filesystem.py` | `agent/memory.py`, `agent/tools/filesystem.py` | 未修 |
| P2 | 缺少专门的 Dream/Memory 测试覆盖 | `tests/` | nanobot 测试/实现路径 | 未补 |
| P2 | `MemoryStore(workspace)` 仍使用全局 `.edgebot` 路径，隔离性差 | `edgebot/agent/memory.py`, `edgebot/config.py` | `agent/memory.py` | 未修 |

## P0: `history.jsonl` 多写入方绕过统一 cursor/锁

状态：已解决（2026-06-09）

修复摘要：

- `MemoryStore.append_history()` 已增加线程锁，cursor 分配、`history.jsonl` append 和 `.cursor` 更新在同一临界区完成。
- `Consolidator` 已改为持有/创建 `MemoryStore`，归档写入统一调用 `MemoryStore.append_history()`，不再直接写 `history.jsonl` 或 `.cursor`。
- `append_history()` 支持 `metadata` 字段，保留 `session_key`、`start_index`、`end_index`、`archived_message_count` 等归档元数据。
- 新增 `tests/test_memory_history_cursor.py`，覆盖 `MemoryStore.append_history()` 与 `Consolidator` 并发写入时 cursor 单调递增、无重复，且 `.cursor` 等于最后一条有效记录 cursor。

### 问题文件

- `edgebot/agent/memory.py`
  - `MemoryStore.append_history()` 分配 cursor、追加 `history.jsonl`、写 `.cursor`。
- `edgebot/agent/consolidator.py`
  - `Consolidator._append_history_record()` 自己读取/写入 `.cursor`，自己追加 `history.jsonl`。
- `edgebot/agent/loop.py`
  - `_archive_turn_summary()` 调用 `MemoryStore.append_history()`。
  - 上下文归档路径调用 `Consolidator.maybe_consolidate_by_tokens()`，后者走自己的写入逻辑。

### 为什么是问题

同一个 `.edgebot/memory/history.jsonl` 和 `.edgebot/memory/.cursor` 现在有至少两个 writer：

1. `MemoryStore.append_history()`
2. `Consolidator._append_history_record()`

这会带来 cursor 竞争风险。比如一轮对话结束时 `_archive_turn_summary()` 写入 turn summary，同时后台/空闲压缩或文件上限裁剪也在写 context archive，两边都可能基于同一个旧 `.cursor` 计算出相同 cursor。

cursor 一旦重复或倒退，Dream 的 `.dream_cursor` 就可能跳过条目、重复处理条目，或者让 `Recent History` 注入逻辑表现不稳定。

### nanobot 参考

- `E:\nanobot\nanobot\nanobot\agent\memory.py`
  - `MemoryStore.append_history()` 使用 `_append_lock` 串行化 cursor 分配和 append。
  - `Consolidator.archive()` 通过 `self.store.append_history(...)` 写入。
  - `MemoryStore.raw_archive()` 也通过 `append_history()` 写入。

nanobot 的关键点是：所有 history 写入都收口到 `MemoryStore.append_history()`。

### 建议修法

- 给 Edgebot 的 `MemoryStore.append_history()` 增加线程锁或异步安全的写入保护。
- 删除或下沉 `Consolidator._append_history_record()` 的直接文件写逻辑。
- 让 `Consolidator` 持有 `MemoryStore` 或接受一个 `append_history` 回调，所有归档写入统一走 `MemoryStore.append_history()`。
- 保留 `session_key/start_index/end_index/archived_message_count` 等字段时，可以让 `append_history()` 支持 `metadata` 或 `extra` 参数。

### 验收标准

- 新增测试：并发触发 `MemoryStore.append_history()` 和 `Consolidator` 归档，最终 `history.jsonl` 中 cursor 单调递增且无重复。
- 新增测试：`.cursor` 等于 `history.jsonl` 最后一条有效记录的 cursor。
- 代码搜索验收：除 `MemoryStore.append_history()` 内部外，不再有业务代码直接 `open(history_file, "a")` 或直接写 `.cursor`。
- 现有上下文测试继续通过：
  - `tests/test_context_consolidator.py`
  - `tests/test_context_autocompact_file_cap.py`
  - `tests/test_context_summary_injection.py`

## P0: Dream 编辑工具没有真正限制到记忆文件

状态：已解决（2026-06-09）

修复摘要：

- `_DreamEditTool` 已增加工具层 allowlist，只允许编辑当前 `MemoryStore` 的 `USER.md`、`SOUL.md`、`memory/MEMORY.md`。
- DreamProcessor 构建工具时会把 store 的实际三类记忆文件传给 edit 工具，测试或自定义 memory_dir 场景不依赖硬编码全局路径。
- 编辑前会先用 `safe_path()` 解析并限制目标仍在工作区内，再用归一化后的真实路径与 allowlist 比对。
- 新增 `tests/test_dream_tool_scope.py`，覆盖非记忆文件拒绝、记忆文件允许，以及 `../`、绝对路径、Windows 分隔符变体不能绕过限制。

### 问题文件

- `edgebot/agent/memory.py`
  - `_DreamReadTool.execute()` 直接调用 `run_read(...)`。
  - `_DreamEditTool.execute()` 直接调用 `run_edit(...)`。
  - `_build_dream_tools()` 的注释写的是 scoped，但没有实际校验目标文件集合。
- `edgebot/tools/filesystem.py`
  - `run_read()` / `run_edit()` 使用 `safe_path()`。
- `edgebot/tools/base.py`
  - `safe_path()` 只限制路径不能逃出 `WORKDIR`，没有限制到记忆文件。

### 为什么是问题

Dream 的任务是维护长期记忆文件，但当前 Dream agent 的 `edit_file` 实际上可以编辑 `WORKDIR` 下任意文件，只要模型传入的 path 没逃出工作区。

这和 Phase 2 系统提示中的限制不等价。提示是软约束，工具层没有硬约束。若 Phase 1 输出被污染、模型误判，或者历史归档中含有诱导内容，Dream 可能修改项目代码、配置文件或其他非记忆文件。

### nanobot 参考

- `E:\nanobot\nanobot\nanobot\agent\memory.py`
  - `MemoryStore.build_dream_tools()`
  - `EditFileTool` 的 `allowed_dir` 是 `memory_dir`，`extra_allowed_dirs` 仅包括 `SOUL.md`、`USER.md`、`skills_dir`。
  - `WriteFileTool` 只允许写 `skills_dir`。

nanobot 的关键点是：Dream 工具在工具层有可编辑根目录限制。

### 建议修法

- 在 Edgebot `_DreamEditTool` 中增加硬编码 allowlist：
  - `.edgebot/USER.md`
  - `.edgebot/SOUL.md`
  - `.edgebot/memory/MEMORY.md`
- 如果后续补齐 SKILL 路由，再允许 `.edgebot/skills/<name>/SKILL.md` 或工作区 skills 目录。
- `_DreamReadTool` 可以读取记忆相关文件和必要技能模板，但编辑工具必须更严格。
- 不要只依赖 prompt 约束。

### 验收标准

- 新增测试：Dream edit 尝试修改 `README.md`、`edgebot/agent/loop.py`、任意非记忆文件时返回拒绝，文件不变。
- 新增测试：Dream edit 修改 `USER.md`、`SOUL.md`、`memory/MEMORY.md` 可以成功。
- 新增测试：路径变体如 `./.edgebot/../README.md`、绝对路径、大小写/分隔符变体不能绕过限制。

## P1: Dream Phase 1 对 `[SKIP]` 的判断会误丢有效发现

状态：已解决（2026-06-09）

修复摘要：

- `DreamProcessor.run()` 不再用全局字符串包含判断把任意 `[SKIP]` 输出当成整批终止信号。
- 新增 `_extract_actionable_findings()`，在 `_filter_dedup()` 之后只提取可执行的 `[USER|SOUL|MEMORY]` 与 `*-REMOVE` 行，并把标签规范为大写。
- `[SKIP]` 现在按行级标签处理；只有纯 skip、空输出或无可执行 findings 时才推进 `.dream_cursor` 并跳过 Phase 2。
- 新增 `tests/test_dream_skip_and_cursor.py`，覆盖 `[SKIP] + [USER]` 混合输出仍进入 Phase 2、纯 `[skip]` 推进 cursor 且不编辑、大小写混合标签行为一致。

### 问题文件

- `edgebot/agent/memory.py`
  - `DreamProcessor.run()`
  - 当前逻辑是：只要 `analysis` 中包含字符串 `[SKIP]`，就推进 `.dream_cursor` 并返回，不再处理同一分析中的其他 `[USER]`、`[SOUL]`、`[MEMORY]` 行。

### 为什么是问题

LLM 很可能输出混合结果，例如：

```text
[SKIP] transient debug logs
[USER] prefers Chinese replies
```

当前实现会因为包含 `[SKIP]` 直接跳过整批 archived history，并推进 `.dream_cursor`。结果是有效记忆发现永久丢失，后续 Dream 不会再处理这些 history 条目。

### nanobot 参考

- `E:\nanobot\nanobot\nanobot\templates\agent\dream.md`
  - `[skip]` 是针对单条内容的分类标签，而不是整批结果的全局终止信号。
- `E:\nanobot\nanobot\nanobot\agent\memory.py`
  - nanobot 的 Dream 由专门 Dream agent 处理完整 prompt，成功完成后推进 cursor。

### 建议修法

- 把 `[SKIP]` 当作行级标签处理。
- 只有当所有有效行都是 `[SKIP]` 或无可处理 `[USER|SOUL|MEMORY|SKILL]` 行时，才推进 cursor 并返回。
- `_filter_dedup()` 之后再判断是否有可执行 findings。

### 验收标准

- 新增测试：Phase 1 输出 `[SKIP]` 加 `[USER]` 时，仍进入 Phase 2。
- 新增测试：Phase 1 仅输出 `[SKIP]` 时，推进 `.dream_cursor` 且不编辑文件。
- 新增测试：Phase 1 输出小写 `[skip]` 和大小写混合标签时行为一致。

## P1: Edgebot Dream 缺少 nanobot 的 `SKILL.md` 路由

### 问题文件

- `edgebot/agent/memory.py`
  - `PHASE1_PROMPT` 只支持 `[USER]`、`[SOUL]`、`[MEMORY]`、`*-REMOVE`。
  - `PHASE2_SYSTEM_PROMPT` 只要求更新 `USER.md`、`SOUL.md`、`MEMORY.md`。
  - `_build_dream_tools()` 没有 `write_file` / `apply_patch` / skill 目录编辑能力。
- `edgebot/templates/skills/memory/SKILL.md`
  - 只描述三类长期记忆文件，没有说明可复用流程应该迁移到 skill。

### 为什么是问题

nanobot 的记忆机制已经把“长期事实”和“可复用工作流”分开：

- 高层项目事实进 `MEMORY.md`。
- 用户偏好进 `USER.md`。
- 行为规则进 `SOUL.md`。
- 重复出现的具体流程、命令、API 参数、操作步骤进 `SKILL.md`。

Edgebot 没有 `[SKILL]` 路由时，Dream 只能把这些内容塞进 `MEMORY.md` 或忽略。长期看会造成 `MEMORY.md` 变成操作手册，和上下文归档摘要混在一起，检索质量下降。

### nanobot 参考

- `E:\nanobot\nanobot\nanobot\templates\agent\dream.md`
  - 明确四类路由：`SOUL.md`、`USER.md`、`memory/MEMORY.md`、`skills/<name>/SKILL.md`。
  - 明确要求把可复用流程迁移到 `SKILL.md`，并从源文件删除重复内容。
- `E:\nanobot\nanobot\nanobot\agent\memory.py`
  - `MemoryStore.build_dream_tools()` 允许 Dream 写 `skills_dir`。

### 建议修法

- 扩展 `PHASE1_PROMPT`，支持 `[SKILL]` 和 `[SKILL-REMOVE]` 或明确的 skill update 结构。
- 扩展 Phase 2，让 Dream 可以创建/更新 `.edgebot/skills/<name>/SKILL.md`。
- 增加技能去重规则：已有技能覆盖同一工作流时，更新已有技能，不创建重复技能。
- 保持 `MEMORY.md` 只存项目高层事实和战略上下文。

### 验收标准

- 新增测试：Phase 1 输出可复用流程时生成或更新对应 `SKILL.md`，不写入 `MEMORY.md`。
- 新增测试：已有 skill 描述匹配时，Dream 更新已有 skill，不创建新目录。
- 新增测试：从 `MEMORY.md` 迁移到 skill 后，`MEMORY.md` 中重复操作步骤被删除。

## P1: 每轮摘要和上下文归档都写入同一 history，Dream 输入容易重复/噪声化

### 问题文件

- `edgebot/agent/loop.py`
  - `_archive_turn_summary()` 每轮把用户和助手预览写入 `history.jsonl`。
- `edgebot/agent/consolidator.py`
  - `maybe_consolidate_by_tokens()` / `compact_idle_session()` / `raw_archive_messages()` 也写入同一个 `history.jsonl`。
- `edgebot/agent/memory.py`
  - `DreamProcessor._select_archived_batch()` 对 history 条目没有按来源或类型区分。

### 为什么是问题

同一段事实可能被写入多次：

1. 每轮 turn summary 写一份。
2. 后续上下文 token 归档再把包含同一轮的旧会话摘要写一份。
3. session file cap 或失败降级 raw archive 又可能写一份。

Dream 的 Phase 1 虽然有 dedup prompt 和 `_filter_dedup()`，但这些都是软防线。重复、噪声化的 archived history 会增加误提取、误删、遗漏和 token 浪费。

### nanobot 参考

- `E:\nanobot\nanobot\nanobot\templates\agent\dream.md`
  - 有 Consolidator tags 的概念：`[skip]`、`[correction]`、`[permanent]`、`[durable]`、`[ephemeral]`。
- `E:\nanobot\nanobot\nanobot\agent\memory.py`
  - `raw_archive()` 和 `archive()` 都通过统一 MemoryStore 写入，并可控制 entry 内容和上限。

### 建议修法

- 给 `history.jsonl` 记录增加结构化字段，例如：
  - `source`: `turn_summary` / `context_archive` / `raw_archive`
  - `session_key`
  - `start_index`
  - `end_index`
  - `tags`: `durable` / `ephemeral` / `skip`
- Dream 处理时按 source 和 tags 决定优先级。
- 考虑关闭或降低 `_archive_turn_summary()` 的写入频率，避免和 context archive 重复。
- 或把 turn summary 写入单独文件，不和 context archive 共用 Dream 输入队列。

### 验收标准

- 新增测试：同一事实同时出现在 turn summary 和 context archive 时，Dream 只生成一条长期记忆。
- 新增测试：`source=raw_archive` 或 `tags=ephemeral` 的条目不会被直接写入长期记忆，除非有明确 durable 证据。
- 新增测试：`read_unprocessed_history()` 能保留并返回结构化字段，Dream prompt 能看到 source/tag 信息。

## P2: Dream read_file 复用全局文件去重状态，可能读不到当前内容

### 问题文件

- `edgebot/agent/memory.py`
  - `_DreamReadTool.parameters` 没有 `force` 参数。
  - `_DreamReadTool.execute()` 调用 `run_read(..., force=False)`。
- `edgebot/tools/filesystem.py`
  - `run_read()` 会因为全局 `file_state` 返回 `[File unchanged since last read: ...]`。

### 为什么是问题

Dream Phase 2 系统提示要求先读当前 `USER.md`、`SOUL.md`、`MEMORY.md`。但如果这些文件刚被主 agent 或之前 Dream 流程读过，`run_read()` 可能返回 `[File unchanged since last read]`，Dream 就拿不到实际内容。

虽然 Phase 2 user prompt 已经嵌入了当前文件内容，但工具提示要求重新读取文件；如果模型依赖工具结果，它可能做出错误编辑。

### nanobot 参考

- `E:\nanobot\nanobot\nanobot\agent\memory.py`
  - `build_dream_tools()` 为 Dream 创建独立 `FileStates()`。
- `E:\nanobot\nanobot\nanobot\agent\tools\filesystem.py`
  - `ReadFileTool` 支持 `force=true`。

### 建议修法

- 给 `_DreamReadTool` 增加 `force` 参数，并默认在 Dream 中强制读取。
- 或为 Dream 使用独立的 file state，不复用主 agent 的读取去重状态。

### 验收标准

- 新增测试：主 agent 先读过 `MEMORY.md` 后，Dream read 仍返回完整内容，而不是 `[File unchanged...]`。
- 新增测试：Dream read schema 包含 `force`，或 Dream read 内部固定 `force=True`。

## P2: 缺少专门的 Dream/Memory 测试覆盖

### 问题文件

- `tests/`
  - 当前上下文相关测试覆盖较多。
  - 未看到专门覆盖 `DreamProcessor`、`.dream_cursor` 推进、Dream git commit、Dream 工具范围、history cursor 并发的测试。

### 为什么是问题

记忆机制的失败通常不是立即抛异常，而是表现为：

- 长期事实没写入。
- 错误事实写入。
- cursor 推进导致事实永久丢失。
- Dream 修改了不该修改的文件。
- `history.jsonl` cursor 重复导致后续处理不稳定。

这些都需要构造性测试，不能只靠人工观察。

### nanobot 参考

- 参考实现位置：
  - `E:\nanobot\nanobot\nanobot\agent\memory.py`
  - `E:\nanobot\nanobot\nanobot\templates\agent\dream.md`
  - `E:\nanobot\nanobot\nanobot\command\builtin.py` 的 `/dream` 流程。

### 建议补充测试

- `tests/test_memory_history_cursor.py`
- `tests/test_dream_processor.py`
- `tests/test_dream_tool_scope.py`
- `tests/test_dream_skip_and_cursor.py`
- `tests/test_dream_skill_routing.py`

### 验收标准

- 上述测试覆盖 P0/P1 风险。
- 测试中使用 fake provider，不依赖真实 LLM。
- 测试使用临时 runtime/memory 目录，不能污染仓库 `.edgebot`。

## P2: `MemoryStore(workspace)` 仍使用全局 `.edgebot` 路径，隔离性差

### 问题文件

- `edgebot/agent/memory.py`
  - `MemoryStore.__init__(workspace)` 接收 workspace 参数，但 `self.memory_dir = MEMORY_DIR`，`self.soul_file = SOUL_MD_PATH`，`self.user_file = USER_MD_PATH`。
- `edgebot/config.py`
  - `MEMORY_DIR`、`SOUL_MD_PATH`、`USER_MD_PATH` 是基于当前全局 `WORKDIR` 的路径。

### 为什么是问题

`MemoryStore` 表面上可以按 workspace 实例化，但内部仍写全局 `.edgebot`。这会让单元测试、多 workspace、子进程或未来复用 MemoryStore 时隔离性变差。

比如测试里传入 `tmp_path`，按直觉应该写 `tmp_path/.edgebot/memory`，但当前实现仍会指向配置里的全局 runtime。

### nanobot 参考

- `E:\nanobot\nanobot\nanobot\agent\memory.py`
  - `MemoryStore.__init__(workspace)` 使用 `workspace / "memory"`、`workspace / "SOUL.md"`、`workspace / "USER.md"`。

### 建议修法

- 让 `MemoryStore` 的路径真正从 `workspace` 派生。
- 如果 Edgebot 必须使用 `.edgebot` 子目录，则派生为：
  - `workspace / ".edgebot" / "memory"`
  - `workspace / ".edgebot" / "SOUL.md"`
  - `workspace / ".edgebot" / "USER.md"`
- 保留从旧全局路径迁移的兼容逻辑。

### 验收标准

- 新增测试：`MemoryStore(tmp_path)` 只在 `tmp_path` 下创建和修改文件。
- 新增测试：两个不同 workspace 的 `MemoryStore` 写入互不影响。
- 现有 CLI 启动路径仍能读取当前 workspace 的 `.edgebot` 记忆文件。

## 建议修复顺序

1. 先修 P0 cursor 写入收口。
2. 再修 P0 Dream 工具硬约束。
3. 再修 P1 `[SKIP]` 误丢有效发现。
4. 再决定是否完整引入 nanobot 的 `[SKILL]` 路由。
5. 最后补 source/tag 结构化 history 和 workspace 隔离。

## 最低验收命令

完成修复后至少运行：

```powershell
uv run pytest tests/test_context_budget_and_history.py tests/test_context_consolidator.py tests/test_context_summary_injection.py tests/test_runner_context_governance.py tests/test_context_autocompact_file_cap.py
uv run pytest tests/test_memory_history_cursor.py tests/test_dream_processor.py tests/test_dream_tool_scope.py tests/test_dream_skip_and_cursor.py
```

如果补齐 `SKILL.md` 路由，再运行：

```powershell
uv run pytest tests/test_dream_skill_routing.py
```

## 一句话结论

Edgebot 的记忆机制已经有 Dream 雏形，也和 nanobot 一样复用了 `history.jsonl` 作为上下文归档到长期记忆的桥梁；但 Edgebot 目前在 writer 收口、工具硬约束、SKILL 路由、Dream 测试覆盖上没有完全对齐 nanobot。这些问题优先级高于进一步优化摘要质量。
