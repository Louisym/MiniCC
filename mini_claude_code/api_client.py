"""
api_client.py — API 客户端: ABC 接口 + Anthropic SDK 流式调用 + 消息格式转换

源码对照:
  - rust/crates/rusty-claude-cli/src/main.rs:2384-2500 (AnthropicRuntimeClient)
  - rust/crates/rusty-claude-cli/src/main.rs:3091-3130 (convert_messages)

关键工程要点:
1. ABC 接口: stream() 返回 list[AssistantEvent]
2. 消息格式转换: 内部 role="tool" → API 的 role="user" + tool_result block
3. 工具定义注入: tools 参数传给 API，LLM 才知道可以调哪些工具
"""

from abc import ABC, abstractmethod
import json
from typing import Literal

from pydantic import BaseModel

try:
    from mini_claude_code.models import Message, TextContentBlock, ToolContentBlock, ToolResultContentBlock
except ImportError:
    from models import Message, TextContentBlock, ToolContentBlock, ToolResultContentBlock


# ============================================================
# 事件类型 — API 返回的流式事件
# ============================================================

class TextDeltaEvent(BaseModel):
    type: Literal['text_delta'] = 'text_delta'
    text: str


class ToolUseEvent(BaseModel):
    type: Literal['tool_use'] = 'tool_use'
    id: str
    name: str
    input: str


class MessageStopEvent(BaseModel):
    type: Literal['message_stop'] = 'message_stop'


AssistantEvent = TextDeltaEvent | ToolUseEvent | MessageStopEvent


# ============================================================
# ABC 接口
# ============================================================

class ApiClient(ABC):
    @abstractmethod
    def stream(self, system_prompt: list[str], messages: list[Message]) -> list[AssistantEvent]:
        ...


# ============================================================
# 消息格式转换 — 内部格式 → Anthropic API 格式
# 源码: main.rs:3091-3130 (convert_messages)
#
# Anthropic API 的要求:
# - role 只接受 "user" 和 "assistant"
# - tool_result 是 role="user" 的消息，content 包含 type="tool_result" block
# - tool_use 是 role="assistant" 的消息，input 必须是 dict 不是 string
# - 连续相同 role 的消息需要合并（API 要求 user/assistant 交替）
#
# 我们的内部格式:
# - role="tool" + ToolResultContentBlock
# - tool_use 的 input 是 string
#
# 这个函数做映射。
# ============================================================

def _convert_messages(messages: list[Message]) -> list[dict]:
    """源码: main.rs:3091-3130

    将内部消息格式转为 Anthropic API 接受的格式。
    """
    result: list[dict] = []

    for msg in messages:
        if msg.role == "tool":
            # tool → user + tool_result content blocks
            content = []
            for block in msg.content:
                if isinstance(block, ToolResultContentBlock):
                    tr: dict = {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": block.output,
                    }
                    if block.is_error:
                        tr["is_error"] = True
                    content.append(tr)
            if content:
                result.append({"role": "user", "content": content})

        elif msg.role == "assistant":
            content = []
            for block in msg.content:
                if isinstance(block, TextContentBlock):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolContentBlock):
                    # input: string → dict
                    try:
                        input_dict = json.loads(block.input)
                    except (json.JSONDecodeError, TypeError):
                        input_dict = {"raw": block.input}
                    content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": input_dict,
                    })
            if content:
                result.append({"role": "assistant", "content": content})

        elif msg.role == "user":
            content = []
            for block in msg.content:
                if isinstance(block, TextContentBlock):
                    content.append({"type": "text", "text": block.text})
            if content:
                result.append({"role": "user", "content": content})

    # 合并连续相同 role 的消息 (API 要求交替)
    merged: list[dict] = []
    for entry in result:
        if merged and merged[-1]["role"] == entry["role"]:
            merged[-1]["content"].extend(entry["content"])
        else:
            merged.append(entry)

    return merged


# ============================================================
# ClaudeApiClient — Anthropic SDK 实现
# ============================================================

class ClaudeApiClient(ApiClient):
    """源码: main.rs:2384-2500 (AnthropicRuntimeClient)

    emit_output: 是否实时打印文本到终端。
    CC 的做法: streaming 时同步渲染 markdown 到 stdout (main.rs:2436-2460)。
    收到 TextDelta 时立刻 print，不等整个响应结束。
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        tools: list[dict] | None = None,
        emit_output: bool = True,
    ):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.tools = tools or []
        self.emit_output = emit_output

    def stream(self, system_prompt: list[str], messages: list[Message]) -> list[AssistantEvent]:
        import sys as _sys

        converted = _convert_messages(messages)
        system_prompt_str = '\n'.join(system_prompt)

        kwargs: dict = {
            "model": self.model,
            "max_tokens": 8096,
            "system": system_prompt_str,
            "messages": converted,
        }
        if self.tools:
            kwargs["tools"] = self.tools

        events: list[AssistantEvent] = []
        streaming_text = False  # 追踪是否正在流式输出文本

        with self.client.messages.stream(**kwargs) as s:
            for event in s:
                if event.type == 'content_block_delta':
                    if event.delta.type == 'text_delta':
                        events.append(TextDeltaEvent(text=event.delta.text))
                        # 实时流式输出 — CC 的 emit_output
                        if self.emit_output:
                            if not streaming_text:
                                _sys.stdout.write("\n")
                                streaming_text = True
                            _sys.stdout.write(event.delta.text)
                            _sys.stdout.flush()

                elif event.type == 'content_block_stop':
                    if event.content_block.type == 'tool_use':
                        # 工具调用前换行
                        if self.emit_output and streaming_text:
                            _sys.stdout.write("\n")
                            _sys.stdout.flush()
                            streaming_text = False
                        events.append(ToolUseEvent(
                            id=event.content_block.id,
                            name=event.content_block.name,
                            input=json.dumps(event.content_block.input),
                        ))

                elif event.type == 'message_stop':
                    if self.emit_output and streaming_text:
                        _sys.stdout.write("\n")
                        _sys.stdout.flush()
                        streaming_text = False
                    events.append(MessageStopEvent())

        return events
