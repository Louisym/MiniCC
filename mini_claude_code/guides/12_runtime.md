# 12 - 对话运行时 (runtime.py)

## 问题背景

前面 11 个模块各管一摊事: API 调用、工具执行、权限校验、Hook 拦截、Token 压缩、会话存储……但谁来把这些全部串起来？

这就是 **ConversationRuntime** — 整个 agent 的心跳循环。它是 CC 的"脊柱"。

## CC 的做法

源码: `rust/crates/runtime/src/conversation.rs` (完整 973 行)

### 核心循环 — run_turn() (conversation.rs:170-283)

```
用户输入 → 推入 session
         ↓
  ┌──→ 调 API (stream) ──→ 构建 assistant 消息
  │              ↓
  │    提取 tool_use blocks
  │              ↓
  │    没有 tool_use? ──→ break (结束循环)
  │              ↓
  │    对每个 tool_use:
  │      ├─ 权限校验 (authorize)
  │      │   └─ 被拒 → 返回 deny reason 作为 tool_result (is_error=true)
  │      ├─ PreToolUse hook
  │      │   └─ 被拒 → 跳过工具，返回 hook deny message
  │      ├─ 执行工具 (tool_executor.execute)
  │      ├─ PostToolUse hook (合并反馈)
  │      └─ 推入 tool_result 到 session
  │              ↓
  └──────────── loop
         ↓
  自动压缩检查 (maybe_auto_compact)
         ↓
  返回 TurnSummary
```

### 1. 泛型双接口 — ApiClient + ToolExecutor

```rust
pub struct ConversationRuntime<C, T> {
    api_client: C,          // C: ApiClient trait
    tool_executor: T,       // T: ToolExecutor trait
    // ...
}
```

**为什么泛型?** 测试时传 mock，生产传真实客户端。Python 用 Protocol 实现同样效果。

### 2. build_assistant_message() — 事件流 → 消息 (conversation.rs:353-390)

API 返回的是事件流 (TextDelta, ToolUse, Usage, MessageStop)，需要"攒"成一条完整消息:
- TextDelta → 累积到文本缓冲区
- ToolUse → flush 当前文本，添加 tool_use block
- MessageStop → 标记结束
- 没有 MessageStop → 报错

### 3. Hook 反馈合并 — merge_hook_feedback() (conversation.rs:408-424)

Pre/Post hook 可能输出消息。这些消息要追加到工具输出中，让 LLM 看到。
如果 hook deny 了，还要标记 `is_error=true`。

### 4. 自动压缩 — maybe_auto_compact() (conversation.rs:310-333)

每轮结束后检查: `cumulative_input_tokens >= threshold?`
- 是 → 执行 compact_session，替换 session 中的消息
- 否 → 不做任何事
- 阈值来自环境变量，默认 200,000

### 5. UsageTracker — Token 用量累计 (usage.rs:162-209)

跨 turn 累计 input/output/cache tokens。从 session 恢复时重建 (from_session)。

### 6. TurnSummary — 结构化的返回值 (conversation.rs:87-93)

不是简单返回文本，而是结构化:
- assistant_messages: 所有 assistant 消息
- tool_results: 所有工具结果
- iterations: 循环了几轮
- usage: 累计 token 用量
- auto_compaction: 是否触发了自动压缩

## 你要练习的工程模式

| 模式 | 说明 | 源码位置 |
|------|------|---------|
| **Harness = Body, LLM = Brain** | Runtime 只负责感知/执行/约束，不做任何"思考" | conversation.rs 全文 |
| **泛型双接口** | ApiClient + ToolExecutor 通过 trait/Protocol 注入 | conversation.rs:100-110 |
| **事件 → 消息重建** | build_assistant_message 把流式事件攒成结构化消息 | conversation.rs:353-390 |
| **flush 模式** | TextDelta 累积，遇到 ToolUse 时 flush 文本缓冲 | conversation.rs:392-398 |
| **max_iterations 安全阀** | 防止无限循环 | conversation.rs:185-189 |
| **Hook 反馈合并** | Pre/Post hook 消息追加到工具输出 | conversation.rs:408-424 |
| **自动压缩** | 基于累计 input_tokens 的阈值触发 | conversation.rs:310-333 |
| **TurnSummary 结构化返回** | 不只是文本，包含元数据 | conversation.rs:87-93 |

## 你需要写的东西

```python
# --- Token 用量追踪 ---
TokenUsage(BaseModel)
  input_tokens: int = 0
  output_tokens: int = 0
  cache_creation_input_tokens: int = 0
  cache_read_input_tokens: int = 0
  total_tokens() -> int

UsageTracker
  __init__()
  record(usage: TokenUsage)
  current_turn_usage() -> TokenUsage
  cumulative_usage() -> TokenUsage
  turns() -> int

# --- Protocol 接口 ---
ToolExecutor(Protocol)
  def execute(self, tool_name: str, input: str) -> str: ...
    # 成功返回字符串，失败抛 ToolError

ToolError(Exception)

# --- 构建 assistant 消息 ---
build_assistant_message(events: list[AssistantEvent])
    -> tuple[Message, Optional[TokenUsage]]
  # flush 模式: TextDelta 累积 → ToolUse 时 flush → MessageStop 结束

# --- Hook 反馈合并 ---
merge_hook_feedback(messages: list[str], output: str, denied: bool) -> str

# --- TurnSummary ---
TurnSummary(BaseModel)
  assistant_messages: list[Message]
  tool_results: list[Message]
  iterations: int
  usage: TokenUsage
  auto_compacted: bool

# --- ConversationRuntime ---
ConversationRuntime
  __init__(session, api_client, tool_executor, permission_policy,
           system_prompt, hook_runner=None)
  with_max_iterations(n) -> self          # Builder 模式
  with_auto_compact_threshold(n) -> self  # Builder 模式
  run_turn(user_input, prompter=None) -> TurnSummary
  session() -> Session
  usage() -> UsageTracker
```

## 易错点

- `build_assistant_message`: TextDelta 要累积，不是每个 delta 一个 block。遇到 ToolUse 时先 flush 文本缓冲
- 没有 MessageStop 事件 → 报错，不是静默忽略
- 权限拒绝 → deny reason 作为 tool_result (is_error=True)，不是抛异常
- Hook deny → 工具不执行，但要继续循环（LLM 需要看到 deny 消息才能调整策略）
- `maybe_auto_compact`: 压缩后要替换 session 的消息列表，不是创建新 session
- `max_iterations` 默认值不是无限大 (CC 用 usize::MAX，我们可以用 1000 或类似的大数)
- UsageTracker 的 `record()` 是累加，不是替换
