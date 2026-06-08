# 上下文管理对齐交接文档

日期：2026-06-08

## 背景

本文档记录 Edgebot 与 nanobot (`E:\nanobot\nanobot\nanobot`) 在上下文管理方面的对齐工作。目标是将 Edgebot 的上下文压缩机制从"简单前缀摘要 + 保留末尾"提升为 nanobot 的完整方案：基于 token 预算的会话归档、有游标的增量整合、以及 runner 级上下文治理。

## 当前差异总结

| 能力 | nanobot | Edgebot 现状 |
|------|---------|-------------|
| Token 预算 | 基于模型的 context_window 动态计算 | 固定阈值 `TOKEN_THRESHOLD=100_000` |
| 归档机制 | `Consolidator` 写入 `memory/history.jsonl`，有 `last_consolidated` 游标 | **已添加** `Consolidator` 与 `last_consolidated` 游标；主循环接入待后续步骤 |
| 历史回放 | `get_history(max_messages, max_tokens)` 只取未归档尾部 | 直接传完整 messages 列表 |
| 摘要注入 | 写入 session metadata，以系统提示段落形式注入 | 合成为普通 user/assistant 消息 |
| 大工具输出 | 持久化到 `.nanobot/tool-results/`，留引用+预览 | 仅 head 截断 |
| `read_file` 强制重读 | 支持 `force=true` 绕过去重 | ~~不支持~~ **已完成** |
| tool 消息 `name` 字段 | 所有 tool 消息都带 `name` | ~~仅 backfill 消息带~~ **已完成** |
| Runner 治理 | drop orphan → backfill → microcompact → tool-result budget → snip history | 仅有 drop/backfill/microcompact，缺 budget 和 snip |
| 空闲压缩 | 通过 Consolidator 统一路径 | 独立实现，默认关闭 |
| 会话文件上限 | `FILE_MAX_MESSAGES=2000`，超限裁剪 | 无 |

## 对齐计划与完成状态

| # | 任务 | 状态 | 涉及文件 |
|---|------|------|----------|
| 1 | tool 消息添加 `name` 字段 | ✅ 已完成 | `edgebot/agent/runner.py` |
| 2 | `read_file` 添加 `force` 参数 | ✅ 已完成 | `edgebot/tools/filesystem.py`, `edgebot/tools/builtin/filesystem.py` |
| 3 | Session 添加 `last_consolidated` 游标 | ✅ 已完成 | `edgebot/session/store.py` |
| 4 | 实现 `Consolidator` 归档到 `memory/history.jsonl` | ✅ 已完成 | 新建 `edgebot/agent/consolidator.py`，改 `edgebot/agent/compression.py` |
| 5 | 用模型感知的 token 预算替换固定阈值 | ⬜ 待实现 | `edgebot/agent/loop.py`, `edgebot/agent/runner.py` |
| 6 | 构建历史回放函数 (max_messages + max_tokens) | ⬜ 待实现 | `edgebot/session/store.py` 或新模块 |
| 7 | 摘要存入 session metadata 并注入系统提示 | ⬜ 待实现 | `edgebot/agent/context.py`, `edgebot/agent/compression.py` |
| 8 | 大工具输出持久化（非 read_file） | ⬜ 待实现 | `edgebot/agent/runner.py`, 新建 `.edgebot/tool-results/` 目录逻辑 |
| 9 | Runner 治理增强 | ⬜ 待实现 | `edgebot/agent/runner.py` |
| 10 | 空闲压缩与文件上限走 Consolidator | ⬜ 待实现 | `edgebot/agent/autocompact.py`, `edgebot/session/store.py` |

## 已完成步骤的实现细节

### Step 1: tool 消息 `name` 传播

**改动位置：** `edgebot/agent/runner.py` 第 264-269 行

```python
tool_msg = {
    "role": "tool",
    "tool_call_id": tc["id"],
    "name": executed.get("name") or tc.get("function", {}).get("name") or "tool",
    "content": output,
}
```

**作用：** `_microcompact()` 依赖 `msg.get("name")` 识别可压缩的工具结果。之前只有 backfill 消息带 `name`，导致正常工具结果无法被微压缩。

### Step 2: `read_file(force=True)`

**改动位置：**
- `edgebot/tools/filesystem.py` — `run_read()` 签名添加 `force: bool = False`，当 `force=True` 时跳过去重检查
- `edgebot/tools/builtin/filesystem.py` — `ReadFileTool` schema 添加可选 `force` 布尔字段，description 说明使用时机

**作用：** 上下文被压缩后，之前读过的文件内容已丢失，agent 再次 `read_file` 会因去重返回 `[File unchanged]` 而拿不到内容。`force=true` 让 agent 能在压缩后强制重读。

### Step 3: Session `last_consolidated` 游标

**改动位置：**
- `edgebot/session/store.py` — metadata 默认包含 `last_consolidated: 0`
- `edgebot/session/store.py` — 新增 `get_last_consolidated()` / `set_last_consolidated()`
- `edgebot/session/store.py` — load/save 时将游标规范化到 `[0, len(messages)]`

**作用：** session 持久化层现在能记录“已归档到 messages 的哪个边界”。旧 session 加载时会自动补齐默认游标，消息列表被压短时游标会被夹到合法范围。

### Step 4: `Consolidator` 增量归档

**改动位置：**
- 新建 `edgebot/agent/consolidator.py`
- `edgebot/agent/compression.py` — 抽出 `summarize_messages()`，供 `auto_compact()` 与 `Consolidator` 共用摘要提示
- 新增 `tests/test_context_consolidator.py`

**作用：** `Consolidator.maybe_consolidate_by_tokens()` 会检查未归档消息的 token 估算，超过阈值时选择不切断 tool_call/tool_result 的 user 消息边界，将归档摘要追加到 `memory/history.jsonl`，再推进 session metadata 的 `last_consolidated`。归档失败时会写入截断后的原始消息 JSON 作为降级记录。当前实现只归档并推进游标，不删除 session 消息；模型感知预算、历史回放和系统提示注入仍在后续步骤。

**验证：**
- `uv run pytest tests/test_context_consolidator.py`
- `uv run python -m compileall edgebot tests`

## 后续实现注意事项

### 关键文件索引

| 文件 | 职责 |
|------|------|
| `edgebot/agent/runner.py` | Agent 主循环，工具执行、消息构造、上下文治理入口 |
| `edgebot/agent/compression.py` | `auto_compact()` 和 `_microcompact()` 的现有实现 |
| `edgebot/agent/loop.py` | 外层循环，触发压缩的 `TOKEN_THRESHOLD` 在此 |
| `edgebot/agent/context.py` | 系统提示和运行时上下文构建 |
| `edgebot/agent/autocompact.py` | 空闲会话压缩（默认关闭） |
| `edgebot/session/store.py` | Session 持久化，消息存取 |
| `edgebot/tools/filesystem.py` | 文件读写核心逻辑 |
| `edgebot/tools/builtin/filesystem.py` | 工具类定义和 schema |
| `edgebot/tools/orchestration.py` | 工具执行编排，返回 `{tool_call, name, args, output}` |
| `edgebot/tools/file_state.py` | 文件读取去重状态管理 |

### 实现建议

1. **Step 3-4 (Consolidator) 是核心结构变更**，建议优先实现。后续步骤大部分依赖它。
   - `last_consolidated` 是一个整数索引，指向 messages 列表中已归档的边界位置
   - 归档边界必须选在 user 消息开头处，不能切断 tool_call/tool_result 对
   - 归档失败时要有降级策略（原文截断存储）

2. **Runner 治理（Step 9）应在模型调用前的副本上执行**，不要修改持久化的 messages 列表。nanobot 的做法是复制一份 history 做裁剪，传给模型，原始列表不变。

3. **Token 计算（Step 5）** 可用 `tiktoken` 做通用估算。关键公式：
   ```
   input_budget = context_window_tokens - max_completion_tokens - 1024
   consolidation_target = int(input_budget * consolidation_ratio)
   ```

4. **大工具输出持久化（Step 8）** 注意：
   - `read_file` 自身已有分页和截断，应豁免通用 offload 逻辑
   - 持久化路径建议：`.edgebot/tool-results/<session_id>/<tool_call_id>.txt`
   - 上下文中保留：文件路径 + 原始大小 + 1200 字符预览

5. **合法边界修复**：任何裁剪历史的操作都需要确保：
   - 不以 orphan tool_result 开头（前面没有对应的 assistant tool_call）
   - 不以 assistant tool_call 结尾（后面没有对应的 tool_result）
   - 尽量从 user 消息开始

6. **测试覆盖**：建议为 `_microcompact()`、`_snip_history()`、`Consolidator.maybe_consolidate_by_tokens()` 编写单元测试，用构造的 messages 列表验证边界裁剪逻辑。
