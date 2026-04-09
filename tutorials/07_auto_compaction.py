"""
Tutorial 07: Auto Compaction — AI 的"记忆压缩"
=================================================

问题：AI 的"脑容量"是有限的
---------------------------
AI 每次回复时，需要看到整个对话历史。但 AI 有一个叫"上下文窗口"的限制
（大约 200k tokens），超过了就放不下了。

想象你和朋友连续聊了 10 个小时的微信。如果让你从头看所有聊天记录再回复，
你的"脑容量"也不够用。怎么办？

答案是"压缩"：把旧的聊天记录总结成几句话，只保留最近的几条消息。
这就是 Auto Compaction（自动压缩）。

比如：
  旧记录（100 条消息）→ 压缩成 1 条摘要："之前我们讨论了 X 功能的实现，
                        修改了 file_a.py 和 file_b.py，还剩下测试没写"
  最近的几条消息 → 原样保留

本教程会教你：
1. 什么时候触发压缩
2. 压缩算法怎么工作
3. 压缩后的消息长什么样
4. 这个模块在 Agentic Loop 里什么位置被调用

对应源码：rust/crates/runtime/src/compact.rs

运行方式：python tutorials/07_auto_compaction.py
"""

from dataclasses import dataclass, field
from typing import Optional
import re


# ============================================================
# 复用消息模型（和 Tutorial 05 一样，精简版）
# ============================================================

@dataclass(frozen=True)
class TextBlock:
    text: str
    type: str = "text"

@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: str
    type: str = "tool_use"

@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    tool_name: str
    output: str
    is_error: bool = False
    type: str = "tool_result"

ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock

@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

@dataclass(frozen=True)
class ConversationMessage:
    role: str
    blocks: tuple[ContentBlock, ...]
    usage: Optional[TokenUsage] = None

    @staticmethod
    def user_text(text: str) -> "ConversationMessage":
        return ConversationMessage(role="user", blocks=(TextBlock(text=text),))

    @staticmethod
    def assistant_text(text: str) -> "ConversationMessage":
        return ConversationMessage(role="assistant", blocks=(TextBlock(text=text),))

    @staticmethod
    def system_text(text: str) -> "ConversationMessage":
        return ConversationMessage(role="system", blocks=(TextBlock(text=text),))

    @staticmethod
    def tool_result(tool_use_id: str, tool_name: str, output: str, is_error: bool = False) -> "ConversationMessage":
        return ConversationMessage(
            role="tool",
            blocks=(ToolResultBlock(tool_use_id=tool_use_id, tool_name=tool_name, output=output, is_error=is_error),),
        )

@dataclass
class Session:
    version: int = 1
    messages: list[ConversationMessage] = field(default_factory=list)


# ============================================================
# 第一步：估算 Token 数量
# ============================================================
# 我们需要知道当前对话用了多少 token，才能判断是否需要压缩。
# 精确计算 token 需要专门的分词器（tokenizer），
# 这里用一个粗略估算：大约每 4 个字符 = 1 个 token。

def estimate_message_tokens(msg: ConversationMessage) -> int:
    """
    粗略估算一条消息占多少 token。

    对应源码: compact.rs:326-338 (estimate_message_tokens)
    """
    total = 0
    for block in msg.blocks:
        if isinstance(block, TextBlock):
            total += len(block.text) // 4 + 1
        elif isinstance(block, ToolUseBlock):
            total += (len(block.name) + len(block.input)) // 4 + 1
        elif isinstance(block, ToolResultBlock):
            total += (len(block.tool_name) + len(block.output)) // 4 + 1
    return total


def estimate_session_tokens(session: Session) -> int:
    """估算整个对话的 token 数"""
    return sum(estimate_message_tokens(msg) for msg in session.messages)


# ============================================================
# 第二步：判断是否需要压缩
# ============================================================

@dataclass(frozen=True)
class CompactionConfig:
    """
    压缩配置。

    preserve_recent_messages: 保留最近几条消息（不压缩它们）
    max_estimated_tokens: 超过多少 token 就触发压缩

    对应源码: compact.rs:4-16
    """
    preserve_recent_messages: int = 4     # 默认保留最近 4 条
    max_estimated_tokens: int = 10_000    # 默认 10k token 就压缩


def should_compact(session: Session, config: CompactionConfig) -> bool:
    """
    判断是否需要压缩。

    两个条件都满足时才压缩：
    1. 消息数量 > 保留数量（否则没东西可压缩）
    2. 预估 token 数 >= 阈值（还没到限制就不压缩）

    对应源码: compact.rs:32-35
    """
    has_enough_messages = len(session.messages) > config.preserve_recent_messages
    exceeds_token_limit = estimate_session_tokens(session) >= config.max_estimated_tokens
    return has_enough_messages and exceeds_token_limit


# ============================================================
# 第三步：生成摘要
# ============================================================
# 压缩的核心：把旧消息变成一段"摘要"。
# 摘要包含哪些关键信息？

def summarize_messages(messages: list[ConversationMessage]) -> str:
    """
    将一组消息压缩成摘要。

    摘要包含：
    - 消息统计（几条 user/assistant/tool 消息）
    - 使用了哪些工具
    - 最近的用户请求
    - 待完成的工作
    - 涉及的关键文件
    - 时间线概要

    对应源码: compact.rs:113-198
    """
    # 1. 统计各角色的消息数
    user_count = sum(1 for m in messages if m.role == "user")
    assistant_count = sum(1 for m in messages if m.role == "assistant")
    tool_count = sum(1 for m in messages if m.role == "tool")

    # 2. 收集使用过的工具名
    tool_names = set()
    for msg in messages:
        for block in msg.blocks:
            if isinstance(block, ToolUseBlock):
                tool_names.add(block.name)
            elif isinstance(block, ToolResultBlock):
                tool_names.add(block.tool_name)

    # 3. 收集最近的用户请求
    recent_requests = []
    for msg in reversed(messages):
        if msg.role == "user":
            for block in msg.blocks:
                if isinstance(block, TextBlock) and block.text.strip():
                    text = block.text[:160] + "..." if len(block.text) > 160 else block.text
                    recent_requests.append(text)
                    if len(recent_requests) >= 3:
                        break
        if len(recent_requests) >= 3:
            break
    recent_requests.reverse()

    # 4. 检测待完成的工作（含"todo"/"next"等关键词的消息）
    pending_work = []
    for msg in reversed(messages):
        for block in msg.blocks:
            if isinstance(block, TextBlock):
                lower = block.text.lower()
                if any(kw in lower for kw in ["todo", "next", "pending", "remaining"]):
                    text = block.text[:160] + "..." if len(block.text) > 160 else block.text
                    pending_work.append(text)
        if len(pending_work) >= 3:
            break
    pending_work.reverse()

    # 5. 提取关键文件路径
    key_files = set()
    for msg in messages:
        for block in msg.blocks:
            content = ""
            if isinstance(block, TextBlock):
                content = block.text
            elif isinstance(block, ToolUseBlock):
                content = block.input
            elif isinstance(block, ToolResultBlock):
                content = block.output
            # 简单的文件路径提取：包含 / 且有常见扩展名
            for token in content.split():
                token = token.strip(",:;()\"'`")
                if "/" in token and any(token.endswith(ext) for ext in [".py", ".rs", ".ts", ".js", ".json", ".md"]):
                    key_files.add(token)

    # 6. 组装摘要
    lines = [
        "<summary>",
        "Conversation summary:",
        f"- Scope: {len(messages)} earlier messages compacted (user={user_count}, assistant={assistant_count}, tool={tool_count}).",
    ]

    if tool_names:
        lines.append(f"- Tools mentioned: {', '.join(sorted(tool_names))}.")

    if recent_requests:
        lines.append("- Recent user requests:")
        for req in recent_requests:
            lines.append(f"  - {req}")

    if pending_work:
        lines.append("- Pending work:")
        for item in pending_work:
            lines.append(f"  - {item}")

    if key_files:
        lines.append(f"- Key files referenced: {', '.join(sorted(key_files)[:8])}.")

    # 7. 时间线（每条消息的简短描述）
    lines.append("- Key timeline:")
    for msg in messages:
        role = msg.role
        parts = []
        for block in msg.blocks:
            if isinstance(block, TextBlock):
                text = block.text[:80].replace("\n", " ")
                parts.append(text)
            elif isinstance(block, ToolUseBlock):
                parts.append(f"tool_use {block.name}({block.input[:40]})")
            elif isinstance(block, ToolResultBlock):
                status = "error " if block.is_error else ""
                parts.append(f"tool_result {block.tool_name}: {status}{block.output[:40]}")
        lines.append(f"  - {role}: {' | '.join(parts)}")

    lines.append("</summary>")
    return "\n".join(lines)


# ============================================================
# 第四步：执行压缩
# ============================================================

@dataclass
class CompactionResult:
    """压缩结果"""
    summary: str                      # 摘要文本
    compacted_session: Session        # 压缩后的新 Session
    removed_message_count: int        # 被压缩掉了多少条消息


def compact_session(session: Session, config: CompactionConfig) -> CompactionResult:
    """
    执行压缩。

    算法很简单：
    1. 判断是否需要压缩 → 不需要就直接返回
    2. 把消息分成两组：旧的（要压缩的）和新的（要保留的）
    3. 旧消息 → 生成摘要
    4. 新 Session = [摘要消息] + [保留的消息]

    对应源码: compact.rs:75-111
    """
    if not should_compact(session, config):
        return CompactionResult(
            summary="",
            compacted_session=session,
            removed_message_count=0,
        )

    # 分割点：保留最后 N 条消息
    keep_from = max(0, len(session.messages) - config.preserve_recent_messages)
    removed_messages = session.messages[:keep_from]
    preserved_messages = session.messages[keep_from:]

    # 生成摘要
    summary = summarize_messages(removed_messages)

    # 构造"续接消息"—— 告诉 AI "之前的对话被压缩了，以下是摘要"
    continuation = (
        "This session is being continued from a previous conversation "
        "that ran out of context. The summary below covers the earlier "
        "portion of the conversation.\n\n"
        f"{summary}\n\n"
        "Recent messages are preserved verbatim.\n"
        "Continue the conversation from where it left off without asking "
        "the user any further questions."
    )

    # 新的消息列表：[系统摘要] + [保留的消息]
    new_messages = [ConversationMessage.system_text(continuation)]
    new_messages.extend(preserved_messages)

    return CompactionResult(
        summary=summary,
        compacted_session=Session(version=session.version, messages=new_messages),
        removed_message_count=len(removed_messages),
    )


# ============================================================
# 第五步：Auto Compaction（在 Agentic Loop 里自动触发）
# ============================================================
# 在 conversation.rs 的 run_turn() 末尾，有一个 maybe_auto_compact()。
# 它在每个对话轮次结束后检查：累计 input tokens 是否超过阈值？
# 超过了就自动压缩。

DEFAULT_AUTO_COMPACTION_THRESHOLD = 200_000  # 200k tokens


def maybe_auto_compact(
    session: Session,
    cumulative_input_tokens: int,
    threshold: int = DEFAULT_AUTO_COMPACTION_THRESHOLD,
) -> Optional[CompactionResult]:
    """
    检查是否需要自动压缩。如果需要，执行压缩。

    对应源码: conversation.rs:310-333 (maybe_auto_compact)
    """
    if cumulative_input_tokens < threshold:
        return None  # 还没到限制，不需要压缩

    result = compact_session(session, CompactionConfig(
        preserve_recent_messages=4,
        max_estimated_tokens=0,  # 强制压缩（0 表示任何大小都要压缩）
    ))

    if result.removed_message_count == 0:
        return None  # 没有可压缩的消息

    return result


# ============================================================
# 第六步：演示
# ============================================================

def main():
    print("=" * 60)
    print("Tutorial 07: Auto Compaction 自动压缩演示")
    print("=" * 60)

    # --- 构建一个包含很多消息的 session ---
    session = Session()

    # 模拟一段长对话
    conversations = [
        ("user", "帮我创建一个 Flask Web 应用"),
        ("assistant", "好的，让我先创建项目结构。我会用 bash 来创建目录。"),
        ("user", "然后在 src/app.py 里写一个简单的 Hello World"),
        ("assistant", "好的，我来创建 src/app.py 文件。TODO: 还需要添加路由测试。"),
        ("user", "帮我添加一个 /api/users 的 REST 接口"),
        ("assistant", "我来修改 src/app.py，添加用户接口。Next: 需要写单元测试。"),
        ("user", "在 tests/test_app.py 里写测试"),
        ("assistant", "测试已经写好了。所有 3 个测试都通过了。"),
        ("user", "帮我添加数据库连接"),
        ("assistant", "我来配置 SQLAlchemy 数据库连接。Remaining: 迁移脚本还没写。"),
        ("user", "看看当前的 git 状态"),
        ("assistant", "当前有 5 个文件被修改，还没提交。"),
    ]

    for role, text in conversations:
        if role == "user":
            session.messages.append(ConversationMessage.user_text(text))
        else:
            session.messages.append(ConversationMessage.assistant_text(text))

    # 加一些工具使用记录
    session.messages.insert(2, ConversationMessage(
        role="assistant",
        blocks=(
            TextBlock(text="让我创建目录。"),
            ToolUseBlock(id="t1", name="bash", input='{"command": "mkdir -p src tests"}'),
        ),
    ))
    session.messages.insert(3, ConversationMessage.tool_result("t1", "bash", "(directory created)"))

    print(f"\n对话总消息数: {len(session.messages)}")
    print(f"预估 token 数: {estimate_session_tokens(session)}")

    # --- 1. 测试是否需要压缩（token 阈值设得低一点方便演示）---
    config = CompactionConfig(preserve_recent_messages=4, max_estimated_tokens=50)
    print(f"\n需要压缩吗 (阈值={config.max_estimated_tokens} tokens)? {should_compact(session, config)}")

    # --- 2. 执行压缩 ---
    result = compact_session(session, config)

    print(f"\n--- 压缩结果 ---")
    print(f"  被压缩掉的消息数: {result.removed_message_count}")
    print(f"  压缩前消息数: {len(session.messages)}")
    print(f"  压缩后消息数: {len(result.compacted_session.messages)}")
    print(f"  压缩前 token 数: {estimate_session_tokens(session)}")
    print(f"  压缩后 token 数: {estimate_session_tokens(result.compacted_session)}")

    # --- 3. 看看摘要长什么样 ---
    print(f"\n--- 生成的摘要 ---")
    print(result.summary)

    # --- 4. 看看压缩后的 session ---
    print(f"\n--- 压缩后的消息列表 ---")
    for i, msg in enumerate(result.compacted_session.messages):
        role = msg.role.upper()
        for block in msg.blocks:
            if isinstance(block, TextBlock):
                preview = block.text.replace("\n", " ")[:80]
                print(f"  [{i}] {role}: {preview}...")
            elif isinstance(block, ToolResultBlock):
                print(f"  [{i}] {role}: [tool_result {block.tool_name}]")

    # --- 5. 模拟自动压缩触发 ---
    print(f"\n--- 自动压缩测试 ---")
    print(f"  累计 input tokens = 150000, 阈值 = 200000")
    auto_result = maybe_auto_compact(session, cumulative_input_tokens=150_000)
    print(f"  触发了吗? {'是' if auto_result else '否'}")

    print(f"\n  累计 input tokens = 250000, 阈值 = 200000")
    auto_result = maybe_auto_compact(session, cumulative_input_tokens=250_000)
    print(f"  触发了吗? {'是' if auto_result else '否'}")
    if auto_result:
        print(f"  压缩掉了 {auto_result.removed_message_count} 条消息")

    # 解说
    print("\n" + "=" * 60)
    print("关键理解要点：")
    print("=" * 60)
    print("""
    1. 为什么需要压缩？
       AI 的上下文窗口有限（约 200k tokens）。对话太长就放不下了。
       压缩 = 把旧消息变成摘要 + 保留最近的消息

    2. 压缩触发条件：
       - 消息数量 > 保留数量 （有东西可以压缩）
       - 且 预估 token 数 >= 阈值 （确实需要压缩）

    3. 压缩算法：
       [旧消息1, 旧消息2, ..., 旧消息N, 新消息1, 新消息2, 新消息3, 新消息4]
                         ↓                              ↓
                    生成摘要（system 消息）          原样保留
                         ↓                              ↓
       [摘要消息, 新消息1, 新消息2, 新消息3, 新消息4]

    4. 摘要包含的信息：
       - 消息统计、使用的工具、用户请求、待完成工作、关键文件、时间线

    5. 在 Agentic Loop 里的位置：
       run_turn() 最后一步调用 maybe_auto_compact()
       → 检查累计 input tokens 是否超过 200k
       → 超过了就自动压缩，替换 session

    对应 Claude Code 源码:
    - compact.rs:4-16    →  CompactionConfig 配置
    - compact.rs:27-35   →  estimate_session_tokens / should_compact
    - compact.rs:75-111  →  compact_session 核心压缩函数
    - compact.rs:113-198 →  summarize_messages 摘要生成
    - conversation.rs:310-333 →  maybe_auto_compact 自动触发
    """)


if __name__ == "__main__":
    main()
