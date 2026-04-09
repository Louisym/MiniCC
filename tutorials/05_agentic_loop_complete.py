"""
Tutorial 05: 完整的 Agentic Loop — 把前 4 个教程整合起来
=========================================================

前 4 个教程分别教了：
  01: Agentic Loop 的基本循环
  02: Session 和消息模型
  03: 工具系统
  04: 权限系统

现在我们把它们组装成一个完整的 ConversationRuntime —— 这就是 Claude Code 的核心引擎！

这个教程会教你：
1. 怎么把各模块组合在一起
2. 一个完整的对话轮次是怎么运行的
3. Token 追踪和使用统计

对应源码：rust/crates/runtime/src/conversation.rs 的 ConversationRuntime

运行方式：python tutorials/05_agentic_loop_complete.py
"""

import json
from dataclasses import dataclass, field
from typing import Protocol, Optional, Callable
from enum import IntEnum


# ======================================================================
# 复用前几个教程的核心类型（精简版，只保留关键部分）
# ======================================================================

# --- 消息模型 (Tutorial 02) ---
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

    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

@dataclass(frozen=True)
class ConversationMessage:
    role: str
    blocks: tuple[ContentBlock, ...]
    usage: Optional[TokenUsage] = None

    @staticmethod
    def user_text(text: str) -> "ConversationMessage":
        return ConversationMessage(role="user", blocks=(TextBlock(text=text),))

    @staticmethod
    def assistant(blocks: tuple[ContentBlock, ...], usage: Optional[TokenUsage] = None) -> "ConversationMessage":
        return ConversationMessage(role="assistant", blocks=blocks, usage=usage)

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


# --- 权限 (Tutorial 04 精简版) ---
class PermissionMode(IntEnum):
    READ_ONLY = 1
    WORKSPACE_WRITE = 2
    DANGER_FULL_ACCESS = 3

class PermissionPrompter(Protocol):
    def decide(self, tool_name: str, tool_input: str) -> tuple[bool, str]: ...

class AutoAllowPrompter:
    def decide(self, tool_name: str, tool_input: str) -> tuple[bool, str]:
        return (True, "auto-approved")

class PermissionPolicy:
    def __init__(self, mode: PermissionMode):
        self.mode = mode
        self._requirements: dict[str, PermissionMode] = {}

    def with_tool_requirement(self, tool_name: str, required: PermissionMode) -> "PermissionPolicy":
        self._requirements[tool_name] = required
        return self

    def authorize(self, tool_name: str, tool_input: str, prompter: Optional[PermissionPrompter] = None) -> tuple[bool, str]:
        required = self._requirements.get(tool_name, PermissionMode.DANGER_FULL_ACCESS)
        if self.mode >= required:
            return (True, "permitted")
        if prompter is not None:
            return prompter.decide(tool_name, tool_input)
        return (False, f"tool '{tool_name}' requires {required.name}, current is {self.mode.name}")


# --- 工具执行器 (Tutorial 03 精简版) ---
class ToolExecutor:
    def __init__(self):
        self._handlers: dict[str, Callable[[str], str]] = {}

    def register(self, name: str, handler: Callable[[str], str]) -> "ToolExecutor":
        self._handlers[name] = handler
        return self

    def execute(self, tool_name: str, tool_input: str) -> tuple[str, bool]:
        if tool_name not in self._handlers:
            return (f"Unknown tool: {tool_name}", True)
        try:
            result = self._handlers[tool_name](tool_input)
            return (result, False)
        except Exception as e:
            return (str(e), True)


# ======================================================================
# 新内容：API Client 接口
# ======================================================================
# 在真正的 Claude Code 里，ApiClient 通过 HTTP 调用 Anthropic API。
# 这里我们用一个模拟的"剧本式"客户端。
#
# 关键概念：ApiRequest 和 AssistantEvent
#
# ApiRequest: 发给 AI 的请求，包含系统提示词和所有对话历史
# AssistantEvent: AI 返回的事件流（流式传输，一点一点返回）

@dataclass
class ApiRequest:
    """发给 AI 的请求"""
    system_prompt: list[str]       # 系统提示词
    messages: list[ConversationMessage]  # 对话历史

@dataclass(frozen=True)
class AssistantEvent:
    """
    AI 返回的一个事件。

    AI 的回复不是一次性返回的，而是一个个事件：
      TextDelta("让我") → TextDelta("算一下") → ToolUse("add", "2,2") → Usage(...) → Stop

    这就像看直播一样，内容一点一点出来，而不是等全部完成才显示。
    这叫做"流式传输"（Streaming），让用户能更快看到 AI 在干什么。

    对应源码: conversation.rs:23-32
    """
    event_type: str  # "text_delta" | "tool_use" | "usage" | "message_stop"
    text: str = ""
    tool_id: str = ""
    tool_name: str = ""
    tool_input: str = ""
    usage: Optional[TokenUsage] = None


class ApiClient(Protocol):
    """
    API 客户端接口 —— 负责和 AI 通信。

    对应源码: conversation.rs:34-36
    """
    def stream(self, request: ApiRequest) -> list[AssistantEvent]: ...


# ======================================================================
# Token 使用追踪器
# ======================================================================

class UsageTracker:
    """
    追踪 Token 使用量（就像手机流量统计）。

    对应源码: runtime/src/usage.rs
    """
    def __init__(self):
        self.turns = 0
        self._cumulative_input = 0
        self._cumulative_output = 0

    def record(self, usage: TokenUsage):
        self.turns += 1
        self._cumulative_input += usage.input_tokens
        self._cumulative_output += usage.output_tokens

    def cumulative_usage(self) -> TokenUsage:
        return TokenUsage(
            input_tokens=self._cumulative_input,
            output_tokens=self._cumulative_output,
        )

    def summary(self) -> str:
        total = self._cumulative_input + self._cumulative_output
        return f"turns={self.turns}, input={self._cumulative_input}, output={self._cumulative_output}, total={total}"


# ======================================================================
# 核心：ConversationRuntime — Claude Code 的引擎
# ======================================================================

@dataclass
class TurnSummary:
    """一个对话轮次的总结"""
    assistant_messages: list[ConversationMessage]
    tool_results: list[ConversationMessage]
    iterations: int
    usage: TokenUsage


class ConversationRuntime:
    """
    对话运行时 —— 把 API、工具、权限、Session 全部组合在一起。

    这就是 Claude Code 的核心引擎。
    对应源码: conversation.rs:100-168

    在 Rust 源码里它是泛型的：ConversationRuntime<C, T>
    C = ApiClient 类型, T = ToolExecutor 类型
    Python 里我们直接用 Protocol，效果一样。
    """

    def __init__(
        self,
        session: Session,
        api_client: ApiClient,
        tool_executor: ToolExecutor,
        permission_policy: PermissionPolicy,
        system_prompt: list[str],
        max_iterations: int = 10,
    ):
        self.session = session
        self.api_client = api_client
        self.tool_executor = tool_executor
        self.permission_policy = permission_policy
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.usage_tracker = UsageTracker()

    def run_turn(
        self,
        user_input: str,
        prompter: Optional[PermissionPrompter] = None,
    ) -> TurnSummary:
        """
        执行一个完整的对话轮次。

        这就是 Tutorial 01 里的 run_turn，但现在是完整版！

        对应源码: conversation.rs:170-283
        """
        # 步骤 1: 添加用户消息
        self.session.messages.append(ConversationMessage.user_text(user_input))

        assistant_messages: list[ConversationMessage] = []
        tool_results: list[ConversationMessage] = []
        iterations = 0

        # 步骤 2: Agentic Loop
        while True:
            iterations += 1
            if iterations > self.max_iterations:
                raise RuntimeError("exceeded maximum iterations")

            # 步骤 2a: 调用 API
            request = ApiRequest(
                system_prompt=self.system_prompt,
                messages=self.session.messages,
            )
            events = self.api_client.stream(request)

            # 步骤 2b: 解析 AI 回复
            assistant_msg, usage = self._build_assistant_message(events)
            if usage:
                self.usage_tracker.record(usage)

            self.session.messages.append(assistant_msg)
            assistant_messages.append(assistant_msg)

            # 步骤 2c: 提取工具调用请求
            pending_tool_uses = [
                block for block in assistant_msg.blocks
                if isinstance(block, ToolUseBlock)
            ]

            # 没有工具调用 → 循环结束
            if not pending_tool_uses:
                break

            # 步骤 2d: 逐个执行工具
            for tool_use in pending_tool_uses:
                # 先检查权限！
                allowed, reason = self.permission_policy.authorize(
                    tool_use.name, tool_use.input, prompter,
                )

                if allowed:
                    # 权限通过 → 执行工具
                    output, is_error = self.tool_executor.execute(
                        tool_use.name, tool_use.input,
                    )
                else:
                    # 权限拒绝 → 把拒绝原因作为工具结果
                    output = reason
                    is_error = True

                # 构建工具结果消息
                result_msg = ConversationMessage.tool_result(
                    tool_use_id=tool_use.id,
                    tool_name=tool_use.name,
                    output=output,
                    is_error=is_error,
                )
                self.session.messages.append(result_msg)
                tool_results.append(result_msg)

        # 返回这个轮次的总结
        return TurnSummary(
            assistant_messages=assistant_messages,
            tool_results=tool_results,
            iterations=iterations,
            usage=self.usage_tracker.cumulative_usage(),
        )

    def _build_assistant_message(
        self, events: list[AssistantEvent],
    ) -> tuple[ConversationMessage, Optional[TokenUsage]]:
        """
        把事件流解析成一条完整的助手消息。

        事件流: [TextDelta("让我"), TextDelta("算一下"), ToolUse(...), Usage(...), Stop]
        → 消息: assistant([TextBlock("让我算一下"), ToolUseBlock(...)])

        对应源码: conversation.rs:353-398
        """
        text_buffer = ""
        blocks: list[ContentBlock] = []
        usage = None

        for event in events:
            if event.event_type == "text_delta":
                text_buffer += event.text
            elif event.event_type == "tool_use":
                # 先把之前积累的文字作为一个 TextBlock
                if text_buffer:
                    blocks.append(TextBlock(text=text_buffer))
                    text_buffer = ""
                blocks.append(ToolUseBlock(
                    id=event.tool_id,
                    name=event.tool_name,
                    input=event.tool_input,
                ))
            elif event.event_type == "usage":
                usage = event.usage
            elif event.event_type == "message_stop":
                pass

        # 别忘了最后还没 flush 的文字
        if text_buffer:
            blocks.append(TextBlock(text=text_buffer))

        return (
            ConversationMessage.assistant(tuple(blocks), usage),
            usage,
        )


# ======================================================================
# 模拟一个完整的场景
# ======================================================================

class DemoApiClient:
    """
    模拟 API 客户端 —— 按剧本回复。

    场景：用户问 "当前目录有什么文件？"
    AI 第一次回复：要用 bash 工具执行 ls
    AI 第二次回复：根据工具结果总结
    """
    def __init__(self):
        self.call_count = 0

    def stream(self, request: ApiRequest) -> list[AssistantEvent]:
        self.call_count += 1

        if self.call_count == 1:
            return [
                AssistantEvent(event_type="text_delta", text="让我看看当前目录。"),
                AssistantEvent(
                    event_type="tool_use",
                    tool_id="tool-001",
                    tool_name="bash",
                    tool_input='{"command": "ls"}',
                ),
                AssistantEvent(
                    event_type="usage",
                    usage=TokenUsage(input_tokens=50, output_tokens=12),
                ),
                AssistantEvent(event_type="message_stop"),
            ]
        else:
            # 从对话历史里找到工具结果
            tool_output = ""
            for msg in request.messages:
                for block in msg.blocks:
                    if isinstance(block, ToolResultBlock):
                        tool_output = block.output

            return [
                AssistantEvent(
                    event_type="text_delta",
                    text=f"当前目录的文件有:\n{tool_output}",
                ),
                AssistantEvent(
                    event_type="usage",
                    usage=TokenUsage(input_tokens=80, output_tokens=20),
                ),
                AssistantEvent(event_type="message_stop"),
            ]


def main():
    print("=" * 60)
    print("Tutorial 05: 完整的 ConversationRuntime 演示")
    print("=" * 60)

    # --- 组装引擎 ---
    session = Session()

    api_client = DemoApiClient()

    tool_executor = (
        ToolExecutor()
        .register("bash", lambda input_str: "file1.py\nfile2.py\nREADME.md")
        .register("read_file", lambda input_str: "file content here")
    )

    permission_policy = (
        PermissionPolicy(PermissionMode.WORKSPACE_WRITE)
        .with_tool_requirement("bash", PermissionMode.DANGER_FULL_ACCESS)
        .with_tool_requirement("read_file", PermissionMode.READ_ONLY)
    )

    runtime = ConversationRuntime(
        session=session,
        api_client=api_client,
        tool_executor=tool_executor,
        permission_policy=permission_policy,
        system_prompt=["You are a helpful coding assistant."],
    )

    # --- 场景 A: 有 Prompter，bash 会被询问并通过 ---
    print("\n--- 场景 A: 使用 AutoAllowPrompter ---")
    summary = runtime.run_turn(
        "当前目录有什么文件？",
        prompter=AutoAllowPrompter(),
    )

    print(f"\n  循环轮次: {summary.iterations}")
    print(f"  助手消息数: {len(summary.assistant_messages)}")
    print(f"  工具结果数: {len(summary.tool_results)}")
    print(f"  Token 使用: {runtime.usage_tracker.summary()}")

    # 打印完整对话
    print("\n  --- 完整对话记录 ---")
    for i, msg in enumerate(session.messages):
        role = msg.role.upper()
        for block in msg.blocks:
            if isinstance(block, TextBlock):
                preview = block.text[:60].replace('\n', ' ')
                print(f"  [{i}] {role}: {preview}")
            elif isinstance(block, ToolUseBlock):
                print(f"  [{i}] {role}: [调用工具 {block.name}({block.input})]")
            elif isinstance(block, ToolResultBlock):
                status = "ERROR" if block.is_error else "OK"
                print(f"  [{i}] {role}: [工具结果 {block.tool_name}: {status}] {block.output[:40]}")

    # --- 场景 B: 没有 Prompter，bash 会被拒绝 ---
    print("\n\n--- 场景 B: 不提供 Prompter，bash 将被拒绝 ---")
    session2 = Session()
    runtime2 = ConversationRuntime(
        session=session2,
        api_client=DemoApiClient(),  # 新的 API 客户端
        tool_executor=tool_executor,
        permission_policy=permission_policy,
        system_prompt=["You are a helpful coding assistant."],
    )
    summary2 = runtime2.run_turn("当前目录有什么文件？", prompter=None)

    print(f"  工具结果数: {len(summary2.tool_results)}")
    for result_msg in summary2.tool_results:
        for block in result_msg.blocks:
            if isinstance(block, ToolResultBlock):
                print(f"  工具 {block.tool_name}: is_error={block.is_error}")
                print(f"  输出: {block.output}")

    # 解说
    print("\n" + "=" * 60)
    print("关键理解要点：")
    print("=" * 60)
    print("""
    ConversationRuntime 把四大模块组装在一起：

    ┌─────────────────────────────────────────┐
    │         ConversationRuntime             │
    │                                         │
    │  ┌───────────┐    ┌──────────────────┐  │
    │  │ ApiClient  │    │  ToolExecutor    │  │
    │  │ (调用 AI)  │    │  (执行工具)       │  │
    │  └─────┬─────┘    └────────┬─────────┘  │
    │        │                   │             │
    │        ▼                   ▼             │
    │  ┌─────────────────────────────────────┐│
    │  │         run_turn() 循环             ││
    │  │  User → API → [Permission] → Tool  ││
    │  │              → API → ...            ││
    │  └─────────────────────────────────────┘│
    │        │                   │             │
    │  ┌─────┴─────┐    ┌──────┴───────────┐  │
    │  │  Session   │    │ PermissionPolicy │  │
    │  │ (对话记忆)  │    │ (权限检查)        │  │
    │  └───────────┘    └──────────────────┘  │
    │        │                                │
    │  ┌─────┴─────┐                          │
    │  │UsageTracker│                          │
    │  │(Token统计) │                          │
    │  └───────────┘                          │
    └─────────────────────────────────────────┘

    流程（对应源码 conversation.rs:170-283）:
    1. 用户输入 → 添加到 session
    2. 循环开始:
       a. 构建 ApiRequest（系统提示 + 全部历史）
       b. 调用 api_client.stream() 获取事件流
       c. 解析事件流 → 构建 assistant 消息
       d. 提取 ToolUse 块
       e. 如果没有 ToolUse → break（循环结束）
       f. 对每个 ToolUse:
          - 权限检查 (permission_policy.authorize)
          - 如果允许 → 执行工具 (tool_executor.execute)
          - 如果拒绝 → 将拒绝原因作为错误结果
          - 结果加入 session
       g. 回到步骤 a
    3. 返回 TurnSummary（总结这一轮发生了什么）
    """)


if __name__ == "__main__":
    main()
