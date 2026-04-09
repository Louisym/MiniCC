"""
教程 15: 权限与 Hook 流水线深度剖析
================================================================
源码对照:
  - rust/crates/runtime/src/permissions.rs (权限系统)
  - rust/crates/runtime/src/hooks.rs (Hook 系统)
  - rust/crates/runtime/src/conversation.rs (集成点)
  - reference/07-permission-pipeline.md (TypeScript 完整版)

核心问题: 当 AI 想执行 rm -rf / 时，谁来拦住它？
答案不是一个 if 语句，而是一个七步流水线，
其中四步是"绕过免疫"的——即使在最高权限模式下也会执行。
================================================================
"""

import os
import sys
import json
import subprocess
import tempfile
from enum import Enum, IntEnum, auto
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Callable, Any, Tuple
from abc import ABC, abstractmethod


# ============================================================
# 第一部分: 权限模式层级 (permissions.rs)
# ============================================================
# 源码: rust/crates/runtime/src/permissions.rs:4-10
#
# 权限模式不是简单的 True/False，而是一个有序的层级。
# 关键设计: 用 IntEnum 实现，这样可以直接用 >= 比较。


class PermissionMode(IntEnum):
    """权限模式层级 — 源码 permissions.rs:4-10

    从最严格到最宽松:
    - ReadOnly: 只能读，不能写任何东西
    - WorkspaceWrite: 可以写工作目录内的文件
    - DangerFullAccess: 可以做任何事（包括 rm -rf /）
    - Prompt: 总是询问用户
    - Allow: 跳过所有检查（最危险）

    为什么 Prompt 比 DangerFullAccess 更"高"？
    因为 Prompt 模式的意思不是"更有权限"，而是
    "这个模式下，需要升级的操作会触发用户提示"。
    在源码中 Prompt 模式会拦截所有需要确认的操作。
    """
    READ_ONLY = 1
    WORKSPACE_WRITE = 2
    DANGER_FULL_ACCESS = 3
    PROMPT = 4
    ALLOW = 5

    def as_str(self) -> str:
        return {
            self.READ_ONLY: "read-only",
            self.WORKSPACE_WRITE: "workspace-write",
            self.DANGER_FULL_ACCESS: "danger-full-access",
            self.PROMPT: "prompt",
            self.ALLOW: "allow",
        }[self]


# ============================================================
# 第二部分: 权限请求与决策 (permissions.rs)
# ============================================================

@dataclass
class PermissionRequest:
    """权限请求 — 源码 permissions.rs:25-30

    当工具要执行时，系统会创建这个请求。
    它包含: 工具名、输入参数、当前模式、所需模式。
    """
    tool_name: str
    input: str
    current_mode: PermissionMode
    required_mode: PermissionMode


class PermissionOutcome(Enum):
    """权限决策结果"""
    ALLOW = "allow"
    DENY = "deny"


@dataclass
class PermissionResult:
    """带原因的权限结果"""
    outcome: PermissionOutcome
    reason: str = ""


class PermissionPrompter(ABC):
    """权限提示器接口 — 源码 permissions.rs:39-41

    这是一个关键的抽象: 权限系统不关心"提示"怎么实现。
    可以是终端 UI 弹窗、也可以是 API 调用、也可以是自动批准。

    在 Rust 中用 trait 实现，Python 中用抽象基类。
    这个设计让权限系统可以在 CLI、IDE 插件、无头模式下复用。
    """
    @abstractmethod
    def decide(self, request: PermissionRequest) -> PermissionResult:
        """让用户决定是否允许这个操作"""
        pass


class PermissionPolicy:
    """权限策略 — 源码 permissions.rs:50-135

    这是权限系统的核心。它管理:
    1. 当前活跃的权限模式
    2. 每个工具的所需权限级别（注册表）
    3. authorize() 决策逻辑
    """

    def __init__(self, active_mode: PermissionMode):
        self._active_mode = active_mode
        # 工具名 → 所需权限级别的映射
        # 源码 permissions.rs:53 用 BTreeMap（有序）
        self._tool_requirements: Dict[str, PermissionMode] = {}

    def with_tool_requirement(
        self, tool_name: str, required_mode: PermissionMode
    ) -> "PermissionPolicy":
        """注册工具的权限需求 — 源码 permissions.rs:64-73"""
        self._tool_requirements[tool_name] = required_mode
        return self

    def active_mode(self) -> PermissionMode:
        return self._active_mode

    def required_mode_for(self, tool_name: str) -> PermissionMode:
        """查询工具需要的权限 — 源码 permissions.rs:81-86

        默认值: DangerFullAccess
        这意味着: 如果你没注册过这个工具的权限需求，
        系统会假设它需要最高权限。安全优先的默认值。
        """
        return self._tool_requirements.get(
            tool_name, PermissionMode.DANGER_FULL_ACCESS
        )

    def authorize(
        self,
        tool_name: str,
        input_str: str,
        prompter: Optional[PermissionPrompter] = None,
    ) -> PermissionResult:
        """核心授权逻辑 — 源码 permissions.rs:89-134

        这是整个权限系统最关键的函数。决策逻辑:

        1. Allow 模式 → 直接放行（最危险）
        2. 当前模式 >= 所需模式 → 放行
           例如: WorkspaceWrite 模式下，ReadOnly 工具可以直接执行
        3. Prompt 模式 → 询问用户
        4. WorkspaceWrite + 需要 DangerFullAccess → 询问用户
           这是最常见的场景: 普通模式下执行危险命令
        5. 其他情况 → 拒绝
           例如: ReadOnly 模式下执行写入操作
        """
        current = self._active_mode
        required = self.required_mode_for(tool_name)

        # 快速路径: Allow 模式或当前权限足够
        if current == PermissionMode.ALLOW or current >= required:
            return PermissionResult(PermissionOutcome.ALLOW)

        # 创建请求对象
        request = PermissionRequest(
            tool_name=tool_name,
            input=input_str,
            current_mode=current,
            required_mode=required,
        )

        # 需要用户确认的情况:
        # 1. Prompt 模式 — 总是询问
        # 2. WorkspaceWrite 模式下需要 DangerFullAccess
        if (current == PermissionMode.PROMPT or
            (current == PermissionMode.WORKSPACE_WRITE and
             required == PermissionMode.DANGER_FULL_ACCESS)):
            if prompter is not None:
                return prompter.decide(request)
            else:
                # 没有提示器（无头模式）→ 拒绝
                return PermissionResult(
                    PermissionOutcome.DENY,
                    f"tool '{tool_name}' requires approval to escalate "
                    f"from {current.as_str()} to {required.as_str()}"
                )

        # 其他情况: 权限不足，直接拒绝
        return PermissionResult(
            PermissionOutcome.DENY,
            f"tool '{tool_name}' requires {required.as_str()} "
            f"permission; current mode is {current.as_str()}"
        )


# ============================================================
# 第三部分: Hook 系统 (hooks.rs)
# ============================================================
# 源码: rust/crates/runtime/src/hooks.rs
#
# Hook 是权限系统的"第二层防御"。它让用户可以注入自定义逻辑:
# - 在工具执行前拦截（PreToolUse）
# - 在工具执行后审计（PostToolUse）
#
# 关键设计: Hook 通过 exit code 通信:
#   0 = 允许（stdout 作为 feedback 附加到结果）
#   2 = 拒绝（阻止工具执行）
#   其他 = 警告（允许执行但记录警告）


class HookEvent(Enum):
    """Hook 事件类型 — 源码 hooks.rs:9-11"""
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"


@dataclass
class HookRunResult:
    """Hook 执行结果 — 源码 hooks.rs:22-27

    denied + messages 的组合让 Hook 可以:
    - 允许 + 无消息: 静默通过
    - 允许 + 有消息: 通过但附加反馈（模型能看到）
    - 拒绝 + 有消息: 阻止并告知原因
    """
    denied: bool = False
    messages: List[str] = field(default_factory=list)

    def is_denied(self) -> bool:
        return self.denied

    @staticmethod
    def allow(messages: List[str] = None) -> "HookRunResult":
        return HookRunResult(denied=False, messages=messages or [])


class HookCommandOutcome(Enum):
    """单个 Hook 命令的结果"""
    ALLOW = "allow"
    DENY = "deny"
    WARN = "warn"


@dataclass
class HookOutcome:
    """带消息的 Hook 结果"""
    outcome: HookCommandOutcome
    message: Optional[str] = None


class HookRunner:
    """Hook 运行器 — 源码 hooks.rs:54-206

    职责: 按顺序运行所有注册的 Hook 命令，
    任何一个返回 deny 就立即中止。
    """

    def __init__(
        self,
        pre_tool_use_commands: List[str] = None,
        post_tool_use_commands: List[str] = None,
    ):
        self._pre_commands = pre_tool_use_commands or []
        self._post_commands = post_tool_use_commands or []

    def run_pre_tool_use(
        self, tool_name: str, tool_input: str
    ) -> HookRunResult:
        """运行 PreToolUse 钩子 — 源码 hooks.rs:66-75"""
        return self._run_commands(
            HookEvent.PRE_TOOL_USE,
            self._pre_commands,
            tool_name, tool_input,
            tool_output=None, is_error=False,
        )

    def run_post_tool_use(
        self, tool_name: str, tool_input: str,
        tool_output: str, is_error: bool,
    ) -> HookRunResult:
        """运行 PostToolUse 钩子 — 源码 hooks.rs:77-93"""
        return self._run_commands(
            HookEvent.POST_TOOL_USE,
            self._post_commands,
            tool_name, tool_input,
            tool_output=tool_output, is_error=is_error,
        )

    def _run_commands(
        self,
        event: HookEvent,
        commands: List[str],
        tool_name: str,
        tool_input: str,
        tool_output: Optional[str],
        is_error: bool,
    ) -> HookRunResult:
        """执行所有 Hook 命令 — 源码 hooks.rs:96-150

        关键逻辑:
        1. 逐个执行命令
        2. 如果任何一个返回 deny → 立即停止，返回 denied
        3. 允许的命令的 stdout 作为 message 收集
        4. 警告的命令不阻止执行，但记录消息
        """
        if not commands:
            return HookRunResult.allow()

        # 构建 JSON payload（通过 stdin 传给 Hook）
        # 源码 hooks.rs:108-115
        payload = json.dumps({
            "hook_event_name": event.value,
            "tool_name": tool_name,
            "tool_input": _parse_tool_input(tool_input),
            "tool_input_json": tool_input,
            "tool_output": tool_output,
            "tool_result_is_error": is_error,
        })

        messages = []

        for command in commands:
            outcome = self._run_single_command(
                command, event, tool_name, tool_input,
                tool_output, is_error, payload,
            )

            if outcome.outcome == HookCommandOutcome.ALLOW:
                if outcome.message:
                    messages.append(outcome.message)

            elif outcome.outcome == HookCommandOutcome.DENY:
                # 拒绝: 立即停止，收集所有消息
                message = outcome.message or (
                    f"{event.value} hook denied tool `{tool_name}`"
                )
                messages.append(message)
                return HookRunResult(denied=True, messages=messages)

            elif outcome.outcome == HookCommandOutcome.WARN:
                messages.append(outcome.message)

        return HookRunResult.allow(messages)

    def _run_single_command(
        self,
        command: str,
        event: HookEvent,
        tool_name: str,
        tool_input: str,
        tool_output: Optional[str],
        is_error: bool,
        payload: str,
    ) -> HookOutcome:
        """运行单个 Hook 命令 — 源码 hooks.rs:152-205

        通信协议:
        - stdin: JSON payload（包含完整上下文）
        - 环境变量: HOOK_EVENT, HOOK_TOOL_NAME 等
        - exit code: 0=允许, 2=拒绝, 其他=警告
        - stdout: 反馈消息（会传给模型看到）

        为什么同时用 stdin 和环境变量？
        - 环境变量: 简单的 Hook 可以直接用 $HOOK_TOOL_NAME
        - stdin JSON: 复杂的 Hook 可以解析完整上下文
        两种方式都支持，让 Hook 开发者选择最方便的。
        """
        env = dict(os.environ)
        env["HOOK_EVENT"] = event.value
        env["HOOK_TOOL_NAME"] = tool_name
        env["HOOK_TOOL_INPUT"] = tool_input
        env["HOOK_TOOL_IS_ERROR"] = "1" if is_error else "0"
        if tool_output is not None:
            env["HOOK_TOOL_OUTPUT"] = tool_output

        try:
            result = subprocess.run(
                ["sh", "-lc", command],
                input=payload.encode(),
                capture_output=True,
                env=env,
                timeout=10,  # Hook 也有超时保护
            )

            stdout = result.stdout.decode("utf-8", errors="replace").strip()
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            message = stdout if stdout else None

            # exit code 协议 — 源码 hooks.rs:179-193
            if result.returncode == 0:
                return HookOutcome(HookCommandOutcome.ALLOW, message)
            elif result.returncode == 2:
                return HookOutcome(HookCommandOutcome.DENY, message)
            else:
                # 非 0 非 2: 警告，但允许继续
                warn_msg = (
                    f"Hook `{command}` exited with status {result.returncode}; "
                    f"allowing tool execution to continue"
                )
                if message:
                    warn_msg += f": {message}"
                elif stderr:
                    warn_msg += f": {stderr}"
                return HookOutcome(HookCommandOutcome.WARN, warn_msg)

        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return HookOutcome(
                HookCommandOutcome.WARN,
                f"{event.value} hook `{command}` failed for `{tool_name}`: {e}"
            )


def _parse_tool_input(tool_input: str) -> Any:
    """解析工具输入 — 源码 hooks.rs:214-216"""
    try:
        return json.loads(tool_input)
    except json.JSONDecodeError:
        return {"raw": tool_input}


# ============================================================
# 第四部分: Hook 反馈合并 (conversation.rs)
# ============================================================
# 源码: rust/crates/runtime/src/conversation.rs:408-424
#
# Hook 的 stdout 不是丢弃的——它会合并到工具的输出里，
# 让模型能看到 Hook 的反馈。这是一个精妙的设计。


def merge_hook_feedback(
    messages: List[str],
    output: str,
    denied: bool,
) -> str:
    """合并 Hook 反馈到工具输出 — 源码 conversation.rs:408-424

    例如:
    工具输出: "4"
    Pre-hook 消息: "pre hook ran"
    Post-hook 消息: "post hook ran"

    合并后: "4\n\nHook feedback:\npre hook ran\npost hook ran"

    如果 denied=True，标签变成 "Hook feedback (denied):"
    """
    if not messages:
        return output

    sections = []
    if output.strip():
        sections.append(output)

    label = "Hook feedback (denied)" if denied else "Hook feedback"
    sections.append(f"{label}:\n" + "\n".join(messages))

    return "\n\n".join(sections)


# ============================================================
# 第五部分: 完整的工具执行流程（权限 + Hook 集成）
# ============================================================
# 源码: rust/crates/runtime/src/conversation.rs:218-271
#
# 这是 agentic loop 里处理每个工具调用的完整流程。
# 注意 pre-hook 和 post-hook 分别在权限检查之后、工具执行前后。


def execute_tool_with_full_pipeline(
    tool_name: str,
    tool_input: str,
    tool_executor: Callable[[str, str], Tuple[str, bool]],
    policy: PermissionPolicy,
    hook_runner: HookRunner,
    prompter: Optional[PermissionPrompter] = None,
) -> Tuple[str, bool]:
    """完整的工具执行流水线 — 源码 conversation.rs:218-271

    流程:
    1. 权限检查 → deny? 返回拒绝消息
    2. Pre-hook → deny? 返回拒绝消息（工具根本不执行）
    3. 执行工具
    4. 合并 pre-hook 反馈到输出
    5. Post-hook → deny? 标记为错误
    6. 合并 post-hook 反馈到输出
    7. 返回最终结果
    """

    # 步骤 1: 权限检查
    perm_result = policy.authorize(tool_name, tool_input, prompter)
    if perm_result.outcome == PermissionOutcome.DENY:
        return perm_result.reason, True  # is_error=True

    # 步骤 2: Pre-hook
    pre_result = hook_runner.run_pre_tool_use(tool_name, tool_input)
    if pre_result.is_denied():
        deny_msg = f"PreToolUse hook denied tool `{tool_name}`"
        output = merge_hook_feedback(
            pre_result.messages, deny_msg, denied=False
        )
        return output, True

    # 步骤 3: 执行工具
    try:
        output, is_error = tool_executor(tool_name, tool_input)
    except Exception as e:
        output, is_error = str(e), True

    # 步骤 4: 合并 pre-hook 反馈
    output = merge_hook_feedback(pre_result.messages, output, denied=False)

    # 步骤 5: Post-hook
    post_result = hook_runner.run_post_tool_use(
        tool_name, tool_input, output, is_error
    )
    if post_result.is_denied():
        is_error = True

    # 步骤 6: 合并 post-hook 反馈
    output = merge_hook_feedback(
        post_result.messages, output, post_result.is_denied()
    )

    return output, is_error


# ============================================================
# 第六部分: TypeScript 版本的高级特性
# ============================================================
# 来源: reference/07-permission-pipeline.md
# Rust 版本是简化版，TypeScript 版本有更多高级特性。


class DenialTracker:
    """拒绝追踪与熔断器 — reference/07 第5节

    问题: 模型被拒绝后可能不断重试同一个操作，
    导致无限循环（拒绝→重试→拒绝→重试...）

    解决方案: 追踪连续拒绝次数和总拒绝次数，
    超过阈值时触发"熔断"——回退到用户提示或终止会话。

    这就是"熔断器模式"（Circuit Breaker）。
    """
    MAX_CONSECUTIVE = 3   # 连续 3 次拒绝 → 熔断
    MAX_TOTAL = 20        # 单次会话总计 20 次 → 熔断

    def __init__(self):
        self.consecutive = 0
        self.total = 0

    def record_denial(self) -> Optional[str]:
        """记录一次拒绝，返回 None 或熔断原因"""
        self.consecutive += 1
        self.total += 1

        if self.consecutive >= self.MAX_CONSECUTIVE:
            return (
                f"连续 {self.consecutive} 次被拒绝，"
                f"触发熔断——回退到用户交互模式"
            )
        if self.total >= self.MAX_TOTAL:
            return (
                f"本次会话累计 {self.total} 次被拒绝，"
                f"触发熔断——回退到用户交互模式"
            )
        return None

    def record_allow(self):
        """允许一次操作，重置连续拒绝计数"""
        self.consecutive = 0


class YoloClassifier:
    """YOLO 分类器 — reference/07 第4节

    当用户开启 auto 模式时，由 AI 分类器代替人工决策。
    但这个分类器有严格的安全约束:

    三级快速路径:
    1. acceptEdits 快速路径: 纯文件编辑操作 → 跳过 AI
    2. 安全工具白名单: Read, Glob 等 → 跳过 AI
    3. 两阶段 AI 分类:
       - 阶段 1 (快速): max_tokens=64，立即判断 yes/no
       - 阶段 2 (深思): 仅在阶段 1 阻止时运行，用思维链减少误报

    关键安全设计:
    - 分类器只看工具调用记录，不看助手文本！
      这防止模型构造诱导性文本来影响分类器。
    - 进入 auto 模式时，危险的通配符权限会被剥离
      比如 Bash(*) 会被暂存，离开 auto 时恢复。
    """

    # 安全工具白名单——这些工具完全跳过 AI 分类
    SAFE_TOOLS = {"Read", "Glob", "Grep", "LS"}

    # 危险权限——进入 auto 模式时必须剥离
    DANGEROUS_PATTERNS = {
        "Bash",           # 无限制的 Bash 访问
        "Bash(python:*)", # 任意 Python 代码
        "Bash(node:*)",   # 任意 Node 代码
        "Agent",          # 子代理
    }

    def __init__(self):
        self._stripped_rules: List[str] = []  # 被暂存的危险规则

    def enter_auto_mode(self, allowed_rules: List[str]) -> List[str]:
        """进入 auto 模式——剥离危险规则

        返回: 清理后的安全规则列表
        """
        safe = []
        for rule in allowed_rules:
            # 检查是否匹配危险模式
            is_dangerous = any(
                rule == pat or rule.startswith(f"{pat}(")
                for pat in self.DANGEROUS_PATTERNS
            )
            if is_dangerous:
                self._stripped_rules.append(rule)
            else:
                safe.append(rule)

        if self._stripped_rules:
            print(f"  [YOLO] 剥离了 {len(self._stripped_rules)} 条危险规则: "
                  f"{self._stripped_rules}")
        return safe

    def exit_auto_mode(self) -> List[str]:
        """退出 auto 模式——恢复被剥离的规则"""
        restored = self._stripped_rules.copy()
        self._stripped_rules.clear()
        return restored

    def classify(
        self,
        tool_name: str,
        tool_input: str,
        tool_history: List[Dict],
    ) -> bool:
        """分类器判断是否允许 — 返回 True=允许

        注意 tool_history 只包含工具调用，不包含助手文本。
        这是防止社会工程攻击的关键设计。
        """
        # 快速路径: 安全工具
        if tool_name in self.SAFE_TOOLS:
            return True

        # 模拟两阶段 AI 分类
        # 真实实现中会调用 LLM API
        print(f"  [YOLO 阶段1] 快速分类: {tool_name}({tool_input[:50]}...)")
        phase1_allow = self._phase1_classify(tool_name, tool_input, tool_history)

        if phase1_allow:
            return True

        # 阶段 2: 深思
        print(f"  [YOLO 阶段2] 深度分类: {tool_name}")
        return self._phase2_classify(tool_name, tool_input, tool_history)

    def _phase1_classify(self, tool_name, tool_input, history) -> bool:
        """阶段 1: 快速分类 (max_tokens=64)"""
        # 简单启发式模拟
        dangerous_keywords = ["rm ", "sudo", "chmod 777", "> /etc/"]
        return not any(kw in tool_input for kw in dangerous_keywords)

    def _phase2_classify(self, tool_name, tool_input, history) -> bool:
        """阶段 2: 深度分类 (思维链)"""
        # 真实实现会用 LLM 做推理
        return False  # 保守: 阶段 2 默认拒绝


# ============================================================
# 演示
# ============================================================

def demo_permission_modes():
    """演示权限模式层级"""
    print("\n" + "=" * 60)
    print("权限模式层级演示")
    print("=" * 60)

    # 注册工具权限需求
    policy = (PermissionPolicy(PermissionMode.WORKSPACE_WRITE)
              .with_tool_requirement("Read", PermissionMode.READ_ONLY)
              .with_tool_requirement("Write", PermissionMode.WORKSPACE_WRITE)
              .with_tool_requirement("Bash", PermissionMode.DANGER_FULL_ACCESS))

    # 测试不同工具在当前模式下的权限
    tools = [
        ("Read", '{"path": "README.md"}'),
        ("Write", '{"path": "src/main.py"}'),
        ("Bash", '{"command": "rm -rf /"}'),
        ("UnknownTool", '{}'),  # 未注册的工具
    ]

    print(f"\n当前模式: {policy.active_mode().as_str()}")
    for tool_name, input_str in tools:
        required = policy.required_mode_for(tool_name)
        result = policy.authorize(tool_name, input_str)
        print(f"  {tool_name}: 需要 {required.as_str()} → "
              f"{'✓ 允许' if result.outcome == PermissionOutcome.ALLOW else '✗ 拒绝'}"
              f"{'  原因: ' + result.reason if result.reason else ''}")


def demo_permission_escalation():
    """演示权限升级提示"""
    print("\n" + "=" * 60)
    print("权限升级提示演示")
    print("=" * 60)

    class AutoApprovePrompter(PermissionPrompter):
        """自动批准提示器"""
        def __init__(self):
            self.seen = []
        def decide(self, request: PermissionRequest) -> PermissionResult:
            self.seen.append(request)
            print(f"  [用户提示] {request.tool_name}: "
                  f"{request.current_mode.as_str()} → {request.required_mode.as_str()}")
            return PermissionResult(PermissionOutcome.ALLOW)

    class RejectPrompter(PermissionPrompter):
        """自动拒绝提示器"""
        def decide(self, request: PermissionRequest) -> PermissionResult:
            print(f"  [用户拒绝] {request.tool_name}")
            return PermissionResult(PermissionOutcome.DENY, "用户说不")

    policy = (PermissionPolicy(PermissionMode.WORKSPACE_WRITE)
              .with_tool_requirement("Bash", PermissionMode.DANGER_FULL_ACCESS))

    # 场景 1: 用户批准
    print("\n场景 1: 用户批准升级")
    approver = AutoApprovePrompter()
    result = policy.authorize("Bash", "echo hello", approver)
    print(f"  结果: {result.outcome.value}")

    # 场景 2: 用户拒绝
    print("\n场景 2: 用户拒绝升级")
    rejecter = RejectPrompter()
    result = policy.authorize("Bash", "echo hello", rejecter)
    print(f"  结果: {result.outcome.value}, 原因: {result.reason}")

    # 场景 3: 无头模式（没有提示器）
    print("\n场景 3: 无头模式（自动拒绝）")
    result = policy.authorize("Bash", "echo hello", None)
    print(f"  结果: {result.outcome.value}, 原因: {result.reason}")


def demo_hook_protocol():
    """演示 Hook 的 exit code 协议"""
    print("\n" + "=" * 60)
    print("Hook Exit Code 协议演示")
    print("=" * 60)

    # 创建不同行为的 Hook
    scenarios = [
        ("允许 (exit 0)", "printf 'hook says ok'; exit 0"),
        ("拒绝 (exit 2)", "printf 'blocked by security policy'; exit 2"),
        ("警告 (exit 1)", "printf 'suspicious but allowed'; exit 1"),
    ]

    for name, cmd in scenarios:
        print(f"\n--- {name} ---")
        runner = HookRunner(pre_tool_use_commands=[cmd])
        result = runner.run_pre_tool_use("Bash", '{"command": "ls"}')
        print(f"  denied: {result.is_denied()}")
        print(f"  messages: {result.messages}")


def demo_full_pipeline():
    """演示完整的权限+Hook流水线"""
    print("\n" + "=" * 60)
    print("完整流水线演示（权限 + Hook）")
    print("=" * 60)

    def mock_executor(tool_name: str, tool_input: str) -> Tuple[str, bool]:
        return f"Tool {tool_name} executed successfully", False

    # 场景 1: 权限通过 + Hook 通过
    print("\n场景 1: 全部通过")
    policy = PermissionPolicy(PermissionMode.DANGER_FULL_ACCESS)
    hooks = HookRunner(
        pre_tool_use_commands=["printf 'pre check ok'"],
        post_tool_use_commands=["printf 'post audit ok'"],
    )
    output, is_error = execute_tool_with_full_pipeline(
        "Bash", '{"command":"ls"}', mock_executor, policy, hooks
    )
    print(f"  error: {is_error}")
    print(f"  output:\n    {output.replace(chr(10), chr(10) + '    ')}")

    # 场景 2: 权限拒绝（Hook 根本不执行）
    print("\n场景 2: 权限拒绝")
    policy = PermissionPolicy(PermissionMode.READ_ONLY)
    policy.with_tool_requirement("Bash", PermissionMode.DANGER_FULL_ACCESS)
    output, is_error = execute_tool_with_full_pipeline(
        "Bash", '{"command":"rm -rf /"}', mock_executor, policy, hooks
    )
    print(f"  error: {is_error}")
    print(f"  output: {output}")

    # 场景 3: 权限通过 + Pre-hook 拒绝
    print("\n场景 3: Pre-hook 拒绝")
    policy = PermissionPolicy(PermissionMode.DANGER_FULL_ACCESS)
    hooks = HookRunner(
        pre_tool_use_commands=["printf 'blocked: dangerous command detected'; exit 2"],
    )
    output, is_error = execute_tool_with_full_pipeline(
        "Bash", '{"command":"rm -rf /"}', mock_executor, policy, hooks
    )
    print(f"  error: {is_error}")
    print(f"  output: {output}")


def demo_denial_circuit_breaker():
    """演示拒绝熔断器"""
    print("\n" + "=" * 60)
    print("拒绝熔断器演示")
    print("=" * 60)

    tracker = DenialTracker()

    # 模拟连续拒绝
    for i in range(5):
        result = tracker.record_denial()
        if result:
            print(f"  第 {i+1} 次拒绝: 🔥 {result}")
            break
        else:
            print(f"  第 {i+1} 次拒绝: 继续运行")

    # 重置：一次允许就清零连续计数
    print("\n  --- 允许一次 ---")
    tracker.record_allow()
    result = tracker.record_denial()
    print(f"  再次拒绝: {'🔥 ' + result if result else '继续运行（连续计数已重置）'}")


def demo_yolo_classifier():
    """演示 YOLO 分类器"""
    print("\n" + "=" * 60)
    print("YOLO 分类器演示")
    print("=" * 60)

    classifier = YoloClassifier()

    # 进入 auto 模式
    print("\n进入 auto 模式:")
    allowed = ["Read", "Write", "Bash", "Bash(python:*)", "Agent(Explore)"]
    safe = classifier.enter_auto_mode(allowed)
    print(f"  保留的安全规则: {safe}")

    # 分类测试
    print("\n分类测试:")
    tests = [
        ("Read", '{"path":"README.md"}', "安全工具白名单"),
        ("Bash", '{"command":"npm test"}', "普通命令"),
        ("Bash", '{"command":"rm -rf /tmp/test"}', "危险命令"),
        ("Bash", '{"command":"sudo chmod 777 /etc/passwd"}', "非常危险"),
    ]
    for tool_name, tool_input, desc in tests:
        allowed = classifier.classify(tool_name, tool_input, [])
        print(f"  {desc}: {tool_name}({tool_input[:40]}...) → "
              f"{'✓ 允许' if allowed else '✗ 阻止'}")

    # 退出 auto 模式
    print("\n退出 auto 模式:")
    restored = classifier.exit_auto_mode()
    print(f"  恢复的规则: {restored}")


# ============================================================
# 主程序
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("教程 15: 权限与 Hook 流水线深度剖析")
    print("=" * 60)

    demo_permission_modes()
    demo_permission_escalation()
    demo_hook_protocol()
    demo_full_pipeline()
    demo_denial_circuit_breaker()
    demo_yolo_classifier()

    print("\n" + "=" * 60)
    print("关键工程要点总结:")
    print("=" * 60)
    print("""
1. 模式层级比较: 用 IntEnum 实现 current >= required
   - ReadOnly < WorkspaceWrite < DangerFullAccess
   - 未注册的工具默认需要最高权限（安全优先）

2. PermissionPrompter 接口解耦:
   - 权限逻辑不关心 UI 怎么实现
   - 同样的逻辑可以在 CLI/IDE/无头模式下运行
   - 无头模式下没有 prompter → 自动拒绝

3. Hook 的 exit code 协议:
   - 0=允许, 2=拒绝, 其他=警告但允许
   - 这让任何 shell 脚本都能成为 Hook
   - 双通道通信: 环境变量(简单) + stdin JSON(复杂)

4. Hook 反馈合并: stdout 不是丢弃的
   - Pre-hook 的 stdout 附加到工具输出
   - 模型能看到 Hook 的反馈，可以据此调整行为
   - 比如 Hook 说"注意: 这个文件受保护"

5. 拒绝熔断器: 防止无限重试
   - 连续 3 次 → 回退到用户交互
   - 总计 20 次 → 回退到用户交互
   - 一次成功就重置连续计数

6. YOLO 分类器的三重保护:
   - 危险权限剥离: 进入 auto 前就移除 Bash(*) 等
   - 排除助手文本: 分类器只看工具调用，防止社会工程
   - 两阶段分类: 快速判断 + 深思确认，减少误报

7. 七步流水线中的"绕过免疫":
   - 即使在 bypassPermissions 模式下
   - .git/ 保护、.claude/ 保护、shell 配置保护仍然生效
   - 这防止了沙箱逃逸和权限自升级
""")
