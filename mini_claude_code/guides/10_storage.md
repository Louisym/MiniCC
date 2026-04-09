# 10 - 持久化存储 (storage.py)

## 问题背景

Agent 对话可能很长，中途崩溃怎么办？关掉终端后怎么恢复？多个对话怎么独立存储？

CC 用 **JSONL 追加写入** + **UUID 链** 解决这三个问题。

## CC 的做法

源码:
- `rust/crates/runtime/src/session.rs` — Session 序列化/反序列化
- TypeScript 层 `utils/sessionStorage.ts` — JSONL 追加 + UUID 链 + 恢复

### 核心设计

#### 1. JSONL (JSON Lines) 格式
每行是一个独立 JSON 对象。追加写入 (append)，不修改已有内容。

```
{"uuid":"a1","parent_uuid":null,"role":"user","content":[...],"timestamp":"..."}
{"uuid":"b2","parent_uuid":"a1","role":"assistant","content":[...],"timestamp":"..."}
{"uuid":"c3","parent_uuid":"b2","role":"user","content":[...],"timestamp":"..."}
```

好处:
- **崩溃安全**: 最多丢失最后未写完的一行
- **追加不锁**: 不需要读-改-写，直接 append
- **流式处理**: 逐行读取，不需要一次性加载整个文件

#### 2. UUID 链 (Parent-UUID Chain)
每条消息有 `uuid` 和 `parent_uuid`。形成链表:

```
msg-A (parent: null)
  └── msg-B (parent: A)
        └── msg-C (parent: B)
```

好处:
- **分叉**: 从 msg-B 开始另一个分支，共享 A→B 前缀
- **压缩截断**: parent_uuid=null 表示新起点
- **崩溃恢复**: 找到叶节点 → 沿 parent 回溯 → 重建完整对话

#### 3. 中断检测
恢复时检查最后一条消息的 role:
- `user` → 用户发了消息但没收到回复
- `assistant` → 正常结束
- `tool` → 工具执行完但 AI 没继续

## 你要练习的工程模式

| 模式 | 说明 |
|------|------|
| **JSONL 追加写入** | 只 append 不 rewrite，崩溃安全 |
| **UUID 链** | parent_uuid 链表，支持分叉和回溯 |
| **懒物化** | 第一条消息时才创建文件 |
| **中断检测** | 根据最后消息的 role 判断中断类型 |
| **Pydantic 序列化** | model_dump() / model_validate() 做 JSON 转换 |

## 你需要写的东西

```python
StorageEntry(BaseModel)
  uuid: str
  parent_uuid: Optional[str]
  role: str
  content: list[ContentBlock]
  timestamp: str   # ISO format

SessionStore
  __init__(storage_dir: Path)
  save_message(session_id, message, parent_uuid) -> str   # 返回 uuid
  load_session(session_id) -> list[Message]                # 从 JSONL 恢复
  list_sessions() -> list[str]                             # 列出所有会话
  detect_interruption(session_id) -> Optional[str]         # "user"/"tool"/None
  _session_path(session_id) -> Path                        # .jsonl 文件路径
  _append_entry(path, entry)                               # 追加一行
  _read_entries(path) -> list[StorageEntry]                # 逐行解析
  _rebuild_chain(entries) -> list[StorageEntry]             # UUID 链回溯
```

## 易错点

- JSONL 追加时每行末尾要有 `\n`
- 读取时跳过空行和解析失败的行（崩溃安全）
- UUID 用 `uuid.uuid4()` 生成
- `_rebuild_chain`: 找到叶节点 → 沿 parent_uuid 回溯 → 反转得到时间序
- 文件不存在时 `load_session` 返回空列表，不报错
- timestamp 用 UTC ISO format: `datetime.utcnow().isoformat()`
