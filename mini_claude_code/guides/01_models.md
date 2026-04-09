# 01 - 消息模型 (models.py)

## 问题背景

Agent 和 AI 之间的通信需要结构化消息。AI 的回复不是纯文本——它可能包含文字、工具调用、工具结果，混在一条消息里。怎么建模？

## CC 的做法

CC 把消息的 `content` 设计为 **Block 列表**，每个 Block 有一个 `type` 字段区分类型。
源码: `rust/crates/runtime/src/conversation.rs`

## 你要练习的工程模式

| 模式 | 说明 |
|------|------|
| **Pydantic BaseModel** | 数据验证 + 序列化，比 dataclass 更适合需要 JSON 转换的场景 |
| **Literal 类型锁定** | 每个子类用 `Literal['text'] = 'text'` 锁死 type 值，防止错误赋值 |
| **classmethod 工厂** | `Message.user_text("hi")` 比手动构造 content 列表方便得多 |
| **content 始终是 list** | 即使只有一个 Block，也是列表。统一处理逻辑 |

## 你需要写的东西

```
ContentBlock          — 基类，type 字段
TextContentBlock      — type='text', text 字段
ToolContentBlock      — type='tool_use', id/name/input
ToolResultContentBlock — type='tool_result', id/name/output/is_error
Message               — role + content: list[ContentBlock]
  工厂方法: user_text(), tool_result(), tool_use()
Session               — messages: list[Message] 的包装
```

## 易错点

- `id` 字段类型应该是 `str` 不是 `int`（Claude API 用字符串 ID）
- 子类的 type 需要设默认值：`type: Literal['text'] = 'text'`
- `content` 永远是列表，即使只有一条文本
- classmethod 返回类型用 `"Message"`（引号，前向引用）
