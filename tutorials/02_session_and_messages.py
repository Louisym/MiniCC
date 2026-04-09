"""
Tutorial 02: Session 与消息模型 — AI 的记忆系统
================================================

上一个教程我们用了最简单的 Message 类。但真正的 Claude Code 里，
消息模型要更复杂一些。这个教程会教你：

1. 为什么消息不只是"一段文字"
2. ContentBlock（内容块）是什么
3. Session（会话）怎么保存和恢复
4. Token 使用统计怎么追踪

对应源码：rust/crates/runtime/src/session.rs

运行方式：python tutorials/02_session_and_messages.py
"""

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Optional


# ============================================================
# 第一步：为什么消息不只是一段文字？
# ============================================================
# 想象 AI 的一条回复：
#
#   "让我帮你看看这个文件。"          ← 这是文字（text）
#   [调用工具: read_file("main.py")]  ← 这是工具调用（tool_use）
#
# 一条消息里可以同时包含文字和工具调用！
# 所以我们不能用一个简单的字符串来表示消息内容。
# 我们需要 ContentBlock（内容块）—— 一条消息由多个"块"组成。


# ============================================================
# 什么是 dataclass？
# ============================================================
# @dataclass 是 Python 的一个装饰器，帮你自动生成 __init__、__repr__ 等方法。
# 比如：
#   @dataclass
#   class Point:
#       x: int
#       y: int
# 等价于手写：
#   class Point:
#       def __init__(self, x, y):
#           self.x = x
#           self.y = y
#
# frozen=True 表示创建后不能修改（不可变的），就像 tuple 不能改一样。
# 为什么要不可变？因为消息一旦发出去就不该被偷偷改掉，这叫"数据安全"。


# ============================================================
# 第二步：定义 ContentBlock（内容块）
# ============================================================

@dataclass(frozen=True)
class TextBlock:
    """
    文字块 —— AI 说的话。

    例子：TextBlock(text="答案是 42")
    """
    text: str
    type: str = "text"  # 固定值，用于区分不同类型的块


@dataclass(frozen=True)
class ToolUseBlock:
    """
    工具调用块 —— AI 说"我要用某个工具"。

    例子：ToolUseBlock(id="tool-1", name="bash", input='{"command": "ls"}')

    参数:
        id: 每次工具调用的唯一标识（就像快递单号，用来匹配结果）
        name: 工具名称
        input: 传给工具的参数（JSON 字符串）
    """
    id: str
    name: str
    input: str
    type: str = "tool_use"


@dataclass(frozen=True)
class ToolResultBlock:
    """
    工具结果块 —— 工具执行后的返回结果。

    例子：ToolResultBlock(tool_use_id="tool-1", tool_name="bash", output="file1.py\nfile2.py", is_error=False)

    参数:
        tool_use_id: 对应哪次工具调用（和 ToolUseBlock 的 id 对应）
        tool_name: 工具名称
        output: 工具的输出结果
        is_error: 执行是否出错了
    """
    tool_use_id: str
    tool_name: str
    output: str
    is_error: bool = False
    type: str = "tool_result"


# ContentBlock 可以是以上三种之一
ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock
# 上面这行的意思是：ContentBlock 这个类型可以是 TextBlock 或 ToolUseBlock 或 ToolResultBlock
# 这叫做"联合类型"（Union Type），Python 3.10+ 支持用 | 语法


# ============================================================
# 第三步：定义 TokenUsage（token 使用统计）
# ============================================================
# 什么是 Token？
# Token 是 AI 处理文字的最小单位。粗略来说：
#   英文：1 个单词 ≈ 1 个 token
#   中文：1 个汉字 ≈ 1-2 个 token
#
# 为什么要统计 token？因为 AI API 按 token 计费！
#   - input_tokens: 你发给 AI 的内容用了多少 token（看菜单的钱）
#   - output_tokens: AI 回复了多少 token（点菜的钱）
#
# Cache 相关的字段是优化措施，暂时不用管。

@dataclass(frozen=True)
class TokenUsage:
    """Token 使用统计 —— 用来追踪花了多少"钱"。"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0  # 暂时忽略
    cache_read_input_tokens: int = 0      # 暂时忽略

    def total_tokens(self) -> int:
        """总共用了多少 token"""
        return (self.input_tokens + self.output_tokens
                + self.cache_creation_input_tokens
                + self.cache_read_input_tokens)


# ============================================================
# 第四步：定义 ConversationMessage（对话消息）
# ============================================================

@dataclass(frozen=True)
class ConversationMessage:
    """
    一条完整的对话消息。

    和 Tutorial 01 里简单的 Message 不同，这里：
    - content 不再是一个字符串，而是一个 ContentBlock 列表
    - 多了 usage 字段来追踪 token 消耗

    对应源码: session.rs:36-40
    """
    role: str                            # "user" | "assistant" | "tool" | "system"
    blocks: tuple[ContentBlock, ...]     # 内容块列表（用 tuple 因为不可变）
    usage: Optional[TokenUsage] = None   # 只有 assistant 消息才有 usage

    # 下面是几个方便创建消息的方法（工厂方法）
    # "工厂方法"就是"帮你快速创建对象的函数"，省得每次都写一堆参数

    @staticmethod
    def user_text(text: str) -> "ConversationMessage":
        """创建一条用户文字消息"""
        return ConversationMessage(
            role="user",
            blocks=(TextBlock(text=text),),
        )

    @staticmethod
    def assistant_text(text: str, usage: Optional[TokenUsage] = None) -> "ConversationMessage":
        """创建一条助手文字消息"""
        return ConversationMessage(
            role="assistant",
            blocks=(TextBlock(text=text),),
            usage=usage,
        )

    @staticmethod
    def assistant_with_tool_use(
        text: str, tool_id: str, tool_name: str, tool_input: str,
        usage: Optional[TokenUsage] = None,
    ) -> "ConversationMessage":
        """创建一条包含文字和工具调用的助手消息"""
        return ConversationMessage(
            role="assistant",
            blocks=(
                TextBlock(text=text),
                ToolUseBlock(id=tool_id, name=tool_name, input=tool_input),
            ),
            usage=usage,
        )

    @staticmethod
    def tool_result(
        tool_use_id: str, tool_name: str, output: str, is_error: bool = False,
    ) -> "ConversationMessage":
        """创建一条工具结果消息"""
        return ConversationMessage(
            role="tool",
            blocks=(ToolResultBlock(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                output=output,
                is_error=is_error,
            ),),
        )


# ============================================================
# 第五步：定义 Session（会话）
# ============================================================
# Session 就是"一整段对话"，包含所有消息。
# 它可以保存到文件（JSON），也可以从文件恢复。
# 就像游戏的"存档"和"读档"功能。

@dataclass
class Session:
    """
    对话会话 —— 包含一整段对话的所有消息。

    注意这里没有 frozen=True，因为我们需要往里面添加消息。
    对应源码: session.rs:42-46
    """
    version: int = 1
    messages: list[ConversationMessage] = field(default_factory=list)

    # ---- 序列化（保存到文件）----
    # 序列化是什么？就是把 Python 对象变成可以写入文件的格式（比如 JSON）。
    # 反序列化就是反过来，从文件读取并还原成 Python 对象。

    def to_dict(self) -> dict:
        """把 Session 变成字典（方便转 JSON）"""
        return {
            "version": self.version,
            "messages": [self._message_to_dict(msg) for msg in self.messages],
        }

    def save_to_file(self, path: str) -> None:
        """保存到 JSON 文件（存档）"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load_from_file(cls, path: str) -> "Session":
        """从 JSON 文件加载（读档）"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        session = cls(version=data["version"])
        for msg_data in data["messages"]:
            session.messages.append(cls._dict_to_message(msg_data))
        return session

    @staticmethod
    def _message_to_dict(msg: ConversationMessage) -> dict:
        result = {"role": msg.role, "blocks": []}
        for block in msg.blocks:
            if isinstance(block, TextBlock):
                result["blocks"].append({"type": "text", "text": block.text})
            elif isinstance(block, ToolUseBlock):
                result["blocks"].append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input,
                })
            elif isinstance(block, ToolResultBlock):
                result["blocks"].append({
                    "type": "tool_result", "tool_use_id": block.tool_use_id,
                    "tool_name": block.tool_name, "output": block.output,
                    "is_error": block.is_error,
                })
        if msg.usage is not None:
            result["usage"] = {
                "input_tokens": msg.usage.input_tokens,
                "output_tokens": msg.usage.output_tokens,
            }
        return result

    @staticmethod
    def _dict_to_message(data: dict) -> ConversationMessage:
        blocks = []
        for b in data["blocks"]:
            if b["type"] == "text":
                blocks.append(TextBlock(text=b["text"]))
            elif b["type"] == "tool_use":
                blocks.append(ToolUseBlock(id=b["id"], name=b["name"], input=b["input"]))
            elif b["type"] == "tool_result":
                blocks.append(ToolResultBlock(
                    tool_use_id=b["tool_use_id"], tool_name=b["tool_name"],
                    output=b["output"], is_error=b.get("is_error", False),
                ))
        usage = None
        if "usage" in data:
            usage = TokenUsage(
                input_tokens=data["usage"]["input_tokens"],
                output_tokens=data["usage"]["output_tokens"],
            )
        return ConversationMessage(role=data["role"], blocks=tuple(blocks), usage=usage)


# ============================================================
# 第六步：演示
# ============================================================

def main():
    print("=" * 60)
    print("Tutorial 02: Session 与消息模型演示")
    print("=" * 60)

    # --- 1. 构建一段完整对话 ---
    print("\n--- 构建一段对话 ---")

    session = Session()

    # 用户说话
    session.messages.append(ConversationMessage.user_text("2+2 等于几？"))
    print(f"添加消息: {session.messages[-1]}")

    # 助手回复（包含文字 + 工具调用）
    session.messages.append(ConversationMessage.assistant_with_tool_use(
        text="让我算一下。",
        tool_id="tool-1",
        tool_name="add",
        tool_input="2,2",
        usage=TokenUsage(input_tokens=20, output_tokens=6),
    ))
    print(f"添加消息: {session.messages[-1]}")

    # 工具返回结果
    session.messages.append(ConversationMessage.tool_result(
        tool_use_id="tool-1",
        tool_name="add",
        output="4",
    ))
    print(f"添加消息: {session.messages[-1]}")

    # 助手最终回答
    session.messages.append(ConversationMessage.assistant_text(
        text="2 + 2 的答案是 4。",
        usage=TokenUsage(input_tokens=30, output_tokens=8),
    ))
    print(f"添加消息: {session.messages[-1]}")

    # --- 2. 查看消息内部结构 ---
    print("\n--- 查看第二条消息的内部结构（助手的工具调用）---")
    msg = session.messages[1]
    print(f"  角色: {msg.role}")
    print(f"  内容块数量: {len(msg.blocks)}")
    for i, block in enumerate(msg.blocks):
        print(f"  块 {i}: type={block.type}, 内容={block}")
    print(f"  Token 使用: {msg.usage}")

    # --- 3. 保存和恢复 ---
    print("\n--- 保存和恢复 Session ---")
    # 创建临时文件来演示
    tmp_path = os.path.join(tempfile.gettempdir(), "tutorial_session.json")
    session.save_to_file(tmp_path)
    print(f"  已保存到: {tmp_path}")

    # 读回来
    restored = Session.load_from_file(tmp_path)
    print(f"  已恢复: {len(restored.messages)} 条消息")
    print(f"  验证一致性: {len(session.messages) == len(restored.messages)}")

    # 看看保存的 JSON 长什么样
    print("\n--- 保存的 JSON 内容（前 500 字符）---")
    with open(tmp_path, "r") as f:
        content = f.read()
    print(content[:500])

    # 清理
    os.remove(tmp_path)

    # --- 4. Token 使用统计 ---
    print("\n--- Token 使用统计 ---")
    total_input = 0
    total_output = 0
    for msg in session.messages:
        if msg.usage:
            total_input += msg.usage.input_tokens
            total_output += msg.usage.output_tokens
    print(f"  总输入 tokens: {total_input}")
    print(f"  总输出 tokens: {total_output}")
    print(f"  总计: {total_input + total_output}")

    # 解说
    print("\n" + "=" * 60)
    print("关键理解要点：")
    print("=" * 60)
    print("""
    1. 一条消息由多个 ContentBlock 组成（不只是一个字符串）
       - TextBlock: 纯文字
       - ToolUseBlock: AI 请求调用工具（包含工具名和参数）
       - ToolResultBlock: 工具的执行结果

    2. ToolUseBlock 的 id 和 ToolResultBlock 的 tool_use_id 必须匹配
       （就像寄快递时的"单号"，用来对应请求和结果）

    3. Session 可以序列化（变成 JSON 保存到文件）和反序列化（从文件恢复）
       这样关掉程序后还能继续之前的对话

    4. TokenUsage 追踪每次 API 调用的开销，用于计费

    5. frozen=True 的 dataclass 创建后不能修改，保证数据安全
       （Session 本身没有 frozen，因为需要往里加消息）

    对应 Claude Code 源码:
    - session.rs:9-16   →  MessageRole 枚举
    - session.rs:18-33  →  ContentBlock 枚举
    - session.rs:36-40  →  ConversationMessage 结构
    - session.rs:42-46  →  Session 结构
    - session.rs:88-96  →  save_to_path / load_from_path
    """)


if __name__ == "__main__":
    main()
