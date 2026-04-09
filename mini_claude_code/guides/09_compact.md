# 09 - 上下文压缩 (compact.py)

## 问题背景

200K tokens 听起来很多，但一个中型项目的对话很快就会撑满。当上下文快爆时怎么办？CC 的答案：把旧消息压缩成摘要，保留最近几条原文。

## CC 的做法

源码: `rust/crates/runtime/src/compact.rs` (486 行)

### 核心流程

```
1. 估算 session 总 token 数 (len/4+1 启发式)
2. 双触发判断: 消息数 > N 且 tokens >= 阈值
3. 分割: 旧消息 | 最近 N 条保留
4. 旧消息 → 结构化摘要 (不是让 AI 总结，是代码提取)
5. 新 session = [system 摘要消息] + [保留消息]
```

### 摘要提取 (代码做的，不是 AI)

源码 compact.rs:113-198 提取:
- 统计: user/assistant/tool 各多少条
- 工具名: 排序 + 去重
- 最近 3 条用户请求 (truncate 到 160 字符)
- 待办事项: 包含 todo/next/pending/remaining 关键词的消息
- 关键文件: 从内容中提取文件路径 (有 `/` 且有已知扩展名)
- 当前工作: 最后一条非空文本
- 时间线: 每条消息的 role + 内容摘要

### 格式化

摘要输出用 `<summary>` 标签包裹。格式化时:
- 删除 `<analysis>` 块 (AI 的推理过程，不需要保留)
- 提取 `<summary>` 块内容，替换为 `Summary:\n` 前缀

## 你要练习的工程模式

| 模式 | 说明 | 源码位置 |
|------|------|---------|
| **Token 估算启发式** | `len(text) // 4 + 1`，无需 tokenizer | compact.rs:326-338 |
| **双触发条件** | 消息数 > N **且** tokens >= 阈值（两个都满足才压缩）| compact.rs:32-35 |
| **Preserve-recent-N** | 保留最近 N 条原文，压缩其余 | compact.rs:85-90 |
| **结构化摘要** | 代码提取关键信息，不依赖 AI | compact.rs:113-198 |
| **文件路径启发式** | 有 `/` + 已知扩展名 = 可能是文件 | compact.rs:301-315 |
| **XML 标签处理** | strip `<analysis>`, extract `<summary>` | compact.rs:340-360 |

## 你需要写的东西

```python
CompactionConfig(BaseModel)
  preserve_recent_messages: int = 4
  max_estimated_tokens: int = 200_000

CompactionResult(BaseModel)
  summary: str
  formatted_summary: str
  compacted_messages: list[Message]
  removed_count: int

estimate_message_tokens(msg: Message) -> int   # len//4+1 per block
estimate_session_tokens(msgs) -> int
should_compact(msgs, config) -> bool            # 双触发

summarize_messages(msgs) -> str                 # 结构化摘要提取
format_compact_summary(summary) -> str          # strip analysis, extract summary
compact_session(msgs, config) -> CompactionResult

辅助:
  truncate_summary(text, max_chars) -> str      # 截断 + "…"
  extract_file_candidates(text) -> list[str]    # 启发式路径提取
  extract_tag_block(text, tag) -> Optional[str]
  strip_tag_block(text, tag) -> str
```

## 易错点

- token 估算是 `len // 4 + 1`，**+1** 不能忘（每个 block 至少 1 token）
- 双触发: 两个条件用 **AND** 不是 OR
- `preserve_recent_messages` 是从末尾数的，`messages[:-N]` 压缩，`messages[-N:]` 保留
- 摘要里工具名要**排序 + 去重**
- 最近用户请求收集后要**反转回时间序**（因为是 rev + take 收集的）
- 文件候选提取: 要有 `/`（排除纯文件名）且有已知扩展名（.rs/.py/.ts 等）
- `<analysis>` 块**整个删除**，`<summary>` 块只提取内容
