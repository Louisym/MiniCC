"""
compact.py — Token 估算 + 双触发压缩 + 结构化摘要提取

忠实还原 Claude Code 的自动压缩系统。
源码对照: rust/crates/runtime/src/compact.rs (486 行)

核心工程要点:
1. Token 估算: len(text) // 4 + 1, 无需 tokenizer (compact.rs:326-338)
2. 双触发: 消息数 > N 且 tokens >= 阈值 (compact.rs:32-35)
3. Preserve-recent-N: 保留最近 N 条原文 (compact.rs:85-90)
4. 结构化摘要: 代码提取关键信息，不依赖 AI (compact.rs:113-198)
"""

import re
from typing import Optional

from pydantic import BaseModel, Field

from mini_claude_code.models import Message, ContentBlock, TextContentBlock, ToolContentBlock, ToolResultContentBlock


# ============================================================
# CompactionConfig
# 源码: compact.rs:4-16
# ============================================================

class CompactionConfig(BaseModel):
    preserve_recent_messages: int = 4
    max_estimated_tokens: int = 200_000


# ============================================================
# CompactionResult
# 源码: compact.rs:18-24
# ============================================================

class CompactionResult(BaseModel):
    summary: str = ""
    formatted_summary: str = ""
    compacted_messages: list[Message] = Field(default_factory=list)
    removed_count: int = 0


# ============================================================
# Token 估算
# 源码: compact.rs:326-338
#
# 启发式: 1 token ≈ 4 字符。每个 block 至少 1 token。
# 不依赖任何 tokenizer 库 — CC 故意选择这个近似值来避免依赖。
# ============================================================

def _estimate_block_tokens(block: ContentBlock) -> int:
    """源码: compact.rs:330-337"""
    if isinstance(block, TextContentBlock):
        return len(block.text) // 4 + 1
    if isinstance(block, ToolContentBlock):
        return (len(block.name) + len(block.input)) // 4 + 1
    if isinstance(block, ToolResultContentBlock):
        return (len(block.name) + len(block.output)) // 4 + 1
    return 1


def estimate_message_tokens(msg: Message) -> int:
    """源码: compact.rs:326-338"""
    return sum(_estimate_block_tokens(b) for b in msg.content)


def estimate_session_tokens(messages: list[Message]) -> int:
    """源码: compact.rs:27-29"""
    return sum(estimate_message_tokens(m) for m in messages)


# ============================================================
# 双触发判断
# 源码: compact.rs:32-35
#
# 两个条件必须同时满足:
#   1. 消息数量 > preserve_recent_messages
#   2. 估算 token 数 >= max_estimated_tokens
#
# 为什么是 AND？因为:
# - 消息少但 token 多: 可能是单条超长消息，压缩帮助不大
# - 消息多但 token 少: 不需要压缩
# ============================================================

def should_compact(messages: list[Message], config: CompactionConfig) -> bool:
    """源码: compact.rs:32-35"""
    return (
        len(messages) > config.preserve_recent_messages
        and estimate_session_tokens(messages) >= config.max_estimated_tokens
    )


# ============================================================
# 辅助函数
# ============================================================

def truncate_summary(text: str, max_chars: int = 160) -> str:
    """源码: compact.rs:317-324"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


_INTERESTING_EXTENSIONS = {"py", "rs", "ts", "tsx", "js", "json", "md", "toml", "yaml", "yml"}


def extract_file_candidates(text: str) -> list[str]:
    """启发式提取文件路径。源码: compact.rs:301-315

    规则: token 包含 '/' 且有已知扩展名。
    """
    candidates: list[str] = []
    for token in text.split():
        cleaned = token.strip(",.;:)(\"'`")
        if "/" not in cleaned:
            continue
        ext = cleaned.rsplit(".", 1)[-1].lower() if "." in cleaned else ""
        if ext in _INTERESTING_EXTENSIONS:
            candidates.append(cleaned)
    return candidates


def extract_tag_block(text: str, tag: str) -> Optional[str]:
    """提取 XML 标签内容。源码: compact.rs:340-346"""
    start = f"<{tag}>"
    end = f"</{tag}>"
    si = text.find(start)
    if si == -1:
        return None
    si += len(start)
    ei = text.find(end, si)
    if ei == -1:
        return None
    return text[si:ei]


def strip_tag_block(text: str, tag: str) -> str:
    """删除整个 XML 标签块。源码: compact.rs:348-360"""
    start = f"<{tag}>"
    end = f"</{tag}>"
    si = text.find(start)
    ei = text.find(end)
    if si == -1 or ei == -1:
        return text
    return text[:si] + text[ei + len(end):]


def _collapse_blank_lines(text: str) -> str:
    """源码: compact.rs:362-375"""
    lines: list[str] = []
    prev_blank = False
    for line in text.splitlines():
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        lines.append(line)
        prev_blank = is_blank
    return "\n".join(lines)


def _first_text(msg: Message) -> Optional[str]:
    """取消息第一个非空文本 block。源码: compact.rs:281-288"""
    for block in msg.content:
        if isinstance(block, TextContentBlock) and block.text.strip():
            return block.text
    return None


# ============================================================
# 结构化摘要
# 源码: compact.rs:113-198
#
# 关键: 这是代码做的提取，不是让 AI 总结。
# 提取 7 类信息: 统计、工具名、最近请求、待办、关键文件、当前工作、时间线。
# ============================================================

def summarize_messages(messages: list[Message]) -> str:
    """源码: compact.rs:113-198"""
    user_count = sum(1 for m in messages if m.role == "user")
    assistant_count = sum(1 for m in messages if m.role == "assistant")
    tool_count = sum(1 for m in messages if m.role == "tool")

    # 工具名: 排序 + 去重 — 源码: compact.rs:127-137
    tool_names: set[str] = set()
    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolContentBlock):
                tool_names.add(block.name)
            elif isinstance(block, ToolResultContentBlock):
                tool_names.add(block.name)
    sorted_tools = sorted(tool_names)

    lines = [
        "<summary>",
        "Conversation summary:",
        f"- Scope: {len(messages)} earlier messages compacted "
        f"(user={user_count}, assistant={assistant_count}, tool={tool_count}).",
    ]

    if sorted_tools:
        lines.append(f"- Tools mentioned: {', '.join(sorted_tools)}.")

    # 最近 3 条用户请求 — 源码: compact.rs:155-163
    user_requests = _collect_recent_role_summaries(messages, "user", 3)
    if user_requests:
        lines.append("- Recent user requests:")
        lines.extend(f"  - {r}" for r in user_requests)

    # 待办事项 — 源码: compact.rs:235-254
    pending = _infer_pending_work(messages)
    if pending:
        lines.append("- Pending work:")
        lines.extend(f"  - {p}" for p in pending)

    # 关键文件 — 源码: compact.rs:256-270
    key_files = _collect_key_files(messages)
    if key_files:
        lines.append(f"- Key files referenced: {', '.join(key_files)}.")

    # 当前工作 — 源码: compact.rs:272-279
    current = _infer_current_work(messages)
    if current:
        lines.append(f"- Current work: {current}")

    # 时间线 — 源码: compact.rs:180-195
    lines.append("- Key timeline:")
    for msg in messages:
        block_summaries = []
        for block in msg.content:
            if isinstance(block, TextContentBlock):
                block_summaries.append(truncate_summary(block.text, 160))
            elif isinstance(block, ToolContentBlock):
                block_summaries.append(truncate_summary(f"tool_use {block.name}({block.input})", 160))
            elif isinstance(block, ToolResultContentBlock):
                err = "error " if block.is_error else ""
                block_summaries.append(truncate_summary(f"tool_result {block.name}: {err}{block.output}", 160))
        lines.append(f"  - {msg.role}: {' | '.join(block_summaries)}")

    lines.append("</summary>")
    return "\n".join(lines)


def _collect_recent_role_summaries(
    messages: list[Message], role: str, limit: int
) -> list[str]:
    """源码: compact.rs:217-233

    反向遍历 → 取最近 N 条 → 再反转回时间序。
    """
    results: list[str] = []
    for msg in reversed(messages):
        if msg.role != role:
            continue
        text = _first_text(msg)
        if text:
            results.append(truncate_summary(text, 160))
        if len(results) >= limit:
            break
    results.reverse()
    return results


def _infer_pending_work(messages: list[Message]) -> list[str]:
    """源码: compact.rs:235-254"""
    keywords = ("todo", "next", "pending", "follow up", "remaining")
    results: list[str] = []
    for msg in reversed(messages):
        text = _first_text(msg)
        if text and any(kw in text.lower() for kw in keywords):
            results.append(truncate_summary(text, 160))
        if len(results) >= 3:
            break
    results.reverse()
    return results


def _collect_key_files(messages: list[Message]) -> list[str]:
    """源码: compact.rs:256-270"""
    files: set[str] = set()
    for msg in messages:
        for block in msg.content:
            if isinstance(block, TextContentBlock):
                files.update(extract_file_candidates(block.text))
            elif isinstance(block, ToolContentBlock):
                files.update(extract_file_candidates(block.input))
            elif isinstance(block, ToolResultContentBlock):
                files.update(extract_file_candidates(block.output))
    return sorted(files)[:8]


def _infer_current_work(messages: list[Message]) -> Optional[str]:
    """源码: compact.rs:272-279"""
    for msg in reversed(messages):
        text = _first_text(msg)
        if text and text.strip():
            return truncate_summary(text, 200)
    return None


# ============================================================
# 格式化摘要
# 源码: compact.rs:38-50
#
# 1. 删除 <analysis> 块 (AI 推理过程)
# 2. 提取 <summary> 块内容，替换为 "Summary:\n" 前缀
# ============================================================

def format_compact_summary(summary: str) -> str:
    """源码: compact.rs:38-50"""
    without_analysis = strip_tag_block(summary, "analysis")
    content = extract_tag_block(without_analysis, "summary")
    if content is not None:
        formatted = without_analysis.replace(
            f"<summary>{content}</summary>",
            f"Summary:\n{content.strip()}",
        )
    else:
        formatted = without_analysis
    return _collapse_blank_lines(formatted).strip()


# ============================================================
# compact_session — 执行压缩
# 源码: compact.rs:75-111
# ============================================================

def compact_session(
    messages: list[Message],
    config: CompactionConfig,
) -> CompactionResult:
    """源码: compact.rs:75-111

    流程:
    1. 检查是否需要压缩 (should_compact)
    2. 分割: messages[:-N] 压缩, messages[-N:] 保留
    3. 旧消息 → 结构化摘要
    4. 新 session = [system 摘要] + [保留消息]
    """
    if not should_compact(messages, config):
        return CompactionResult(
            compacted_messages=list(messages),
            removed_count=0,
        )

    keep_from = max(0, len(messages) - config.preserve_recent_messages)
    removed = messages[:keep_from]
    preserved = messages[keep_from:]

    summary = summarize_messages(removed)
    formatted_summary = format_compact_summary(summary)

    continuation_text = (
        "This session is being continued from a previous conversation "
        "that ran out of context. The summary below covers the earlier portion.\n\n"
        f"{formatted_summary}"
    )
    if preserved:
        continuation_text += "\n\nRecent messages are preserved verbatim."
    continuation_text += (
        "\nContinue the conversation from where it left off without "
        "asking the user any further questions."
    )

    # 摘要作为 system 角色的消息 — 源码: compact.rs:95-99
    system_msg = Message(
        role="user",  # 我们的 models.py 没有 system role，用 user 代替
        content=[TextContentBlock(text=continuation_text)],
    )

    compacted = [system_msg] + list(preserved)

    return CompactionResult(
        summary=summary,
        formatted_summary=formatted_summary,
        compacted_messages=compacted,
        removed_count=len(removed),
    )
