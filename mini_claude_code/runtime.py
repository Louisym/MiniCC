"""
runtime.py — 对话运行时: Agentic Loop + 泛型双接口 + 自动压缩

忠实还原 Claude Code 的 ConversationRuntime。
源码对照: rust/crates/runtime/src/conversation.rs (973 行)

核心工程要点:
1. Harness = Body, LLM = Brain: Runtime 不做任何"思考"，只感知/执行/约束
2. 泛型双接口: ApiClient + ToolExecutor 通过 Protocol 注入 (conversation.rs:100-110)
3. 事件→消息重建: build_assistant_message 把流式事件攒成结构化消息 (conversation.rs:353-390)
4. flush 模式: TextDelta 累积，遇到 ToolUse 时 flush (conversation.rs:392-398)
5. Hook 反馈合并: Pre/Post hook 消息追加到工具输出 (conversation.rs:408-424)
6. 自动压缩: 基于累计 input_tokens 阈值触发 (conversation.rs:310-333)
7. TurnSummary: 结构化返回值，不只是文本 (conversation.rs:87-93)
"""

from typing import Optional, Protocol

from pydantic import BaseModel, Field

from mini_claude_code.api_client import (
    AssistantEvent,
    MessageStopEvent,
    TextDeltaEvent,
    ToolUseEvent,
)
from mini_claude_code.compact import (
    CompactionConfig,
    compact_session,
)
from mini_claude_code.hooks import HookResult, HookRunner
from mini_claude_code.models import (
    Message,
    Session,
    TextContentBlock,
    ToolContentBlock,
    ToolResultContentBlock,
)
from mini_claude_code.permissions import PermissionPolicy, PermissionPrompter


# ============================================================
# TokenUsage — Token 用量
# 源码: usage.rs:28-34
#
# 四维 token 统计: input, output, cache_creation, cache_read。
# CC 用这四个维度计费，不同维度价格不同 (e.g. cache_read 很便宜)。
# ============================================================

class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def total_tokens(self) -> int:
        """源码: usage.rs:81-86"""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


# ============================================================
# UsageTracker — 跨 turn 累计 token 用量
# 源码: usage.rs:162-209
#
# 关键: record() 是累加 (+=)，不是替换 (=)。
# latest_turn 记录最近一次 API 调用的用量。
# cumulative 记录整个会话的总用量。
#
# from_session: 从恢复的 session 重建 tracker —
# 遍历所有 assistant 消息，提取 usage 累加。
# ============================================================

class UsageTracker:
    def __init__(self) -> None:
        self._latest_turn = TokenUsage()
        self._cumulative = TokenUsage()
        self._turns: int = 0

    def record(self, usage: TokenUsage) -> None:
        """源码: usage.rs:186-193"""
        self._latest_turn = usage
        self._cumulative = TokenUsage(
            input_tokens=self._cumulative.input_tokens + usage.input_tokens,
            output_tokens=self._cumulative.output_tokens + usage.output_tokens,
            cache_creation_input_tokens=(
                self._cumulative.cache_creation_input_tokens
                + usage.cache_creation_input_tokens
            ),
            cache_read_input_tokens=(
                self._cumulative.cache_read_input_tokens
                + usage.cache_read_input_tokens
            ),
        )
        self._turns += 1

    def current_turn_usage(self) -> TokenUsage:
        """源码: usage.rs:196-198"""
        return self._latest_turn

    def cumulative_usage(self) -> TokenUsage:
        """源码: usage.rs:201-203"""
        return self._cumulative

    def turns(self) -> int:
        """源码: usage.rs:206-208"""
        return self._turns


# ============================================================
# ToolError — 工具执行错误
# 源码: conversation.rs:42-63
# ============================================================

class ToolError(Exception):
    pass


# ============================================================
# ToolExecutor — 工具执行接口
# 源码: conversation.rs:38-40
#
# CC 用 Rust trait。Python 用 Protocol (结构化子类型)。
# 和 ApiClient 一样，这让我们可以注入 mock 做测试。
#
# execute 成功 → 返回 str
# execute 失败 → 抛 ToolError
# ============================================================

class ToolExecutor(Protocol):
    def execute(self, tool_name: str, input: str) -> str: ...


# ============================================================
# build_assistant_message — 事件流 → 结构化消息
# 源码: conversation.rs:353-390
#
# 关键工程模式: **flush 缓冲**
#
# API 返回流式事件 (TextDelta, ToolUse, MessageStop)。
# TextDelta 可能很多个，需要累积到缓冲区。
# 遇到 ToolUse 时:
#   1. 先 flush 缓冲区 (把累积的文本变成一个 TextContentBlock)
#   2. 再添加 ToolContentBlock
#
# 为什么不直接每个 TextDelta 一个 block?
# 因为 API 是按 chunk 推送的，可能把一句话拆成很多 delta。
# 合并后才是一个语义完整的文本段。
# ============================================================

class _UsageEvent(BaseModel):
    """内部: 用来在 api_client 扩展 Usage 事件。
    现有 api_client.py 没有 Usage 事件类型，
    我们在 build_assistant_message 中兼容处理。
    """
    type: str = "usage"
    usage: TokenUsage


def build_assistant_message(
    events: list[AssistantEvent],
) -> tuple[Message, Optional[TokenUsage]]:
    """源码: conversation.rs:353-390

    返回: (assistant_message, optional_usage)
    """
    text_buffer: str = ""
    blocks: list = []
    finished: bool = False
    usage: Optional[TokenUsage] = None

    for event in events:
        if isinstance(event, TextDeltaEvent):
            text_buffer += event.text

        elif isinstance(event, ToolUseEvent):
            # flush 文本缓冲 — 源码: conversation.rs:392-398
            if text_buffer:
                blocks.append(TextContentBlock(text=text_buffer))
                text_buffer = ""
            blocks.append(ToolContentBlock(
                id=event.id,
                name=event.name,
                input=event.input,
            ))

        elif isinstance(event, MessageStopEvent):
            finished = True

    # 最后 flush 残留文本
    if text_buffer:
        blocks.append(TextContentBlock(text=text_buffer))

    if not finished:
        raise RuntimeError(
            "assistant stream ended without a message stop event"
        )
    if not blocks:
        raise RuntimeError("assistant stream produced no content")

    message = Message(role="assistant", content=blocks)
    return message, usage


# ============================================================
# merge_hook_feedback — 合并 hook 反馈到工具输出
# 源码: conversation.rs:408-424
#
# 设计: hook 的 stdout 输出附加到工具结果尾部，
# 让 LLM 能看到 hook 的反馈信息 (比如 linter 警告)。
# ============================================================

def merge_hook_feedback(
    messages: list[str],
    output: str,
    denied: bool,
) -> str:
    """源码: conversation.rs:408-424"""
    if not messages:
        return output

    sections: list[str] = []
    if output.strip():
        sections.append(output)

    label = "Hook feedback (denied)" if denied else "Hook feedback"
    sections.append(f"{label}:\n{chr(10).join(messages)}")
    return "\n\n".join(sections)


# ============================================================
# TurnSummary — 结构化的 turn 返回值
# 源码: conversation.rs:87-93
#
# 比起返回一个字符串，结构化返回让调用方能精确知道:
# - LLM 说了什么 (assistant_messages)
# - 工具执行了什么 (tool_results)
# - 循环了几轮 (iterations)
# - 花了多少 token (usage)
# - 是否触发了压缩 (auto_compacted)
# ============================================================

class TurnSummary(BaseModel):
    assistant_messages: list[Message] = Field(default_factory=list)
    tool_results: list[Message] = Field(default_factory=list)
    iterations: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    auto_compacted: bool = False


# ============================================================
# ConversationRuntime — 对话运行时
# 源码: conversation.rs:100-334
#
# 这是整个 agent 的心跳。它组合了所有之前写的模块:
#   - api_client: 调 LLM API
#   - tool_executor: 执行工具
#   - permission_policy: 权限校验
#   - hook_runner: Hook 拦截
#   - compact: Token 压缩
#   - session: 消息历史
#   - usage_tracker: Token 计量
#
# 设计哲学: "Harness = Body, LLM = Brain"
# Runtime 永远不做决策。它只:
#   1. 感知 (接收事件)
#   2. 执行 (调工具)
#   3. 记忆 (session + storage)
#   4. 约束 (permissions + hooks + compaction)
# 所有"智能"来自 LLM。
# ============================================================

DEFAULT_MAX_ITERATIONS = 128
DEFAULT_AUTO_COMPACT_THRESHOLD = 200_000


class ConversationRuntime:
    def __init__(
        self,
        session: Session,
        api_client,  # ApiClient Protocol
        tool_executor: ToolExecutor,
        permission_policy: PermissionPolicy,
        system_prompt: list[str],
        hook_runner: Optional[HookRunner] = None,
    ):
        """源码: conversation.rs:117-133"""
        self._session = session
        self._api_client = api_client
        self._tool_executor = tool_executor
        self._permission_policy = permission_policy
        self._system_prompt = system_prompt
        self._hook_runner = hook_runner or HookRunner()
        self._max_iterations = DEFAULT_MAX_ITERATIONS
        self._usage_tracker = UsageTracker()
        self._auto_compact_threshold = DEFAULT_AUTO_COMPACT_THRESHOLD

    # --------------------------------------------------------
    # Builder 方法 — 链式配置
    # 源码: conversation.rs:158-168
    # --------------------------------------------------------

    def with_max_iterations(self, n: int) -> "ConversationRuntime":
        """源码: conversation.rs:158-162"""
        self._max_iterations = n
        return self

    def with_auto_compact_threshold(self, threshold: int) -> "ConversationRuntime":
        """源码: conversation.rs:164-168"""
        self._auto_compact_threshold = threshold
        return self

    # --------------------------------------------------------
    # run_turn — 核心 agentic loop
    # 源码: conversation.rs:170-283
    #
    # 这就是那个著名的循环:
    #   用户输入 → [API → 提取 tool_use → 权限 → hook → 执行 → loop] → 返回
    #
    # 注意: 一个 turn 可能包含多轮 API 调用 (当 LLM 需要多次工具调用时)。
    # iterations 计数的是 API 调用次数，不是用户交互次数。
    # --------------------------------------------------------

    def run_turn(
        self,
        user_input: str,
        prompter: Optional[PermissionPrompter] = None,
    ) -> TurnSummary:
        """源码: conversation.rs:170-283"""
        # 推入用户消息 — conversation.rs:175-176
        self._session.messages.append(Message.user_text(user_input))

        assistant_messages: list[Message] = []
        tool_results: list[Message] = []
        iterations = 0

        # ===== 主循环 =====
        while True:
            iterations += 1
            if iterations > self._max_iterations:
                raise RuntimeError(
                    "conversation loop exceeded the maximum number of iterations"
                )

            # 调 API — conversation.rs:191-195
            events = self._api_client.stream(
                self._system_prompt,
                self._session.messages,
            )

            # 事件流 → 结构化消息 — conversation.rs:196
            assistant_message, usage = build_assistant_message(events)

            if usage is not None:
                self._usage_tracker.record(usage)

            # 提取 tool_use blocks — conversation.rs:200-209
            pending_tool_uses: list[tuple[str, str, str]] = []
            for block in assistant_message.content:
                if isinstance(block, ToolContentBlock):
                    pending_tool_uses.append(
                        (block.id, block.name, block.input)
                    )

            # 推入 session — conversation.rs:211
            self._session.messages.append(assistant_message)
            assistant_messages.append(assistant_message)

            # 没有 tool_use → 结束 — conversation.rs:214-216
            if not pending_tool_uses:
                break

            # 对每个 tool_use 执行 — conversation.rs:218-271
            for tool_use_id, tool_name, tool_input in pending_tool_uses:
                result_message = self._process_tool_use(
                    tool_use_id, tool_name, tool_input, prompter,
                )
                self._session.messages.append(result_message)
                tool_results.append(result_message)

        # 自动压缩 — conversation.rs:274
        auto_compacted = self._maybe_auto_compact()

        return TurnSummary(
            assistant_messages=assistant_messages,
            tool_results=tool_results,
            iterations=iterations,
            usage=self._usage_tracker.cumulative_usage(),
            auto_compacted=auto_compacted,
        )

    # --------------------------------------------------------
    # _process_tool_use — 处理单个工具调用
    # 源码: conversation.rs:218-268
    #
    # 三层防线:
    # 1. 权限校验 (permission_policy.authorize)
    # 2. PreToolUse hook
    # 3. 工具执行 + PostToolUse hook
    #
    # 任何一层拒绝都返回 is_error=True 的 tool_result。
    # 关键: 拒绝不抛异常，而是返回错误消息让 LLM 看到。
    # 这样 LLM 可以调整策略 (比如换一个权限够的工具)。
    # --------------------------------------------------------

    def _process_tool_use(
        self,
        tool_use_id: str,
        tool_name: str,
        tool_input: str,
        prompter: Optional[PermissionPrompter],
    ) -> Message:
        """源码: conversation.rs:218-268"""
        # 第一层: 权限校验 — conversation.rs:219-224
        perm_result = self._permission_policy.authorize(
            tool_name, tool_input, prompter,
        )

        if not perm_result.is_allowed:
            # 权限拒绝 → 返回 deny reason — conversation.rs:265-267
            return Message.tool_result(
                tool_use_id, tool_name, perm_result.reason, True,
            )

        # 第二层: PreToolUse hook — conversation.rs:228-236
        pre_hook = self._hook_runner.run_pre_tool_use(tool_name, tool_input)
        if pre_hook.denied:
            deny_msg = f"PreToolUse hook denied tool `{tool_name}`"
            output = _format_hook_message(pre_hook, deny_msg)
            return Message.tool_result(
                tool_use_id, tool_name, output, True,
            )

        # 第三层: 执行工具 — conversation.rs:238-242
        try:
            output = self._tool_executor.execute(tool_name, tool_input)
            is_error = False
        except (ToolError, Exception) as exc:
            output = str(exc)
            is_error = True

        # 合并 pre hook 反馈 — conversation.rs:243
        output = merge_hook_feedback(pre_hook.messages, output, False)

        # PostToolUse hook — conversation.rs:245-255
        post_hook = self._hook_runner.run_post_tool_use(
            tool_name, tool_input, output, is_error,
        )
        if post_hook.denied:
            is_error = True
        output = merge_hook_feedback(
            post_hook.messages, output, post_hook.denied,
        )

        return Message.tool_result(
            tool_use_id, tool_name, output, is_error,
        )

    # --------------------------------------------------------
    # _maybe_auto_compact — 自动压缩
    # 源码: conversation.rs:310-333
    #
    # 触发条件: cumulative_input_tokens >= threshold
    # 执行: 调 compact_session，用 max_estimated_tokens=0 (强制压缩)
    # 效果: 用摘要替换旧消息，减少 context 占用
    # --------------------------------------------------------

    def _maybe_auto_compact(self) -> bool:
        """源码: conversation.rs:310-333"""
        cumulative = self._usage_tracker.cumulative_usage()
        if cumulative.input_tokens < self._auto_compact_threshold:
            return False

        result = compact_session(
            self._session.messages,
            CompactionConfig(
                max_estimated_tokens=0,  # 强制压缩
            ),
        )

        if result.removed_count == 0:
            return False

        self._session.messages = result.compacted_messages
        return True

    # --------------------------------------------------------
    # 访问器
    # --------------------------------------------------------

    def session(self) -> Session:
        """源码: conversation.rs:301-303"""
        return self._session

    def usage(self) -> UsageTracker:
        """源码: conversation.rs:296-298"""
        return self._usage_tracker


# ============================================================
# 辅助函数
# ============================================================

def _format_hook_message(result: HookResult, fallback: str) -> str:
    """源码: conversation.rs:400-406"""
    if not result.messages:
        return fallback
    return "\n".join(result.messages)
