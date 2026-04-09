"""
Tutorial 10: SSE Streaming — 流式输出的秘密
=============================================

你在用 Claude Code 时，AI 的回复是一个字一个字"打"出来的，而不是
等很久然后"啪"一下全部出现。这就是"流式输出"(streaming)。

它背后的技术叫 SSE（Server-Sent Events，服务器推送事件）。

生活类比：
  普通请求 = 叫外卖：你下单（发请求），等 30 分钟，骑手一次送来整份餐
  流式请求 = 自助传送带火锅：你下单后，菜品一盘一盘地通过传送带送到你面前

SSE 的好处：
  1. 用户体验好 —— 不用干等，能看到 AI "正在思考"
  2. 第一个字出现很快（首 token 延迟低）
  3. 可以中途取消（看到结果不对就停下来）

本教程会教你：
  1. SSE 协议的文本格式（超简单！）
  2. 如何增量解析 SSE 流（处理网络分片）
  3. Claude API 的 StreamEvent 类型
  4. 如何把流式事件组装成完整的 AI 回复
  5. 完整的流式对话演示

对应源码：
  - rust/crates/api/src/sse.rs      → SSE 文本解析器
  - rust/crates/api/src/types.rs    → StreamEvent 类型定义
  - rust/crates/api/src/client.rs   → MessageStream（流式客户端）
  - rust/crates/runtime/src/sse.rs  → IncrementalSseParser（另一个增量解析器）
  - rust/crates/runtime/src/conversation.rs → build_assistant_message()

运行方式：python tutorials/10_sse_streaming.py
"""

import json
import time
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ============================================================
# 第一步：理解 SSE 协议的文本格式
# ============================================================
# SSE 是 HTTP 的一种用法。它很简单：
#
# 普通 HTTP 请求：
#   客户端 → 发请求 → 服务器处理 → 返回完整 JSON → 连接关闭
#
# SSE 请求：
#   客户端 → 发请求 → 服务器开始持续发送文本 → 一条一条发 → 最后关闭
#
# SSE 的文本格式非常简单，就是纯文本，每个"事件"长这样：
#
#   event: 事件类型名
#   data: 这里是数据（通常是 JSON）
#
#   （两个换行符 \n\n 表示这个事件结束了）
#
# 特殊规则：
#   - 以 : 开头的行是注释，忽略
#   - data: [DONE] 表示整个流结束了
#   - event: ping 是心跳包，也忽略
#
# 举个真实例子，Claude API 返回 "Hello" 这个词，SSE 流长这样：
#
#   event: message_start
#   data: {"type":"message_start","message":{...}}
#
#   event: content_block_start
#   data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}
#
#   event: content_block_delta
#   data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}
#
#   event: content_block_stop
#   data: {"type":"content_block_stop","index":0}
#
#   event: message_delta
#   data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{...}}
#
#   event: message_stop
#   data: {"type":"message_stop"}
#
#   data: [DONE]

# 我们先定义一个简单的 SSE 事件结构
@dataclass
class SseEvent:
    """
    一个 SSE 事件。

    event: 事件类型名（比如 "message_start", "content_block_delta"）
    data:  事件数据（通常是 JSON 字符串）
    id:    事件 ID（可选，Claude API 不用这个）
    retry: 重连间隔毫秒数（可选，Claude API 不用这个）

    对应源码: runtime/sse.rs:4-9
    """
    event: Optional[str] = None
    data: str = ""
    id: Optional[str] = None
    retry: Optional[int] = None


# ============================================================
# 第二步：SSE 解析器 —— 处理网络分片的关键
# ============================================================
# 为什么需要"增量解析"？
#
# 因为网络传输时，数据不一定按事件的边界来切分！
#
# 想象一下：服务器发了两个事件 A 和 B。
# 理想情况：你收到两个完整的包 [事件A] [事件B]
# 实际情况：网络把它们切成了三块   [事件A的前半] [事件A的后半+事件B的前半] [事件B的后半]
#
# 生活类比：
#   你在听收音机读报纸。播音员一句一句读，但信号不稳定，
#   有时候一句话被切成两段传过来。你需要在脑子里"拼接"，
#   等拼成完整一句话再理解它的意思。
#
# SSE 解析器的工作就是：
#   1. 收到一块数据（chunk）→ 放进缓冲区（buffer）
#   2. 在缓冲区里找 \n\n（事件结束标记）
#   3. 找到了 → 提取出完整事件，解析它
#   4. 没找到 → 继续等下一块数据

class SseParser:
    """
    SSE 增量解析器。

    核心思路：维护一个字符串缓冲区。
    每次收到新数据就追加到缓冲区，然后尝试从中提取完整的事件。

    对应源码: api/sse.rs:5-61 (SseParser)
              runtime/sse.rs:12-97 (IncrementalSseParser)
    """

    def __init__(self):
        self.buffer = ""           # 缓冲区：存放还没解析完的数据
        self._event_name = None    # 当前正在解析的事件名
        self._data_lines = []      # 当前事件的 data 行
        self._id = None            # 当前事件的 id
        self._retry = None         # 当前事件的 retry

    def push_chunk(self, chunk: str) -> list[SseEvent]:
        """
        推入一块新收到的数据，返回解析出的完整事件列表。

        可能返回 0 个事件（数据不够完整）
        可能返回 1 个事件（刚好凑齐一个）
        可能返回多个事件（一块数据里包含了好几个事件）

        对应源码: runtime/sse.rs:26-39 (push_chunk)
        """
        self.buffer += chunk
        events = []

        # 逐行处理缓冲区
        while "\n" in self.buffer:
            # 找到第一个换行符的位置
            index = self.buffer.index("\n")
            # 提取这一行（不包含换行符）
            line = self.buffer[:index]
            # 从缓冲区移除已处理的部分（包含换行符）
            self.buffer = self.buffer[index + 1:]
            # 处理 \r\n 的情况（Windows 风格换行）
            line = line.rstrip("\r")
            # 处理这一行
            self._process_line(line, events)

        return events

    def finish(self) -> list[SseEvent]:
        """
        流结束时调用。处理缓冲区中剩余的数据。

        对应源码: runtime/sse.rs:44-53 (finish)
        """
        events = []
        if self.buffer:
            line = self.buffer.rstrip("\r")
            self.buffer = ""
            self._process_line(line, events)
        # 如果还有未完成的事件，提取出来
        event = self._take_event()
        if event:
            events.append(event)
        return events

    def _process_line(self, line: str, events: list[SseEvent]):
        """
        处理一行 SSE 文本。

        SSE 协议的核心规则在这里：
        - 空行 → 当前事件结束，提取它
        - : 开头 → 注释，忽略
        - event: → 设置事件名
        - data: → 添加数据行
        - id: → 设置事件 ID
        - retry: → 设置重连间隔

        对应源码: runtime/sse.rs:56-79 (process_line)
        """
        if not line:
            # 空行 = 事件结束标记
            event = self._take_event()
            if event:
                events.append(event)
            return

        if line.startswith(":"):
            # 注释行（比如 ": keepalive"），直接忽略
            return

        # 把行拆成 "字段名: 值" 的形式
        if ":" in line:
            field_name, value = line.split(":", 1)
            # SSE 规范：冒号后面如果有一个空格，去掉这个空格
            if value.startswith(" "):
                value = value[1:]
        else:
            field_name = line
            value = ""

        # 根据字段名分别处理
        if field_name == "event":
            self._event_name = value
        elif field_name == "data":
            self._data_lines.append(value)
        elif field_name == "id":
            self._id = value
        elif field_name == "retry":
            try:
                self._retry = int(value)
            except ValueError:
                pass
        # 其他未知字段直接忽略（SSE 规范要求的）

    def _take_event(self) -> Optional[SseEvent]:
        """
        提取当前积累的事件数据，组装成 SseEvent。
        提取后重置内部状态，准备解析下一个事件。

        对应源码: runtime/sse.rs:82-96 (take_event)
        """
        # 如果什么数据都没有，说明没有有效事件
        if not self._data_lines and not self._event_name and not self._id and self._retry is None:
            return None

        # 多行 data 用换行符拼接（SSE 规范）
        data = "\n".join(self._data_lines)
        self._data_lines = []

        event = SseEvent(
            event=self._event_name,
            data=data,
            id=self._id,
            retry=self._retry,
        )

        # 重置状态
        self._event_name = None
        self._id = None
        self._retry = None

        return event


# ============================================================
# 第三步：Claude API 的 StreamEvent 类型
# ============================================================
# Claude API 通过 SSE 流式返回回复，事件类型有这些：
#
#   message_start        → AI 开始回复了（包含消息元信息）
#   content_block_start  → 一个内容块开始了（文本块 or 工具调用块）
#   content_block_delta  → 内容块的增量更新（一小段文字 or 一小段 JSON）
#   content_block_stop   → 一个内容块结束了
#   message_delta        → 消息级别的增量（stop_reason, usage）
#   message_stop         → AI 回复结束了
#
# 一次完整回复的事件流是这样的：
#
#   message_start
#     ├── content_block_start (index=0, 文本块)
#     │     ├── content_block_delta (text="你")
#     │     ├── content_block_delta (text="好")
#     │     ├── content_block_delta (text="！")
#     │     └── content_block_stop (index=0)
#     ├── content_block_start (index=1, 工具调用块)
#     │     ├── content_block_delta (partial_json='{"com')
#     │     ├── content_block_delta (partial_json='mand')
#     │     ├── content_block_delta (partial_json='": "ls"}')
#     │     └── content_block_stop (index=1)
#     ├── message_delta (stop_reason="tool_use", usage=...)
#     └── message_stop

class StreamEventType(Enum):
    """
    流式事件类型。

    对应源码: api/types.rs:203-212
    """
    MESSAGE_START = "message_start"
    CONTENT_BLOCK_START = "content_block_start"
    CONTENT_BLOCK_DELTA = "content_block_delta"
    CONTENT_BLOCK_STOP = "content_block_stop"
    MESSAGE_DELTA = "message_delta"
    MESSAGE_STOP = "message_stop"


@dataclass
class StreamEvent:
    """
    解析后的流式事件。

    对应源码: api/types.rs:203-212 (StreamEvent enum)
    """
    event_type: str           # 事件类型
    data: dict                # 解析后的 JSON 数据


def parse_stream_event(sse_event: SseEvent) -> Optional[StreamEvent]:
    """
    把原始 SSE 事件转换为 Claude API 的 StreamEvent。

    跳过：ping 事件、[DONE] 标记、注释、无数据事件。

    对应源码: api/sse.rs:63-101 (parse_frame)
    """
    # 跳过 ping 心跳
    if sse_event.event == "ping":
        return None

    # 跳过没有数据的事件
    if not sse_event.data:
        return None

    # 跳过 [DONE] 标记（流结束信号）
    if sse_event.data == "[DONE]":
        return None

    # 解析 JSON 数据
    try:
        data = json.loads(sse_event.data)
    except json.JSONDecodeError:
        print(f"  [WARNING] 无法解析 JSON: {sse_event.data[:50]}...")
        return None

    event_type = data.get("type", sse_event.event or "unknown")
    return StreamEvent(event_type=event_type, data=data)


# ============================================================
# 第四步：StreamAssembler — 把碎片事件组装成完整回复
# ============================================================
# SSE 给你的是一堆碎片事件。但 Agentic Loop 需要的是完整的消息。
# StreamAssembler 就是"拼图工人"：把碎片拼成完整的 AI 回复。
#
# 它需要做的事情：
#   1. 累积 text_delta → 拼出完整的文本
#   2. 累积 input_json_delta → 拼出完整的工具调用 JSON
#   3. 记录 usage（token 使用量）
#   4. 检测 message_stop（是否结束）

class AssistantEventType(Enum):
    """
    Agentic Loop 关心的事件类型（从 StreamEvent 精简而来）。

    对应源码: conversation.rs:23-32 (AssistantEvent)
    """
    TEXT_DELTA = "text_delta"
    TOOL_USE = "tool_use"
    USAGE = "usage"
    MESSAGE_STOP = "message_stop"


@dataclass
class AssistantEvent:
    """
    给 Agentic Loop 用的精简事件。

    对应源码: conversation.rs:23-32
    """
    event_type: AssistantEventType
    text: str = ""                   # TEXT_DELTA 时用
    tool_id: str = ""                # TOOL_USE 时用
    tool_name: str = ""              # TOOL_USE 时用
    tool_input: str = ""             # TOOL_USE 时用
    input_tokens: int = 0            # USAGE 时用
    output_tokens: int = 0           # USAGE 时用


@dataclass
class ContentBlockState:
    """
    正在构建中的内容块的状态。

    因为 content_block_delta 是一小段一小段来的，
    我们需要把它们累积起来。

    就像拼图：每个 delta 是一块拼图碎片，
    content_block_stop 时拼图就完成了。
    """
    index: int
    block_type: str  # "text" or "tool_use"
    text: str = ""   # 累积的文本
    tool_id: str = ""
    tool_name: str = ""
    partial_json: str = ""  # 累积的工具调用 JSON


def process_stream_events(stream_events: list[StreamEvent]) -> list[AssistantEvent]:
    """
    将 StreamEvent 列表转换为 AssistantEvent 列表。

    这个函数模拟了 Claude Code 中从 API 流式事件
    到 Agentic Loop 可用事件的转换过程。

    对应源码:
      - api/client.rs:538-562 (MessageStream::next_event)
      - conversation.rs:353-390 (build_assistant_message)
    """
    assistant_events = []
    # 正在构建中的内容块 (key = index)
    building_blocks: dict[int, ContentBlockState] = {}

    for event in stream_events:
        event_type = event.event_type

        if event_type == "message_start":
            # 消息开始，通常包含元信息（model, id 等）
            # 我们这里不需要特别处理
            pass

        elif event_type == "content_block_start":
            # 一个新的内容块开始了
            index = event.data["index"]
            block = event.data["content_block"]
            block_type = block["type"]  # "text" or "tool_use"

            state = ContentBlockState(
                index=index,
                block_type=block_type,
            )

            if block_type == "tool_use":
                state.tool_id = block.get("id", "")
                state.tool_name = block.get("name", "")

            if block_type == "text" and block.get("text"):
                # 有些 content_block_start 自带初始文本
                state.text = block["text"]
                assistant_events.append(AssistantEvent(
                    event_type=AssistantEventType.TEXT_DELTA,
                    text=block["text"],
                ))

            building_blocks[index] = state

        elif event_type == "content_block_delta":
            # 增量更新
            index = event.data["index"]
            delta = event.data["delta"]
            delta_type = delta["type"]

            state = building_blocks.get(index)
            if state is None:
                continue

            if delta_type == "text_delta":
                # 文本增量 → 累积文本，同时产生一个实时事件
                text = delta["text"]
                state.text += text
                assistant_events.append(AssistantEvent(
                    event_type=AssistantEventType.TEXT_DELTA,
                    text=text,
                ))

            elif delta_type == "input_json_delta":
                # 工具调用的 JSON 增量 → 累积 JSON 字符串
                # 注意：这个 JSON 是一小段一小段来的，不是完整的！
                # 比如：'{"com' → 'mand' → '": "ls"}'
                state.partial_json += delta["partial_json"]

        elif event_type == "content_block_stop":
            # 一个内容块结束了
            index = event.data["index"]
            state = building_blocks.pop(index, None)
            if state is None:
                continue

            if state.block_type == "tool_use":
                # 工具调用块结束 → 拼完整的 JSON，产生 TOOL_USE 事件
                assistant_events.append(AssistantEvent(
                    event_type=AssistantEventType.TOOL_USE,
                    tool_id=state.tool_id,
                    tool_name=state.tool_name,
                    tool_input=state.partial_json,
                ))

        elif event_type == "message_delta":
            # 消息级别更新（包含 stop_reason 和 usage）
            usage = event.data.get("usage", {})
            if usage:
                assistant_events.append(AssistantEvent(
                    event_type=AssistantEventType.USAGE,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                ))

        elif event_type == "message_stop":
            # 消息结束
            assistant_events.append(AssistantEvent(
                event_type=AssistantEventType.MESSAGE_STOP,
            ))

    return assistant_events


# ============================================================
# 第五步：build_assistant_message — 从事件流构建完整消息
# ============================================================
# 这是 Agentic Loop 中的关键函数。
# 它把 AssistantEvent 列表组装成一条完整的 AI 消息。

@dataclass
class ContentBlock:
    """消息中的内容块"""
    block_type: str  # "text" or "tool_use"
    text: str = ""
    tool_id: str = ""
    tool_name: str = ""
    tool_input: str = ""


@dataclass
class AssistantMessage:
    """完整的 AI 回复消息"""
    blocks: list[ContentBlock]
    input_tokens: int = 0
    output_tokens: int = 0


def build_assistant_message(events: list[AssistantEvent]) -> AssistantMessage:
    """
    把 AssistantEvent 列表组装成一条完整的 AI 消息。

    逻辑：
    1. 累积 text_delta → 合并成一个 TextBlock
    2. 遇到 tool_use → 先把之前累积的文本存起来，再存工具调用
    3. 记录 usage
    4. 检查是否收到 message_stop

    对应源码: conversation.rs:353-390 (build_assistant_message)
    """
    blocks = []
    current_text = ""  # 正在累积的文本
    input_tokens = 0
    output_tokens = 0
    finished = False

    for event in events:
        if event.event_type == AssistantEventType.TEXT_DELTA:
            current_text += event.text

        elif event.event_type == AssistantEventType.TOOL_USE:
            # 遇到工具调用 → 先把之前的文本存起来
            if current_text:
                blocks.append(ContentBlock(block_type="text", text=current_text))
                current_text = ""
            # 再存工具调用
            blocks.append(ContentBlock(
                block_type="tool_use",
                tool_id=event.tool_id,
                tool_name=event.tool_name,
                tool_input=event.tool_input,
            ))

        elif event.event_type == AssistantEventType.USAGE:
            input_tokens = event.input_tokens
            output_tokens = event.output_tokens

        elif event.event_type == AssistantEventType.MESSAGE_STOP:
            finished = True

    # 把最后累积的文本也存起来
    if current_text:
        blocks.append(ContentBlock(block_type="text", text=current_text))

    if not finished:
        print("  [WARNING] 流结束了但没收到 message_stop 事件！")

    return AssistantMessage(
        blocks=blocks,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


# ============================================================
# 第六步：模拟完整的流式对话
# ============================================================

def simulate_sse_stream(scenario: str) -> list[str]:
    """
    模拟服务器发送的 SSE 数据块。

    真实情况下这些是通过 HTTP 连接一块一块到达的。
    我们用字符串列表来模拟"一块一块收到的数据"。
    """
    if scenario == "text_only":
        # 场景 1：AI 只回复文本 "Hello World!"
        # 注意：数据被切成了不规则的块！（模拟网络分片）
        return [
            # --- 第一块：包含 message_start 和 content_block_start ---
            (
                'event: message_start\n'
                'data: {"type":"message_start","message":{"id":"msg_01","type":"message","role":"assistant","model":"claude-opus-4-6","content":[],"usage":{"input_tokens":25,"output_tokens":1}}}\n'
                '\n'
                'event: content_block_start\n'
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                '\n'
            ),
            # --- 第二块：文本增量（被切成两块！）---
            (
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel'
            ),
            # --- 第三块：第二块的剩余部分 + 更多文本 ---
            (
                'lo"}}\n'
                '\n'
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" World!"}}\n'
                '\n'
            ),
            # --- 第四块：结束事件 ---
            (
                'event: content_block_stop\n'
                'data: {"type":"content_block_stop","index":0}\n'
                '\n'
                'event: message_delta\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":25,"output_tokens":10}}\n'
                '\n'
                'event: message_stop\n'
                'data: {"type":"message_stop"}\n'
                '\n'
                'data: [DONE]\n'
                '\n'
            ),
        ]

    elif scenario == "tool_use":
        # 场景 2：AI 回复文本 + 调用工具
        return [
            # message_start + text block
            (
                'event: message_start\n'
                'data: {"type":"message_start","message":{"id":"msg_02","type":"message","role":"assistant","model":"claude-opus-4-6","content":[],"usage":{"input_tokens":50,"output_tokens":1}}}\n'
                '\n'
                'event: content_block_start\n'
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                '\n'
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Let me check the files."}}\n'
                '\n'
                'event: content_block_stop\n'
                'data: {"type":"content_block_stop","index":0}\n'
                '\n'
            ),
            # tool_use block（JSON 被切成多个 delta！）
            (
                'event: content_block_start\n'
                'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_01","name":"bash"}}\n'
                '\n'
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"com"}}\n'
                '\n'
            ),
            # 更多 JSON 碎片
            (
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"mand\\": \\"ls"}}\n'
                '\n'
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":" -la\\"}"}}\n'
                '\n'
                'event: content_block_stop\n'
                'data: {"type":"content_block_stop","index":1}\n'
                '\n'
            ),
            # 结束
            (
                'event: message_delta\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"input_tokens":50,"output_tokens":30}}\n'
                '\n'
                'event: message_stop\n'
                'data: {"type":"message_stop"}\n'
                '\n'
                'data: [DONE]\n'
                '\n'
            ),
        ]

    elif scenario == "with_ping":
        # 场景 3：包含 ping 心跳和注释
        return [
            (
                ': keepalive comment\n'
                'event: ping\n'
                'data: {"type":"ping"}\n'
                '\n'
            ),
            (
                'event: message_start\n'
                'data: {"type":"message_start","message":{"id":"msg_03","type":"message","role":"assistant","model":"claude-opus-4-6","content":[],"usage":{"input_tokens":10,"output_tokens":1}}}\n'
                '\n'
                'event: content_block_start\n'
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n'
                '\n'
                ': another comment\n'
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi!"}}\n'
                '\n'
                'event: content_block_stop\n'
                'data: {"type":"content_block_stop","index":0}\n'
                '\n'
                'event: message_delta\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":10,"output_tokens":3}}\n'
                '\n'
                'event: message_stop\n'
                'data: {"type":"message_stop"}\n'
                '\n'
                'data: [DONE]\n'
                '\n'
            ),
        ]

    return []


def run_streaming_demo(scenario: str, label: str):
    """
    模拟流式接收和解析的完整过程。

    这就是 Claude Code 内部做的事情：
    1. 收到 HTTP 数据块
    2. SSE 解析器提取事件
    3. 转换为 StreamEvent
    4. 组装为 AssistantEvent
    5. 构建完整消息
    """
    print(f"\n--- {label} ---")
    chunks = simulate_sse_stream(scenario)

    # 第 1 层：SSE 解析器（处理网络分片）
    parser = SseParser()
    all_stream_events = []

    print(f"\n  [网络层] 收到 {len(chunks)} 个数据块")
    for i, chunk in enumerate(chunks):
        # 模拟网络延迟
        time.sleep(0.1)

        # 推入数据块，提取 SSE 事件
        sse_events = parser.push_chunk(chunk)
        print(f"  [网络层] 块 {i+1}: {len(chunk)} 字节 → 解析出 {len(sse_events)} 个 SSE 事件")

        # 第 2 层：把 SSE 事件转换为 StreamEvent
        for sse_event in sse_events:
            stream_event = parse_stream_event(sse_event)
            if stream_event:
                all_stream_events.append(stream_event)
                # 实时输出（模拟打字效果）
                if stream_event.event_type == "content_block_delta":
                    delta = stream_event.data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        print(f"  [实时输出] ← \"{delta['text']}\"")
                    elif delta.get("type") == "input_json_delta":
                        print(f"  [JSON碎片] ← \"{delta['partial_json']}\"")

    # 处理流末尾
    remaining = parser.finish()
    for sse_event in remaining:
        stream_event = parse_stream_event(sse_event)
        if stream_event:
            all_stream_events.append(stream_event)

    print(f"\n  [解析层] 共解析出 {len(all_stream_events)} 个 StreamEvent:")
    for event in all_stream_events:
        print(f"    {event.event_type}")

    # 第 3 层：组装为 AssistantEvent
    assistant_events = process_stream_events(all_stream_events)

    # 第 4 层：构建完整消息
    message = build_assistant_message(assistant_events)

    print(f"\n  [组装层] 最终消息包含 {len(message.blocks)} 个内容块:")
    for block in message.blocks:
        if block.block_type == "text":
            print(f"    [Text] \"{block.text}\"")
        elif block.block_type == "tool_use":
            print(f"    [ToolUse] {block.tool_name}({block.tool_input})")
    print(f"  [Token] input={message.input_tokens}, output={message.output_tokens}")


# ============================================================
# 第七步：演示
# ============================================================

def main():
    print("=" * 60)
    print("Tutorial 10: SSE Streaming 流式输出演示")
    print("=" * 60)

    # --- 1. SSE 格式基础 ---
    print("\n" + "=" * 40)
    print("Part 1: SSE 格式基础")
    print("=" * 40)

    raw_sse_text = (
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n'
        '\n'
    )
    print(f"\n  原始 SSE 文本:")
    for line in raw_sse_text.split("\n"):
        if line:
            print(f"    {line}")
        else:
            print(f"    (空行 = 事件结束标记)")

    parser = SseParser()
    events = parser.push_chunk(raw_sse_text)
    print(f"\n  解析结果:")
    for event in events:
        print(f"    event = {event.event}")
        print(f"    data  = {event.data}")

    # --- 2. 网络分片处理 ---
    print("\n" + "=" * 40)
    print("Part 2: 网络分片处理")
    print("=" * 40)
    print("\n  同一个事件被切成两块网络数据：")

    parser2 = SseParser()

    chunk1 = 'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_del'
    chunk2 = 'ta","text":"Hello"}}\n\n'

    print(f"    块1: \"{chunk1[:40]}...\" ({len(chunk1)} 字节)")
    events1 = parser2.push_chunk(chunk1)
    print(f"    → 解析出 {len(events1)} 个事件 (数据不完整，继续等)")

    print(f"    块2: \"{chunk2[:40]}...\" ({len(chunk2)} 字节)")
    events2 = parser2.push_chunk(chunk2)
    print(f"    → 解析出 {len(events2)} 个事件 (凑齐了！)")

    if events2:
        parsed = json.loads(events2[0].data)
        print(f"    → 拼出的文本: \"{parsed['delta']['text']}\"")

    # --- 3. 完整的流式场景 ---
    print("\n" + "=" * 40)
    print("Part 3: 完整的流式场景演示")
    print("=" * 40)

    run_streaming_demo("text_only", "场景 A: 纯文本回复")
    run_streaming_demo("tool_use", "场景 B: 文本 + 工具调用")
    run_streaming_demo("with_ping", "场景 C: 包含 ping 心跳和注释")

    # --- 4. 数据流全景图 ---
    print("\n" + "=" * 60)
    print("数据流全景图：从 HTTP 到 Agentic Loop")
    print("=" * 60)
    print("""
    HTTP 响应（字节流）
        │
        ▼
    ┌─────────────────────────────┐
    │  SseParser (增量解析器)       │ ← 处理网络分片
    │  - 缓冲区 buffer             │    把不规则的数据块
    │  - 找 \\n\\n 事件边界           │    拼成完整的 SSE 事件
    │  - 解析 event:/data:/id:     │
    └─────────────────────────────┘
        │ SseEvent (event, data)
        ▼
    ┌─────────────────────────────┐
    │  parse_stream_event()        │ ← 过滤 + JSON 解析
    │  - 跳过 ping, [DONE], 注释   │    把 data 字符串解析
    │  - JSON.parse(data)          │    成结构化数据
    └─────────────────────────────┘
        │ StreamEvent (type, data)
        ▼
    ┌─────────────────────────────┐
    │  process_stream_events()     │ ← 状态机
    │  - 跟踪 content_block 状态   │    累积 text_delta
    │  - 累积 text / json 碎片     │    拼接 json 碎片
    │  - 产生 TEXT_DELTA, TOOL_USE │
    └─────────────────────────────┘
        │ AssistantEvent
        ▼
    ┌─────────────────────────────┐
    │  build_assistant_message()   │ ← 组装器
    │  - 合并连续 text_delta       │    碎片 → 完整消息
    │  - 收集 tool_use             │
    │  - 记录 usage                │
    └─────────────────────────────┘
        │ AssistantMessage (blocks, usage)
        ▼
    ┌─────────────────────────────┐
    │  Agentic Loop (run_turn)     │
    │  - 文本块 → 显示给用户        │
    │  - 工具块 → 执行工具 → 继续   │
    └─────────────────────────────┘
    """)

    # 解说
    print("=" * 60)
    print("关键理解要点：")
    print("=" * 60)
    print("""
    1. SSE 格式非常简单:
       - 每个事件由 event: 和 data: 行组成
       - 空行(\\n\\n) 分隔不同事件
       - 以 : 开头是注释，ping 是心跳，[DONE] 是结束

    2. 增量解析是核心难点:
       网络数据不按事件边界切分！解析器要维护缓冲区，
       把不完整的数据块拼成完整的事件。
       就像听不稳定的收音机，要自己在脑中拼凑完整句子。

    3. Claude API 的 6 种 StreamEvent:
       message_start → content_block_start → content_block_delta(多次)
       → content_block_stop → message_delta → message_stop
       就像一本书: 开始写书 → 开始写章 → 写内容(多段)
       → 章结束 → 书的元信息 → 书写完了

    4. 工具调用的 JSON 也是流式的:
       input_json_delta 一小段一小段来: '{"com' + 'mand' + '": "ls"}'
       必须等 content_block_stop 后才能拼出完整 JSON。

    5. 四层解析管线:
       HTTP字节 → SseEvent → StreamEvent → AssistantEvent → AssistantMessage
       每一层都在做"提纯"：去噪声、结构化、状态跟踪、组装。

    6. 实时显示的秘密:
       text_delta 一到就可以立刻显示给用户（打字机效果），
       不用等整个回复完成。这就是流式体验好的原因！

    对应 Claude Code 源码:
    - api/sse.rs:5-61         → SseParser (帧级解析)
    - runtime/sse.rs:12-97    → IncrementalSseParser (行级解析)
    - api/types.rs:203-212    → StreamEvent 枚举
    - api/client.rs:524-563   → MessageStream (异步流式消费)
    - conversation.rs:23-32   → AssistantEvent
    - conversation.rs:353-390 → build_assistant_message()
    """)


if __name__ == "__main__":
    main()
