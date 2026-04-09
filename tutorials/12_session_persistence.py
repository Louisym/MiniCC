"""
Tutorial 12: 会话持久化 — Agent 的记忆力
========================================

为什么会话持久化重要？

    没有持久化的 Agent = 金鱼记忆。
    每次关掉终端，所有对话历史全部丢失。
    用户第二天回来，要从头开始。

    有了会话持久化：
    - 关掉终端再打开 → 用 --resume 恢复上次对话
    - 程序崩溃 → 已保存的消息不丢失
    - 多个会话 → 每个会话独立存储，可以切换

生活类比：
    没有持久化 = 和一个失忆的人聊天，每次都要重新介绍自己
    有持久化 = 和一个记日记的人聊天，他翻翻日记就知道上次聊到哪了

    JSONL 格式 = 日记本。每说一句话就记一行。
    如果日记本被撕了最后一页（程序崩溃），前面的内容都还在。

Claude Code 的持久化策略：
    1. JSONL 仅追加 — 每条消息写一行 JSON，不修改已有内容
    2. Parent-UUID 链 — 消息形成链表，支持分支和恢复
    3. 崩溃安全 — 最多丢一行（最后没写完的那行）

对应源码：
    - rust/crates/runtime/src/session.rs → Session, save_to_path(), load_from_path()
    - rust/crates/runtime/src/conversation.rs → ConversationRuntime 使用 Session
    - reference EP09 → 完整的 JSONL 持久化设计（TS 版本更复杂）

运行方式：python tutorials/12_session_persistence.py
"""

import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path


# ============================================================
# 第一步：理解 JSONL 格式 — 每行一条 JSON
# ============================================================
# JSONL (JSON Lines) = 每行是一个独立的 JSON 对象
#
# 和普通 JSON 的区别：
#   普通 JSON：整个文件是一个大 JSON 对象（修改需要重写整个文件）
#   JSONL：每行独立（只需要在末尾追加新行）
#
# 为什么 Agent 系统选择 JSONL？
#   1. 追加写入 — 新消息直接写在文件末尾，不用读-改-写整个文件
#   2. 崩溃安全 — 程序崩溃时，最多丢失最后一行（写到一半的那行）
#   3. 流式处理 — 可以逐行读取，不需要全部加载到内存
#
# 举例：
#   {"type":"user","text":"你好"}
#   {"type":"assistant","text":"你好！有什么我可以帮你的？"}
#   {"type":"user","text":"帮我写个函数"}
#   {"type":"assistant","text":"好的","tool_use":{"name":"write_file",...}}
#   {"type":"tool_result","output":"文件已创建"}


# ============================================================
# 第二步：消息模型 — 对应源码中的 Session 和 ConversationMessage
# ============================================================

@dataclass
class ContentBlock:
    """
    消息内容块。

    对应源码: session.rs:18-33
        pub enum ContentBlock {
            Text { text: String },
            ToolUse { id, name, input },
            ToolResult { tool_use_id, tool_name, output, is_error },
        }
    """
    block_type: str   # "text", "tool_use", "tool_result"
    text: str = ""
    tool_id: str = ""
    tool_name: str = ""
    tool_input: str = ""
    tool_use_id: str = ""
    output: str = ""
    is_error: bool = False

    def to_dict(self) -> dict:
        """序列化为字典（用于 JSON）"""
        if self.block_type == "text":
            return {"type": "text", "text": self.text}
        elif self.block_type == "tool_use":
            return {
                "type": "tool_use",
                "id": self.tool_id,
                "name": self.tool_name,
                "input": self.tool_input,
            }
        elif self.block_type == "tool_result":
            return {
                "type": "tool_result",
                "tool_use_id": self.tool_use_id,
                "tool_name": self.tool_name,
                "output": self.output,
                "is_error": self.is_error,
            }
        return {}

    @staticmethod
    def from_dict(data: dict) -> "ContentBlock":
        """从字典反序列化"""
        block_type = data["type"]
        if block_type == "text":
            return ContentBlock(block_type="text", text=data["text"])
        elif block_type == "tool_use":
            return ContentBlock(
                block_type="tool_use",
                tool_id=data["id"],
                tool_name=data["name"],
                tool_input=data["input"],
            )
        elif block_type == "tool_result":
            return ContentBlock(
                block_type="tool_result",
                tool_use_id=data["tool_use_id"],
                tool_name=data["tool_name"],
                output=data["output"],
                is_error=data.get("is_error", False),
            )
        raise ValueError(f"unknown block type: {block_type}")


@dataclass
class ConversationMessage:
    """
    对话消息。

    对应源码: session.rs:36-46
        pub struct ConversationMessage {
            pub role: MessageRole,
            pub blocks: Vec<ContentBlock>,
            pub usage: Option<TokenUsage>,
        }
    """
    role: str  # "user", "assistant", "tool"
    blocks: list[ContentBlock]
    uuid: str = ""
    parent_uuid: str = ""
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.uuid:
            self.uuid = str(uuid.uuid4())[:8]
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "blocks": [b.to_dict() for b in self.blocks],
            "uuid": self.uuid,
            "parent_uuid": self.parent_uuid,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(data: dict) -> "ConversationMessage":
        return ConversationMessage(
            role=data["role"],
            blocks=[ContentBlock.from_dict(b) for b in data["blocks"]],
            uuid=data.get("uuid", ""),
            parent_uuid=data.get("parent_uuid", ""),
            timestamp=data.get("timestamp", 0.0),
        )

    @staticmethod
    def user_text(text: str) -> "ConversationMessage":
        return ConversationMessage(
            role="user",
            blocks=[ContentBlock(block_type="text", text=text)],
        )

    @staticmethod
    def assistant_text(text: str) -> "ConversationMessage":
        return ConversationMessage(
            role="assistant",
            blocks=[ContentBlock(block_type="text", text=text)],
        )


# ============================================================
# 第三步：Session — 管理消息列表
# ============================================================

@dataclass
class Session:
    """
    会话：消息的有序集合。

    对应源码: session.rs:42-46
        pub struct Session {
            pub version: u32,
            pub messages: Vec<ConversationMessage>,
        }
    """
    version: int = 1
    messages: list[ConversationMessage] = field(default_factory=list)
    session_id: str = ""

    def __post_init__(self):
        if not self.session_id:
            self.session_id = str(uuid.uuid4())[:12]

    def add_message(self, message: ConversationMessage):
        """
        添加消息，自动设置 parent_uuid 形成链表。
        """
        if self.messages:
            message.parent_uuid = self.messages[-1].uuid
        self.messages.append(message)


# ============================================================
# 第四步：两种持久化方案
# ============================================================
# 方案 A：JSON 整文件（Rust 源码中的简单实现）
# 方案 B：JSONL 仅追加（Reference EP09 描述的生产实现）

# --- 方案 A：JSON 整文件 ---
# 这是 Rust 源码中实际实现的方式（简单但有局限）

def save_session_json(session: Session, path: str):
    """
    整文件 JSON 保存。

    对应源码: session.rs:88-91
        pub fn save_to_path(&self, path) -> Result<(), SessionError> {
            fs::write(path, self.to_json().render())?;
            Ok(())
        }
    """
    data = {
        "version": session.version,
        "session_id": session.session_id,
        "messages": [m.to_dict() for m in session.messages],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_session_json(path: str) -> Session:
    """
    从 JSON 文件加载会话。

    对应源码: session.rs:93-96
        pub fn load_from_path(path) -> Result<Self, SessionError> {
            let contents = fs::read_to_string(path)?;
            Self::from_json(&JsonValue::parse(&contents)?)
        }
    """
    with open(path, "r") as f:
        data = json.load(f)
    session = Session(
        version=data["version"],
        session_id=data.get("session_id", ""),
    )
    for msg_data in data["messages"]:
        session.messages.append(ConversationMessage.from_dict(msg_data))
    return session


# --- 方案 B：JSONL 仅追加 ---
# 这是 Reference EP09 描述的生产级方案

class JsonlSessionStorage:
    """
    JSONL 会话存储器。

    设计要点（来自 Reference EP09）：
    1. 仅追加写入 — 不修改已有内容
    2. 崩溃安全 — 追加写入是原子操作（配合 O_APPEND）
    3. 每条消息独立 — 一行坏了不影响其他行

    对应 Reference: architecture/zh-CN/09-session-persistence.md
        - sessionStorage.ts:5,106 行
        - JSONL 仅追加存储
    """

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _session_path(self, session_id: str) -> str:
        """
        生成会话文件路径。

        对应 Reference: EP09 §1
            ~/.claude/projects/{sanitized_cwd}/{session-id}.jsonl
        """
        return os.path.join(self.base_dir, f"{session_id}.jsonl")

    def append_message(self, session_id: str, message: ConversationMessage):
        """
        追加一条消息到 JSONL 文件。

        这是最核心的写入操作。每条消息一行，追加到文件末尾。

        对应 Reference: EP09 §2
            Project.appendEntry() → enqueueWrite(filePath, entry)
        """
        path = self._session_path(session_id)
        entry = {
            "type": message.role,
            **message.to_dict(),
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(path, "a") as f:
            f.write(line)

    def append_metadata(self, session_id: str, meta_type: str, data: dict):
        """
        追加元数据条目（标题、标签等）。

        对应 Reference: EP09 §1 条目类型
            custom-title, ai-title, last-prompt, tag 等
        """
        entry = {"type": meta_type, **data}
        path = self._session_path(session_id)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(path, "a") as f:
            f.write(line)

    def load_session(self, session_id: str) -> Session:
        """
        从 JSONL 文件加载会话。

        逐行解析，跳过解析失败的行（崩溃安全）。

        对应 Reference: EP09 §3
            parseJSONL → Map<UUID, TranscriptMessage>
        """
        path = self._session_path(session_id)
        session = Session(session_id=session_id)
        metadata = {}

        with open(path, "r") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # 崩溃安全：跳过写到一半的行
                    print(f"    [WARNING] 第 {line_no} 行解析失败，跳过 "
                          "(可能是崩溃时写到一半的数据)")
                    continue

                entry_type = entry.get("type", "")
                if entry_type in ("user", "assistant", "tool"):
                    session.messages.append(
                        ConversationMessage.from_dict(entry)
                    )
                elif entry_type == "custom-title":
                    metadata["title"] = entry.get("title", "")
                elif entry_type == "last-prompt":
                    metadata["last_prompt"] = entry.get("text", "")

        return session

    def list_sessions(self) -> list[dict]:
        """
        列出所有会话（用于 --resume 选择界面）。

        对应 Reference: EP09 §8
            listSessionsImpl() 使用两阶段策略
        """
        sessions = []
        for filename in os.listdir(self.base_dir):
            if not filename.endswith(".jsonl"):
                continue
            path = os.path.join(self.base_dir, filename)
            session_id = filename[:-6]  # 去掉 .jsonl

            # 快速读取：只读头尾
            # 对应 Reference: EP09 §4
            #   readHeadAndTail(filePath, fileSize, buf)
            #   LITE_READ_BUF_SIZE = 65536 (64KB)
            first_prompt = ""
            last_prompt = ""
            mtime = os.path.getmtime(path)

            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") == "user" and not first_prompt:
                        blocks = entry.get("blocks", [])
                        for b in blocks:
                            if b.get("type") == "text":
                                first_prompt = b["text"][:80]
                                break
                    if entry.get("type") == "last-prompt":
                        last_prompt = entry.get("text", "")[:80]

            sessions.append({
                "session_id": session_id,
                "first_prompt": first_prompt,
                "last_prompt": last_prompt or first_prompt,
                "modified": mtime,
            })

        # 按修改时间降序排列
        sessions.sort(key=lambda s: s["modified"], reverse=True)
        return sessions


# ============================================================
# 第五步：Parent-UUID 链 — 消息的家谱
# ============================================================
# 每条消息有 uuid 和 parent_uuid，形成链表结构。
#
# 为什么需要链表而不是简单的数组索引？
#   1. 分支支持：fork 时，两个分支共享前面的消息
#   2. 压缩边界：压缩后 parent_uuid 设为 null，截断历史
#   3. 崩溃恢复：从叶节点沿链回溯，重建完整对话

def build_conversation_chain(
    messages: dict[str, ConversationMessage],
    leaf_uuid: str,
) -> list[ConversationMessage]:
    """
    从叶节点沿 parent_uuid 链回溯，重建对话。

    对应 Reference: EP09 §3
        let currentMsg = leafMessage
        while (currentMsg) {
            if (seen.has(currentMsg.uuid)) break  // 环检测
            seen.add(currentMsg.uuid)
            transcript.push(currentMsg)
            currentMsg = messages.get(currentMsg.parentUuid)
        }
        transcript.reverse()
    """
    chain = []
    seen = set()  # 环检测（防止损坏的链指针导致无限循环）
    current_uuid = leaf_uuid

    while current_uuid:
        if current_uuid in seen:
            print(f"    [WARNING] 检测到循环引用 {current_uuid}，停止遍历")
            break
        seen.add(current_uuid)

        msg = messages.get(current_uuid)
        if msg is None:
            break

        chain.append(msg)
        current_uuid = msg.parent_uuid if msg.parent_uuid else ""

    chain.reverse()  # 从根到叶的顺序
    return chain


# ============================================================
# 第六步：中断检测 — 上次会话是怎么结束的？
# ============================================================

def detect_interruption(messages: list[ConversationMessage]) -> str:
    """
    检测上次会话是否在中途被中断。

    对应 Reference: EP09 §3
        detectTurnInterruption():
        | 最后消息类型       | 状态         | 动作                     |
        | assistant         | 轮次完成     | none                     |
        | user(tool_result) | 工具执行中   | interrupted_turn → 注入继续 |
        | user(text)        | 提示未响应   | interrupted_prompt        |
    """
    if not messages:
        return "empty"

    last = messages[-1]

    if last.role == "assistant":
        return "completed"  # 正常结束

    if last.role == "tool":
        return "interrupted_turn"  # 工具执行中被中断

    if last.role == "user":
        # 检查是不是 tool_result
        for block in last.blocks:
            if block.block_type == "tool_result":
                return "interrupted_turn"
        return "interrupted_prompt"  # 用户提问了但没收到回复

    return "unknown"


# ============================================================
# 第七步：演示
# ============================================================

def main():
    print("=" * 60)
    print("Tutorial 12: 会话持久化（JSONL）")
    print("=" * 60)

    # 创建临时目录
    tmpdir = tempfile.mkdtemp(prefix="session_demo_")

    # --- 1. 方案 A：JSON 整文件 ---
    print("\n--- 方案 A: JSON 整文件（源码中的实现）---")

    session = Session()
    session.add_message(ConversationMessage.user_text("帮我写个排序函数"))
    session.add_message(ConversationMessage.assistant_text(
        "好的，我来帮你写一个快速排序。"
    ))

    json_path = os.path.join(tmpdir, "session.json")
    save_session_json(session, json_path)
    print(f"  保存到: {json_path}")

    restored = load_session_json(json_path)
    print(f"  恢复成功！消息数: {len(restored.messages)}")
    for msg in restored.messages:
        text = msg.blocks[0].text if msg.blocks else ""
        print(f"    [{msg.role}] {text[:40]}")

    # --- 2. 方案 B：JSONL 仅追加 ---
    print("\n--- 方案 B: JSONL 仅追加（生产级方案）---")

    storage = JsonlSessionStorage(tmpdir)
    sid = "demo-session-001"

    # 模拟对话
    msgs = [
        ConversationMessage.user_text("帮我分析一下这个 bug"),
        ConversationMessage.assistant_text(
            "好的，让我看看代码。"
        ),
        ConversationMessage.user_text("bug 在 auth.py 第 42 行"),
        ConversationMessage.assistant_text(
            "我看到了，这是一个空指针问题。已修复。"
        ),
    ]

    # 逐条追加（模拟实时对话）
    prev_uuid = ""
    for msg in msgs:
        msg.parent_uuid = prev_uuid
        storage.append_message(sid, msg)
        prev_uuid = msg.uuid
        print(f"  追加: [{msg.role}] {msg.blocks[0].text[:30]}...")

    # 追加元数据
    storage.append_metadata(sid, "custom-title",
                            {"title": "修复 auth.py 空指针 bug"})
    storage.append_metadata(sid, "last-prompt",
                            {"text": "bug 在 auth.py 第 42 行"})
    print(f"  追加: 标题和最后提示")

    # 查看文件内容
    jsonl_path = storage._session_path(sid)
    print(f"\n  JSONL 文件内容 ({jsonl_path}):")
    with open(jsonl_path, "r") as f:
        for i, line in enumerate(f, 1):
            entry = json.loads(line)
            entry_type = entry.get("type", "?")
            if entry_type in ("user", "assistant"):
                blocks = entry.get("blocks", [])
                text = blocks[0]["text"][:30] if blocks else ""
                print(f"    行{i}: [{entry_type}] {text}...")
            else:
                print(f"    行{i}: [{entry_type}] (元数据)")

    # 加载
    loaded = storage.load_session(sid)
    print(f"\n  加载成功！消息数: {len(loaded.messages)}")

    # --- 3. 崩溃安全演示 ---
    print("\n--- 崩溃安全演示 ---")

    crash_sid = "crash-demo"
    storage.append_message(crash_sid,
                           ConversationMessage.user_text("第一条消息"))
    storage.append_message(crash_sid,
                           ConversationMessage.assistant_text("第二条消息"))

    # 模拟崩溃：写一半的数据
    crash_path = storage._session_path(crash_sid)
    with open(crash_path, "a") as f:
        f.write('{"type":"assistant","blocks":[{"type":"tex')
        # ↑ 写到一半就"崩溃"了！JSON 不完整

    # 尝试加载
    loaded = storage.load_session(crash_sid)
    print(f"  崩溃后加载：成功恢复 {len(loaded.messages)} 条消息"
          "（损坏的行被跳过）")

    # --- 4. Parent-UUID 链演示 ---
    print("\n--- Parent-UUID 链演示 ---")

    # 构建消息链
    msg_a = ConversationMessage.user_text("你好")
    msg_a.uuid = "aaa"
    msg_a.parent_uuid = ""

    msg_b = ConversationMessage.assistant_text("你好！")
    msg_b.uuid = "bbb"
    msg_b.parent_uuid = "aaa"

    msg_c = ConversationMessage.user_text("帮我写代码")
    msg_c.uuid = "ccc"
    msg_c.parent_uuid = "bbb"

    msg_d = ConversationMessage.assistant_text("好的，代码写好了。")
    msg_d.uuid = "ddd"
    msg_d.parent_uuid = "ccc"

    # 用字典存储（模拟 JSONL 加载后的 Map<UUID, Message>）
    all_msgs = {m.uuid: m for m in [msg_a, msg_b, msg_c, msg_d]}

    # 从叶节点 ddd 回溯重建
    chain = build_conversation_chain(all_msgs, "ddd")
    print(f"  从叶节点 'ddd' 回溯，重建了 {len(chain)} 条消息:")
    for msg in chain:
        text = msg.blocks[0].text if msg.blocks else ""
        print(f"    {msg.uuid} ← {msg.parent_uuid or '(root)'}"
              f"  [{msg.role}] {text}")

    # --- 5. 中断检测 ---
    print("\n--- 中断检测演示 ---")

    # 场景 A：正常结束
    status = detect_interruption([
        ConversationMessage.user_text("hi"),
        ConversationMessage.assistant_text("hello!"),
    ])
    print(f"  场景A（最后是 assistant）: {status}")

    # 场景 B：工具执行中被中断
    tool_msg = ConversationMessage(
        role="tool",
        blocks=[ContentBlock(
            block_type="tool_result",
            tool_use_id="t1",
            tool_name="bash",
            output="running...",
        )],
    )
    status = detect_interruption([
        ConversationMessage.user_text("run tests"),
        ConversationMessage.assistant_text("let me run the tests"),
        tool_msg,
    ])
    print(f"  场景B（最后是 tool_result）: {status}")

    # 场景 C：用户问了但没收到回复
    status = detect_interruption([
        ConversationMessage.user_text("fix the bug"),
    ])
    print(f"  场景C（最后是 user text）: {status}")

    # --- 6. 列出所有会话 ---
    print("\n--- 列出所有会话 ---")

    sessions = storage.list_sessions()
    for s in sessions:
        print(f"  [{s['session_id']}] "
              f"首次: \"{s['first_prompt'][:30]}\" ")

    # 全景图
    print("\n" + "=" * 60)
    print("会话持久化全景图")
    print("=" * 60)
    print("""
    对话进行中
        │
        ▼
    ┌─────────────────────────────┐
    │  Session (内存中)            │
    │  messages: [msg_a, msg_b, …]│
    └─────────────────────────────┘
        │ 每条消息追加写入
        ▼
    ┌─────────────────────────────┐
    │  JSONL 文件 (磁盘)           │
    │  {"type":"user","text":…}   │ ← 第 1 行
    │  {"type":"assistant",…}     │ ← 第 2 行
    │  {"type":"user",…}          │ ← 第 3 行
    │  {"type":"custom-title",…}  │ ← 元数据
    └─────────────────────────────┘

    恢复时 (--resume)
        │
        ▼
    ┌─────────────────────────────┐
    │  逐行解析 JSONL              │
    │  - 跳过损坏行（崩溃安全）     │
    │  - 构建 UUID → Message 映射  │
    └─────────────────────────────┘
        │
        ▼
    ┌─────────────────────────────┐
    │  找到叶节点（最后一条消息）   │
    │  沿 parent_uuid 回溯至根     │
    │  反转 → 得到按时间排序的对话  │
    └─────────────────────────────┘
        │
        ▼
    ┌─────────────────────────────┐
    │  中断检测                    │
    │  最后是 assistant? → 正常    │
    │  最后是 tool_result? → 注入  │
    │    "请从断点继续" 消息       │
    └─────────────────────────────┘

    对应源码：
    - session.rs:88-91    → save_to_path() (JSON 整文件)
    - session.rs:93-96    → load_from_path()
    - session.rs:99-115   → to_json() 序列化
    - session.rs:117-135  → from_json() 反序列化

    生产级扩展（Reference EP09）：
    - JSONL 仅追加 + 100ms 合并窗口
    - 64KB 头尾窗口快速列出会话
    - Parent-UUID 链支持分支和压缩
    - 中断检测 + 自动恢复
    """)

    # 清理
    import shutil
    shutil.rmtree(tmpdir)
    print(f"  (临时文件已清理)")


if __name__ == "__main__":
    main()
