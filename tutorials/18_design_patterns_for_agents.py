"""
教程 18: 面向 Agent 系统的设计模式
=================================

来源: conversation.rs, permissions.rs, prompt.rs 中的真实架构
目标: 用 Python 重现 Claude Code 使用的 5 种核心设计模式
前置: 你只需要会 Python 的 class / 函数 / 字典

为什么需要设计模式?
─────────────────
想象你在搭乐高。如果所有零件粘死在一起, 你就只能造出一种东西。
设计模式就是 "零件之间怎么连接" 的标准方法——让你能拆、能换、能扩展。

Claude Code 的架构中, 5 种模式反复出现:
  1. 接口 (ABC/Protocol) — "合同": 定义能做什么, 不管怎么做
  2. 依赖注入 — "插座": 从外部传入实现, 不在内部写死
  3. Builder 模式 — "点菜单": 一步步组装复杂对象
  4. 注册表模式 — "电话簿": 按名字查找处理函数
  5. 策略模式 — "换挡": 运行时切换行为

下面每个模式都: 先讲日常类比 → 对应源码 → 可运行 Python demo
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Protocol


# ============================================================
# 第一课: 接口 (ABC / Protocol) — "签合同, 不管你怎么干活"
# ============================================================

def lesson_1_interface():
    """
    对应源码:
      conversation.rs:34-36  → trait ApiClient { fn stream(...) }
      conversation.rs:38-39  → trait ToolExecutor { fn execute(...) }
      permissions.rs:39-41   → trait PermissionPrompter { fn decide(...) }

    这三个 trait (Rust 的叫法) 就是 Python 的 ABC (Abstract Base Class)。
    它们定义了 "你必须实现这些方法", 但不关心你怎么实现。

    为什么? 因为 ConversationRuntime 不应该知道:
    - API 调用是走 HTTP 还是假数据 (测试时)
    - 工具是执行 Bash 还是读文件
    - 权限提示是 CLI 弹窗还是 IDE 对话框

    日常类比: USB 接口
    ─────────────
    USB 口不管你插的是鼠标、键盘还是 U盘——只要你有 USB 插头就行。
    "USB 接口" = trait/ABC, "鼠标" = 一个具体实现。
    """
    print("=" * 60)
    print("第一课: 接口 (ABC) — 签合同, 不管你怎么干活")
    print("=" * 60)

    # ---- 步骤 1: 定义接口 (对应 Rust 的 trait) ----

    # 对应 conversation.rs:34-36
    # pub trait ApiClient {
    #     fn stream(&mut self, request: ApiRequest) -> Result<Vec<AssistantEvent>, RuntimeError>;
    # }
    class ApiClient(ABC):
        """API 客户端接口: 你必须能发送请求并返回事件流"""
        @abstractmethod
        def stream(self, messages: list[dict]) -> list[dict]:
            """发送消息, 返回 assistant 事件列表"""
            ...

    # 对应 conversation.rs:38-39
    # pub trait ToolExecutor {
    #     fn execute(&mut self, tool_name: &str, input: &str) -> Result<String, ToolError>;
    # }
    class ToolExecutor(ABC):
        """工具执行器接口: 你必须能按名字执行工具"""
        @abstractmethod
        def execute(self, tool_name: str, tool_input: str) -> str:
            ...

    # 对应 permissions.rs:39-41
    # pub trait PermissionPrompter {
    #     fn decide(&mut self, request: &PermissionRequest) -> PermissionPromptDecision;
    # }
    class PermissionPrompter(ABC):
        """权限提示器接口: 你必须能做出允许/拒绝决定"""
        @abstractmethod
        def decide(self, tool_name: str, tool_input: str) -> bool:
            ...

    # ---- 步骤 2: 多种实现 ----

    # 实现 1: 真正调 API (生产环境)
    class RealApiClient(ApiClient):
        def stream(self, messages):
            # 真实场景: 发 HTTP 请求到 Anthropic API
            return [{"type": "text", "text": f"我收到了 {len(messages)} 条消息"}]

    # 实现 2: 假数据 (测试环境)
    class MockApiClient(ApiClient):
        def __init__(self, fixed_response: str):
            self.fixed_response = fixed_response

        def stream(self, messages):
            return [{"type": "text", "text": self.fixed_response}]

    # 实现 3: 记录所有请求 (调试环境)
    class LoggingApiClient(ApiClient):
        def __init__(self, inner: ApiClient):
            self.inner = inner
            self.log: list[dict] = []

        def stream(self, messages):
            self.log.append({"messages": messages})
            result = self.inner.stream(messages)
            self.log[-1]["response"] = result
            return result

    # ---- 步骤 3: 使用——完全相同的代码, 不同的行为 ----

    def run_agent(client: ApiClient):
        """这个函数不知道也不关心 client 是真的还是假的"""
        events = client.stream([{"role": "user", "content": "hello"}])
        return events[0]["text"]

    # 生产
    real = RealApiClient()
    print(f"  生产环境: {run_agent(real)}")

    # 测试
    mock = MockApiClient("固定回答")
    print(f"  测试环境: {run_agent(mock)}")

    # 调试 (套娃: Logging 包裹 Real)
    logging = LoggingApiClient(RealApiClient())
    print(f"  调试环境: {run_agent(logging)}")
    print(f"  日志记录: {len(logging.log)} 条请求")

    # ---- Python 的另一种写法: Protocol (鸭子类型) ----

    print()
    print("    Python 特有: Protocol (鸭子类型)")
    print("    ─────────────────────────────────")
    print("    ABC 要求你显式继承 (class Foo(ABC): ...)")
    print("    Protocol 只要求你有对应方法——不需要继承!")
    print("    这更接近 Python 的哲学: '像鸭子就是鸭子'")
    print()

    # Protocol 版本——不需要继承, 只要有 stream 方法就行
    class ApiClientProtocol(Protocol):
        def stream(self, messages: list[dict]) -> list[dict]: ...

    # 这个类没有继承任何东西, 但它有 stream 方法, 所以它"是" ApiClient
    class SimpleClient:
        def stream(self, messages):
            return [{"type": "text", "text": "我没继承任何类!"}]

    # 可以直接传给 run_agent, 因为 Python 不检查继承关系
    simple = SimpleClient()
    print(f"  Protocol版: {run_agent(simple)}")

    print()
    print("    ABC vs Protocol 怎么选?")
    print("    ─────────────────────────")
    print("    ABC:      强制继承, 忘了实现方法 → 立刻报错 (更安全)")
    print("    Protocol: 鸭子类型, 更灵活, 适合测试和简单场景")
    print("    Claude Code 的 Rust trait 更像 ABC (编译时检查)")
    print("    mini-claude-code 建议: 核心接口用 ABC, 小工具用 Protocol")
    print()


# ============================================================
# 第二课: 依赖注入 — "插座: 从外部传入, 不在内部写死"
# ============================================================

def lesson_2_dependency_injection():
    """
    对应源码:
      conversation.rs:100-110
        pub struct ConversationRuntime<C, T> {
            api_client: C,          ← 从外部传入
            tool_executor: T,       ← 从外部传入
            permission_policy: ..., ← 从外部传入
            hook_runner: ...,       ← 从外部传入
        }

      112-116:
        impl<C, T> ConversationRuntime<C, T>
        where
            C: ApiClient,       ← 只要求实现 ApiClient 接口
            T: ToolExecutor,    ← 只要求实现 ToolExecutor 接口

    "依赖注入" 听起来很可怕, 其实就是:
    不要在内部 new, 而是从外部传进来。

    日常类比: 手机壳
    ─────────────
    手机不会在出厂时焊死一个壳。
    你从外面套上去——想换透明的? 硅胶的? 皮革的? 随便换。
    "手机" = ConversationRuntime, "手机壳" = ApiClient/ToolExecutor。
    """
    print("=" * 60)
    print("第二课: 依赖注入 — 插座: 从外部传入, 不在内部写死")
    print("=" * 60)

    # ---- 反面教材: 写死依赖 (不要这样!) ----
    print()
    print("  反面教材 (耦合):")
    print("  ──────────────")

    class BadRuntime:
        """糟糕的设计: API 客户端在内部写死"""
        def __init__(self):
            # 问题: 测试时怎么办? 没网络怎么办? 想换 API 怎么办?
            import urllib.request  # noqa: F401 — 演示"写死"
            self.api_url = "https://api.anthropic.com/v1/messages"

        def run(self, user_input: str):
            # 这里直接调 API——无法测试、无法替换
            pass

    print("    class BadRuntime:")
    print("        def __init__(self):")
    print("            self.api_url = 'https://api.anthropic.com/...'  # 写死!")
    print("    问题: 测试? 没网? 换服务? 全部改代码!")
    print()

    # ---- 正面教材: 依赖注入 ----
    print("  正面教材 (注入):")
    print("  ──────────────")

    class ApiClient(ABC):
        @abstractmethod
        def stream(self, messages: list) -> list:
            ...

    class ToolExecutor(ABC):
        @abstractmethod
        def execute(self, name: str, inp: str) -> str:
            ...

    # 对应 conversation.rs:100-110 的 ConversationRuntime<C, T>
    class ConversationRuntime:
        """
        注意: __init__ 不创建任何依赖, 全部从外部接收。
        这就是依赖注入——"把依赖注入进来, 而不是自己造"。
        """
        def __init__(
            self,
            api_client: ApiClient,        # 从外部传入
            tool_executor: ToolExecutor,   # 从外部传入
            max_iterations: int = 10,
        ):
            self.api_client = api_client
            self.tool_executor = tool_executor
            self.max_iterations = max_iterations
            self.messages: list[dict] = []

        def run_turn(self, user_input: str) -> str:
            """对应 conversation.rs:170-283 的 run_turn"""
            self.messages.append({"role": "user", "content": user_input})

            for i in range(self.max_iterations):
                events = self.api_client.stream(self.messages)
                text_parts = []
                tool_calls = []

                for event in events:
                    if event["type"] == "text":
                        text_parts.append(event["text"])
                    elif event["type"] == "tool_use":
                        tool_calls.append(event)

                self.messages.append({
                    "role": "assistant",
                    "content": "".join(text_parts),
                    "tool_calls": tool_calls,
                })

                if not tool_calls:
                    return "".join(text_parts)

                for call in tool_calls:
                    result = self.tool_executor.execute(
                        call["name"], call["input"]
                    )
                    self.messages.append({
                        "role": "tool",
                        "tool_use_id": call["id"],
                        "content": result,
                    })

            return "达到最大迭代次数"

    # ---- 演示: 同一个 Runtime, 不同的 "插头" ----

    class MockApi(ApiClient):
        def __init__(self, responses):
            self.responses = iter(responses)
        def stream(self, messages):
            return next(self.responses)

    class PrintToolExecutor(ToolExecutor):
        def execute(self, name, inp):
            return f"[{name}] 执行了: {inp}"

    # 场景 1: 纯文本回答 (无工具调用)
    runtime = ConversationRuntime(
        api_client=MockApi([
            [{"type": "text", "text": "你好, 我是 Claude!"}]
        ]),
        tool_executor=PrintToolExecutor(),
    )
    print(f"    场景1 (纯文本): {runtime.run_turn('hello')}")

    # 场景 2: 一次工具调用 → 一次回答
    runtime2 = ConversationRuntime(
        api_client=MockApi([
            # 第一轮: 调工具
            [{"type": "tool_use", "id": "t1", "name": "bash", "input": "ls"}],
            # 第二轮: 用工具结果生成回答
            [{"type": "text", "text": "目录下有 3 个文件"}],
        ]),
        tool_executor=PrintToolExecutor(),
    )
    print(f"    场景2 (带工具): {runtime2.run_turn('列出文件')}")

    print()
    print("    关键洞察:")
    print("    ─────────")
    print("    ConversationRuntime 的 run_turn() 逻辑完全不变!")
    print("    变化的只有 '插进来' 的 ApiClient 和 ToolExecutor。")
    print("    这就是为什么 Claude Code 能在测试中用 MockApiClient,")
    print("    在生产中用 RealApiClient——核心逻辑一行都不用改。")
    print()


# ============================================================
# 第三课: Builder 模式 — "点菜单: 一步步组装复杂对象"
# ============================================================

def lesson_3_builder_pattern():
    """
    对应源码:
      prompt.rs:85-93  → SystemPromptBuilder 结构体
      prompt.rs:95-156 → with_*() 链式调用 + build() 方法

      conversation.rs:429-447 → StaticToolExecutor 的 register() 链式调用
      permissions.rs:64-73    → PermissionPolicy 的 with_tool_requirement()

    问题: 有些对象的参数太多了, 全放构造函数里很痛苦。
    比如 SystemPrompt 需要: OS信息、风格、项目上下文、配置、自定义段落...
    如果写成: SystemPrompt(os, style, ctx, config, sections, ...) — 噩梦!

    Builder 模式: 链式调用, 需要什么加什么, 最后 .build()。

    日常类比: 赛百味点三明治
    ────────────────────
    你不会一口气说 "我要全麦面包白面包火鸡肉双倍芝士没有酸黄瓜加墨西哥辣椒"。
    你是一步步选:
      面包 → 全麦
      肉   → 火鸡
      芝士 → 双倍
      去掉 → 酸黄瓜
      加上 → 墨西哥辣椒
    最后说 "好了, 做吧!" → .build()
    """
    print("=" * 60)
    print("第三课: Builder 模式 — 点菜单: 一步步组装复杂对象")
    print("=" * 60)

    # ---- 反面教材: 参数爆炸 ----
    print()
    print("  反面教材 (参数爆炸):")
    print("  ──────────────────")
    print("    SystemPrompt(")
    print("        os_name='macOS',")
    print("        os_version='15.0',")
    print("        style_name='concise',")
    print("        style_prompt='Be brief',")
    print("        cwd='/home/user',")
    print("        date='2024-01-01',")
    print("        git_status='...',")
    print("        config=...,")
    print("        extra_sections=[...],")
    print("    )")
    print("    12 个参数! 大部分可选, 顺序容易搞错。")
    print()

    # ---- 正面教材: Builder 模式 ----

    # 对应 prompt.rs:85-93 的 SystemPromptBuilder
    @dataclass
    class SystemPromptBuilder:
        """
        Builder 模式三要素:
        1. 字段全部可选 (None 默认值)
        2. with_*() 方法返回 self (链式调用)
        3. build() 方法组装最终结果
        """
        # 所有字段可选 — 对应 prompt.rs:86-92 的 Option<String>
        _os_name: str | None = None
        _os_version: str | None = None
        _style_name: str | None = None
        _style_prompt: str | None = None
        _project_cwd: str | None = None
        _current_date: str | None = None
        _extra_sections: list[str] = field(default_factory=list)

        # 对应 prompt.rs:108-113
        # pub fn with_os(mut self, name, version) -> Self { ... self }
        def with_os(self, name: str, version: str) -> SystemPromptBuilder:
            self._os_name = name
            self._os_version = version
            return self  # 返回 self → 支持链式调用

        # 对应 prompt.rs:101-106
        def with_style(self, name: str, prompt: str) -> SystemPromptBuilder:
            self._style_name = name
            self._style_prompt = prompt
            return self

        def with_project(self, cwd: str, date: str) -> SystemPromptBuilder:
            self._project_cwd = cwd
            self._current_date = date
            return self

        # 对应 prompt.rs:128-131 的 append_section
        def append_section(self, section: str) -> SystemPromptBuilder:
            self._extra_sections.append(section)
            return self

        # 对应 prompt.rs:134-156 的 build()
        def build(self) -> list[str]:
            """组装最终的 system prompt 段落列表"""
            sections = []

            # 固定段落 (对应 get_simple_intro_section 等)
            sections.append("You are Claude, an AI assistant by Anthropic.")

            # 可选: 输出风格
            if self._style_name and self._style_prompt:
                sections.append(f"# Output Style: {self._style_name}\n{self._style_prompt}")

            sections.append("# System\n- Follow instructions carefully.")
            sections.append("# Doing tasks\n- Complete tasks step by step.")

            # 动态分界线 (对应 SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
            # 分界线以上是静态内容 (API 缓存), 以下是动态内容 (每次不同)
            sections.append("__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__")

            # 可选: 环境信息
            if self._os_name:
                sections.append(
                    f"# Environment\n"
                    f"- Platform: {self._os_name} {self._os_version}\n"
                    f"- CWD: {self._project_cwd or 'unknown'}\n"
                    f"- Date: {self._current_date or 'unknown'}"
                )

            # 自定义段落
            sections.extend(self._extra_sections)

            return sections

    # ---- 使用 Builder ----
    print("  正面教材 (Builder 链式调用):")
    print("  ─────────────────────────")

    # 对应源码中的用法:
    # SystemPromptBuilder::new()
    #     .with_os("darwin", "15.0")
    #     .with_output_style("concise", "Be brief")
    #     .with_project_context(ctx)
    #     .build()

    prompt_sections = (
        SystemPromptBuilder()
        .with_os("macOS", "15.0")
        .with_style("concise", "Be brief and direct.")
        .with_project("~/my-project", "2024-06-01")
        .append_section("# Custom\nUser prefers Chinese.")
        .build()
    )

    for i, section in enumerate(prompt_sections):
        preview = section[:60].replace("\n", " | ")
        print(f"    段落 {i}: {preview}...")

    # ---- Claude Code 中还有两个 Builder ----
    print()
    print("  Claude Code 中的三个 Builder:")
    print("  ────────────────────────────")

    # Builder 2: StaticToolExecutor (conversation.rs:429-447)
    print()
    print("  Builder 2: StaticToolExecutor (工具注册)")

    class StaticToolExecutor:
        """对应 conversation.rs:429-447"""
        def __init__(self):
            self._handlers: dict[str, Callable] = {}

        def register(self, name: str, handler: Callable) -> StaticToolExecutor:
            """对应 .register(tool_name, handler) -> Self"""
            self._handlers[name] = handler
            return self  # 链式调用

        def execute(self, name: str, inp: str) -> str:
            if name not in self._handlers:
                raise KeyError(f"unknown tool: {name}")
            return self._handlers[name](inp)

    executor = (
        StaticToolExecutor()
        .register("bash", lambda inp: f"$ {inp}")
        .register("read", lambda inp: f"读取: {inp}")
        .register("write", lambda inp: f"写入: {inp}")
    )

    print(f"    bash 工具: {executor.execute('bash', 'ls -la')}")
    print(f"    read 工具: {executor.execute('read', 'main.py')}")

    # Builder 3: PermissionPolicy (permissions.rs:64-73)
    print()
    print("  Builder 3: PermissionPolicy (权限策略)")

    class PermissionMode(IntEnum):
        ReadOnly = 0
        WorkspaceWrite = 1
        DangerFullAccess = 2
        Prompt = 3
        Allow = 4

    class PermissionPolicy:
        """对应 permissions.rs:49-73"""
        def __init__(self, active_mode: PermissionMode):
            self._active_mode = active_mode
            self._requirements: dict[str, PermissionMode] = {}

        def with_tool_requirement(
            self, tool_name: str, required: PermissionMode
        ) -> PermissionPolicy:
            self._requirements[tool_name] = required
            return self  # 链式调用

        def is_allowed(self, tool_name: str) -> bool:
            required = self._requirements.get(tool_name, PermissionMode.DangerFullAccess)
            return self._active_mode >= required

    policy = (
        PermissionPolicy(PermissionMode.WorkspaceWrite)
        .with_tool_requirement("read", PermissionMode.ReadOnly)
        .with_tool_requirement("write", PermissionMode.WorkspaceWrite)
        .with_tool_requirement("bash", PermissionMode.DangerFullAccess)
    )

    print(f"    当前模式: WorkspaceWrite")
    print(f"    read  → 允许? {policy.is_allowed('read')}")    # True
    print(f"    write → 允许? {policy.is_allowed('write')}")   # True
    print(f"    bash  → 允许? {policy.is_allowed('bash')}")    # False (需要 DangerFullAccess)

    print()
    print("    Builder 模式速记:")
    print("    ─────────────────")
    print("    1. 字段全部可选 (Option / None)")
    print("    2. with_*() 返回 self (链式调用)")
    print("    3. build() 组装最终产物")
    print("    4. 调用者只设置自己需要的——其余用默认值")
    print()


# ============================================================
# 第四课: 注册表模式 — "电话簿: 按名字查找处理函数"
# ============================================================

def lesson_4_registry_pattern():
    """
    对应源码:
      conversation.rs:429-456 → StaticToolExecutor
        handlers: BTreeMap<String, ToolHandler>   ← "名字 → 函数" 的映射
        fn execute(&mut self, tool_name: &str, input: &str) → 按名字查找并调用

    Agent 系统有很多工具 (bash, read, write, grep, glob, ...),
    每个工具的执行逻辑不同。怎么组织?

    方案 A (if/elif 地狱):
      if tool == "bash": do_bash()
      elif tool == "read": do_read()
      elif tool == "write": do_write()
      elif ... (50 个 elif)
      → 每加一个工具就要改这个 if 链, 容易出错

    方案 B (注册表):
      registry["bash"] = do_bash
      registry["read"] = do_read
      result = registry[tool](input)
      → 加工具只需 register(), 查找和执行完全解耦

    日常类比: 手机通讯录
    ────────────────────
    你不会记住所有人的电话号码。
    你把 "名字 → 号码" 存进通讯录, 需要时按名字搜索。
    注册表就是 "工具名 → 处理函数" 的通讯录。
    """
    print("=" * 60)
    print("第四课: 注册表模式 — 电话簿: 按名字查找处理函数")
    print("=" * 60)

    # ---- 反面教材 ----
    print()
    print("  反面教材 (if/elif 地狱):")
    print("  ─────────────────────")
    print("    def execute(tool, input):")
    print("        if tool == 'bash': return run_bash(input)")
    print("        elif tool == 'read': return read_file(input)")
    print("        elif tool == 'write': return write_file(input)")
    print("        elif tool == 'grep': return grep_file(input)")
    print("        elif ...  # 每加一个工具就改这里!")
    print()

    # ---- 正面教材: 注册表 ----
    print("  正面教材 (注册表):")
    print("  ────────────────")

    # 对应 conversation.rs:429-456
    class ToolRegistry:
        """
        工具注册表
        核心数据结构: dict[str, Callable]
        对应 Rust: BTreeMap<String, Box<dyn FnMut(&str) -> Result<String, ToolError>>>
        """
        def __init__(self):
            self._handlers: dict[str, Callable[[str], str]] = {}

        def register(self, name: str, handler: Callable[[str], str]):
            """注册一个工具: 名字 → 处理函数"""
            self._handlers[name] = handler

        def execute(self, name: str, tool_input: str) -> str:
            """按名字查找并执行"""
            handler = self._handlers.get(name)
            if handler is None:
                raise KeyError(f"unknown tool: {name}")
            return handler(tool_input)

        def list_tools(self) -> list[str]:
            return list(self._handlers.keys())

    # 注册工具
    registry = ToolRegistry()
    registry.register("bash", lambda inp: f"执行命令: {json.loads(inp)['command']}")
    registry.register("read", lambda inp: f"读取文件: {json.loads(inp)['path']}")
    registry.register("write", lambda inp: f"写入文件: {json.loads(inp)['path']}")

    # 模拟 agentic loop 中的工具调用
    tool_calls = [
        {"name": "read", "input": '{"path": "main.py"}'},
        {"name": "bash", "input": '{"command": "python main.py"}'},
        {"name": "write", "input": '{"path": "output.txt"}'},
    ]

    print(f"    已注册工具: {registry.list_tools()}")
    print()
    for call in tool_calls:
        result = registry.execute(call["name"], call["input"])
        print(f"    {call['name']:>8} → {result}")

    # 动态注册 (运行时添加新工具)
    print()
    print("  动态注册 (运行时添加):")
    print("  ────────────────────")
    registry.register("grep", lambda inp: f"搜索: {json.loads(inp)['pattern']}")
    print(f"    新增 grep 后: {registry.list_tools()}")
    print(f"    grep 测试: {registry.execute('grep', json.dumps({'pattern': 'TODO'}))}")

    # ---- 高级用法: 装饰器注册 ----
    print()
    print("  进阶: 装饰器自动注册")
    print("  ──────────────────")

    class DecoratorRegistry:
        """用 Python 装饰器语法自动注册"""
        def __init__(self):
            self._handlers: dict[str, Callable] = {}

        def tool(self, name: str):
            """装饰器: @registry.tool("bash")"""
            def decorator(func):
                self._handlers[name] = func
                return func
            return decorator

        def execute(self, name: str, inp: str) -> str:
            return self._handlers[name](inp)

    r = DecoratorRegistry()

    @r.tool("bash")
    def handle_bash(inp: str) -> str:
        return f"$ {json.loads(inp)['command']}"

    @r.tool("read")
    def handle_read(inp: str) -> str:
        return f"cat {json.loads(inp)['path']}"

    print(f"    @r.tool('bash') → {r.execute('bash', json.dumps({'command': 'ls'}))}")
    print(f"    @r.tool('read') → {r.execute('read', json.dumps({'path': 'a.py'}))}")

    print()
    print("    注册表 vs if/elif:")
    print("    ─────────────────")
    print("    加新工具:  register('x', fn)  vs  在 if 链里插一行")
    print("    删工具:    del handlers['x']   vs  找到 elif 删掉")
    print("    列工具:    handlers.keys()     vs  人肉读 if 链")
    print("    注册表 = 数据驱动, if/elif = 代码驱动")
    print()


# ============================================================
# 第五课: 策略模式 — "换挡: 运行时切换行为"
# ============================================================

def lesson_5_strategy_pattern():
    """
    对应源码:
      permissions.rs:4-9   → PermissionMode 枚举 (5 种策略)
      sandbox.rs           → FilesystemIsolationMode (3 种隔离策略)
      conversation.rs:170  → run_turn 中根据 permission_outcome 切换行为

    策略模式和接口很像, 但侧重点不同:
    - 接口: "你必须实现这些方法" (关注 what)
    - 策略: "在运行时选择哪种行为" (关注 when/which)

    日常类比: 导航 App 的路线选择
    ─────────────────────────────
    同样从 A 到 B:
    - 策略 1: 最短距离 → 走小路
    - 策略 2: 最快时间 → 走高速
    - 策略 3: 避开收费 → 走免费路
    算法不同, 但 "导航" 的框架完全一样。
    """
    print("=" * 60)
    print("第五课: 策略模式 — 换挡: 运行时切换行为")
    print("=" * 60)

    # ---- 例 1: 权限策略 (PermissionMode) ----
    print()
    print("  例 1: 权限策略 (对应 permissions.rs)")
    print("  ────────────────────────────────────")

    class PermissionMode(IntEnum):
        """对应 permissions.rs:4-9 的 5 级权限"""
        ReadOnly = 0           # 只能读
        WorkspaceWrite = 1     # 能在工作区写
        DangerFullAccess = 2   # 完全文件系统访问
        Prompt = 3             # 每次询问用户
        Allow = 4              # 全部允许 (YOLO 模式)

    # 策略 1: 只读模式——一切写操作被拦截
    # 策略 2: 工作区写——只能改工作区文件
    # 策略 3: 全权限——随便搞

    def check_permission(
        tool: str,
        required: PermissionMode,
        current: PermissionMode,
    ) -> str:
        """
        对应 permissions.rs:80-120 的 authorize()
        核心逻辑: current >= required → 允许
        """
        if current >= required:
            return "允许"
        else:
            return f"拒绝 (需要 {required.name}, 当前 {current.name})"

    tools = [
        ("read",  PermissionMode.ReadOnly),
        ("write", PermissionMode.WorkspaceWrite),
        ("bash",  PermissionMode.DangerFullAccess),
    ]

    for mode in [PermissionMode.ReadOnly, PermissionMode.WorkspaceWrite, PermissionMode.Allow]:
        print(f"\n    当前模式: {mode.name}")
        for tool_name, required in tools:
            result = check_permission(tool_name, required, mode)
            print(f"      {tool_name:>6} (需要 {required.name:>20}) → {result}")

    # ---- 例 2: 沙箱隔离策略 ----
    print()
    print()
    print("  例 2: 沙箱隔离策略 (对应 sandbox.rs)")
    print("  ─────────────────────────────────────")

    class IsolationStrategy(ABC):
        """不同的文件系统隔离策略"""
        @abstractmethod
        def check_path(self, path: str) -> bool:
            """返回 True = 允许访问, False = 拒绝"""
            ...
        @abstractmethod
        def name(self) -> str:
            ...

    class NoIsolation(IsolationStrategy):
        """Off 模式: 不做任何隔离"""
        def check_path(self, path): return True
        def name(self): return "Off"

    class WorkspaceOnly(IsolationStrategy):
        """WorkspaceOnly 模式: 只允许访问工作区"""
        def __init__(self, workspace: str):
            self.workspace = workspace
        def check_path(self, path):
            return path.startswith(self.workspace)
        def name(self): return "WorkspaceOnly"

    class AllowList(IsolationStrategy):
        """AllowList 模式: 只允许白名单路径"""
        def __init__(self, allowed: list[str]):
            self.allowed = allowed
        def check_path(self, path):
            return any(path.startswith(a) for a in self.allowed)
        def name(self): return "AllowList"

    # 同一组路径, 不同策略, 不同结果
    test_paths = [
        "/home/user/project/main.py",
        "/etc/passwd",
        "/tmp/cache.db",
    ]

    strategies: list[IsolationStrategy] = [
        NoIsolation(),
        WorkspaceOnly("/home/user/project"),
        AllowList(["/home/user/project", "/tmp"]),
    ]

    for strategy in strategies:
        print(f"\n    策略: {strategy.name()}")
        for path in test_paths:
            allowed = strategy.check_path(path)
            mark = "OK" if allowed else "BLOCKED"
            print(f"      {path:40} → {mark}")

    # ---- 策略在 agentic loop 中的应用 ----
    print()
    print()
    print("  策略模式在 agentic loop 中的应用:")
    print("  ─────────────────────────────────")
    print("    conversation.rs run_turn() 中:")
    print()
    print("    match permission_outcome {")
    print("        Allow => {")
    print("            // 策略 A: 执行工具")
    print("            pre_hook → execute → post_hook")
    print("        }")
    print("        Deny { reason } => {")
    print("            // 策略 B: 返回拒绝消息给模型")
    print("            tool_result(reason, is_error=true)")
    print("        }")
    print("    }")
    print()
    print("    模型收到拒绝后会调整方案——这就是 '反馈驱动' 的策略切换。")
    print("    不是 crash, 而是 graceful degradation (优雅降级)。")
    print()


# ============================================================
# 第六课: 组合大演示 — 完整的 mini-agentic-loop
# ============================================================

def lesson_6_full_demo():
    """
    把前 5 课的模式全部组合, 构建一个完整的 mini agentic loop。
    这就是 mini-claude-code 的骨架!

    对应关系:
    - ApiClient (接口)           → conversation.rs:34
    - ToolExecutor (注册表)      → conversation.rs:429
    - PermissionPolicy (策略)    → permissions.rs:49
    - SystemPromptBuilder (Builder) → prompt.rs:85
    - ConversationRuntime (依赖注入) → conversation.rs:100
    """
    print("=" * 60)
    print("第六课: 组合大演示 — 5 种模式构建 mini agentic loop")
    print("=" * 60)

    # ==== 1. 接口定义 ====

    class ApiClient(ABC):
        @abstractmethod
        def stream(self, system_prompt: list[str], messages: list[dict]) -> list[dict]:
            ...

    class ToolExecutor(ABC):
        @abstractmethod
        def execute(self, name: str, inp: str) -> str:
            ...

    class PermissionChecker(ABC):
        @abstractmethod
        def check(self, tool_name: str) -> tuple[bool, str]:
            """返回 (允许?, 原因)"""
            ...

    # ==== 2. Builder: 系统提示词 ====

    @dataclass
    class PromptBuilder:
        _sections: list[str] = field(default_factory=list)

        def add(self, section: str) -> PromptBuilder:
            self._sections.append(section)
            return self

        def build(self) -> list[str]:
            return list(self._sections)

    # ==== 3. 注册表: 工具执行器 ====

    class ToolRegistry(ToolExecutor):
        def __init__(self):
            self._handlers: dict[str, Callable[[str], str]] = {}

        def register(self, name: str, handler: Callable) -> ToolRegistry:
            self._handlers[name] = handler
            return self

        def execute(self, name: str, inp: str) -> str:
            handler = self._handlers.get(name)
            if handler is None:
                return f"ERROR: unknown tool '{name}'"
            return handler(inp)

    # ==== 4. 策略: 权限控制 ====

    class SimplePermission(PermissionChecker):
        def __init__(self, blocked_tools: list[str]):
            self.blocked = set(blocked_tools)

        def check(self, tool_name: str) -> tuple[bool, str]:
            if tool_name in self.blocked:
                return False, f"工具 '{tool_name}' 已被禁止"
            return True, "允许"

    # ==== 5. 依赖注入: Runtime ====

    class AgenticRuntime:
        """
        对应 conversation.rs:100-283 的 ConversationRuntime
        所有依赖从外部注入, 内部只有"循环"逻辑
        """
        def __init__(
            self,
            api_client: ApiClient,
            tool_executor: ToolExecutor,
            permission: PermissionChecker,
            system_prompt: list[str],
            max_iterations: int = 5,
        ):
            self.api = api_client
            self.tools = tool_executor
            self.permission = permission
            self.system_prompt = system_prompt
            self.max_iterations = max_iterations
            self.messages: list[dict] = []

        def run_turn(self, user_input: str) -> str:
            """
            完整的 agentic loop:
            1. 用户输入 → 消息列表
            2. 调 API → 得到 assistant 回复
            3. 有工具调用? → 权限检查 → 执行 → 结果回消息
            4. 没有工具调用? → 返回文本
            5. 重复 2-4 直到无工具调用或达到上限
            """
            self.messages.append({"role": "user", "content": user_input})
            log = []

            for iteration in range(1, self.max_iterations + 1):
                # 调 API
                events = self.api.stream(self.system_prompt, self.messages)

                # 解析 events
                text_parts = []
                tool_calls = []
                for event in events:
                    if event["type"] == "text":
                        text_parts.append(event["text"])
                    elif event["type"] == "tool_use":
                        tool_calls.append(event)

                assistant_text = "".join(text_parts)
                self.messages.append({
                    "role": "assistant",
                    "content": assistant_text,
                    "tool_calls": tool_calls,
                })

                # 无工具调用 → 结束
                if not tool_calls:
                    log.append(f"    [迭代 {iteration}] 纯文本回答, 循环结束")
                    for line in log:
                        print(line)
                    return assistant_text

                # 有工具调用 → 权限 + 执行
                for call in tool_calls:
                    allowed, reason = self.permission.check(call["name"])

                    if allowed:
                        result = self.tools.execute(call["name"], call["input"])
                        is_error = result.startswith("ERROR:")
                        log.append(
                            f"    [迭代 {iteration}] {call['name']}"
                            f"({call['input'][:30]}) → {result[:50]}"
                        )
                    else:
                        result = reason
                        is_error = True
                        log.append(
                            f"    [迭代 {iteration}] {call['name']} → 拒绝: {reason}"
                        )

                    self.messages.append({
                        "role": "tool",
                        "tool_use_id": call["id"],
                        "content": result,
                        "is_error": is_error,
                    })

            for line in log:
                print(line)
            return "达到最大迭代次数"

    # ==== 组装并运行! ====

    # 模拟 API: 先调工具, 再根据结果回答
    class ScriptedApi(ApiClient):
        """按脚本返回预设响应的 Mock API"""
        def __init__(self, script: list[list[dict]]):
            self._script = iter(script)
        def stream(self, system_prompt, messages):
            return next(self._script)

    # 构建系统 (所有模式都用上了!)

    # Builder: 构建 prompt
    prompt = (
        PromptBuilder()
        .add("You are a helpful coding assistant.")
        .add("Always use tools to verify your work.")
        .build()
    )

    # 注册表: 注册工具
    tools = (
        ToolRegistry()
        .register("bash", lambda inp: "main.py  test.py  README.md")
        .register("read", lambda inp: "def hello():\n    print('hello')")
    )

    # 策略: bash 被禁止
    permission = SimplePermission(blocked_tools=["bash"])

    # Mock API 脚本:
    # 轮 1: 尝试 bash ls (会被拒绝)
    # 轮 2: 改用 read (被允许)
    # 轮 3: 纯文本回答
    api = ScriptedApi([
        [{"type": "tool_use", "id": "t1", "name": "bash", "input": '{"cmd":"ls"}'}],
        [{"type": "tool_use", "id": "t2", "name": "read", "input": '{"path":"main.py"}'}],
        [{"type": "text", "text": "main.py 包含一个 hello 函数"}],
    ])

    # 依赖注入: 组装 Runtime
    runtime = AgenticRuntime(
        api_client=api,
        tool_executor=tools,
        permission=permission,
        system_prompt=prompt,
        max_iterations=5,
    )

    print()
    print("  运行 agentic loop:")
    print("  ─────────────────")
    result = runtime.run_turn("帮我看看项目里有什么文件")
    print(f"\n  最终回答: {result}")

    print()
    print("  发生了什么:")
    print("  ──────────")
    print("  1. AI 想用 bash ls → 权限策略拒绝 (bash 被禁)")
    print("  2. AI 收到拒绝, 改用 read → 权限允许, 工具注册表执行")
    print("  3. AI 用工具结果生成最终回答")
    print()
    print("  这就是 Claude Code 的核心循环!")
    print("  5 种模式各司其职:")
    print("    接口(ABC)    → 定义 ApiClient/ToolExecutor 的合同")
    print("    依赖注入      → Runtime 不创建依赖, 从外部接收")
    print("    Builder       → 链式构建 prompt 和工具注册表")
    print("    注册表        → 按名字查找工具处理函数")
    print("    策略          → 运行时权限检查, 拒绝后优雅降级")
    print()


# ============================================================
# 速查表
# ============================================================

def cheatsheet():
    print("=" * 60)
    print("速查表: 设计模式 → Claude Code 源码 → Python")
    print("=" * 60)
    print("""
    模式          Claude Code 源码                  Python 等价
    ─────────    ────────────────────────────────  ──────────────────
    接口          trait ApiClient                   ABC / Protocol
    依赖注入      ConversationRuntime<C, T>         __init__(client, executor)
    Builder       SystemPromptBuilder.with_*()      方法返回 self 链式调用
    注册表        StaticToolExecutor.register()     dict[str, Callable]
    策略          PermissionMode 枚举               IntEnum + >= 比较

    mini-claude-code 中的应用:
    ─────────────────────────
    ApiClient(ABC)         → 实现 stream() 调 Claude API
    ToolExecutor(ABC)      → 注册 bash/read/write/grep 等工具
    PermissionChecker      → 根据模式决定允许/拒绝/询问
    PromptBuilder          → 组装 system prompt (静态+动态)
    AgenticRuntime         → 注入以上所有, run_turn() 循环

    何时用哪个:
    ──────────
    "同一个动作, 多种实现"   → 接口 + 策略
    "对象太复杂, 参数太多"   → Builder
    "按名字找处理函数"       → 注册表
    "内部不该知道外部实现"   → 依赖注入
    """)


# ============================================================
# 主程序
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("教程 18: 面向 Agent 系统的设计模式")
    print("从 Claude Code 源码中提取的 5 种核心模式")
    print("=" * 60)

    lesson_1_interface()
    lesson_2_dependency_injection()
    lesson_3_builder_pattern()
    lesson_4_registry_pattern()
    lesson_5_strategy_pattern()
    lesson_6_full_demo()
    cheatsheet()
