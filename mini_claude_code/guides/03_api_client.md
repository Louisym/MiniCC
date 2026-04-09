# 03 - API 客户端 (api_client.py)

## 问题背景

Agent 需要调用 AI 模型。但 AI 的回复不是一次性返回的，而是**流式传输**：一个字一个字地出来。我们需要把这些流式事件解析成结构化的对象。

## CC 的做法

CC 定义了一个 `ApiClient` trait（接口），任何实现了 `stream()` 方法的类都能当 API 客户端。这样测试时可以用假客户端，生产用真客户端。
源码: `rust/crates/api/src/client.rs`

## 你要练习的工程模式

| 模式 | 说明 |
|------|------|
| **ABC 抽象接口** | 定义合同: "必须有 stream() 方法"，不关心具体实现 |
| **事件分类** | 流式返回的事件分三类: TextDelta / ToolUse / MessageStop |
| **Union 类型** | `AssistantEvent = TextDeltaEvent \| ToolUseEvent \| MessageStopEvent` |
| **依赖注入** | runtime 接收 ApiClient 接口，不绑定具体实现 |

## 你需要写的东西

```
TextDeltaEvent    — type='text_delta', text
ToolUseEvent      — type='tool_use', id, name, input (JSON string)
MessageStopEvent  — type='message_stop'
AssistantEvent    = Union[上面三个]

ApiClient(ABC)
  abstract stream(system_prompt, messages) -> list[AssistantEvent]

ClaudeApiClient(ApiClient)
  __init__(api_key, model, tools=[])
  stream() — 调用 Anthropic SDK，解析事件流
```

## 关键知识: Anthropic SDK 的事件流

```python
with client.messages.stream(...) as stream:
    for event in stream:
        # event.type == 'text' → TextDelta
        # event.type == 'content_block_stop' → 检查是否是 tool_use 块
        # event.type == 'message_stop' → 消息结束
```

tool_use 的 input 在流式过程中是逐步拼出来的，完整的 input 要等到 `content_block_stop` 事件才能拿到。

## 易错点

- `event` 不是 `events`（变量名拼写）
- tool input 要用 `json.dumps()` 转成字符串（因为我们的 ToolContentBlock.input 是 str）
- `stream()` 返回的是 `list[AssistantEvent]`，不是 generator
