"""
Tutorial 04: Permission System — 安全阀门
==========================================

AI 能执行 shell 命令，能读写文件 —— 这很强大，但也很危险！
如果 AI 误解了你的意思，执行了 rm -rf / 怎么办？

所以 Claude Code 有一套权限系统（Permission System），就像大楼的门禁：
  - 一楼大厅谁都能进（ReadOnly: 只读）
  - 办公区需要刷卡（WorkspaceWrite: 写入）
  - 机房需要特殊授权（DangerFullAccess: 完全访问）
  - 每次进入需要保安确认（Prompt: 询问模式）

本教程会教你：
1. 权限等级是什么
2. 每个工具需要什么权限
3. 权限判断的逻辑
4. "询问用户"是怎么实现的

对应源码：rust/crates/runtime/src/permissions.rs

运行方式：python tutorials/04_permission_system.py
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol, Optional


# ============================================================
# 第一步：定义权限等级
# ============================================================
# IntEnum 是什么？
# 就是一个有数字大小的枚举。比如 ReadOnly=1, WorkspaceWrite=2
# 数字越大，权限越高。我们可以用 > < 来比较它们。

class PermissionMode(IntEnum):
    """
    权限等级 —— 从低到高。

    对应源码: permissions.rs:3-9
    """
    READ_ONLY = 1          # 只能读取，最安全
    WORKSPACE_WRITE = 2    # 可以在工作目录里写文件
    DANGER_FULL_ACCESS = 3 # 可以做任何事情（包括执行危险命令）

    def __str__(self):
        names = {1: "read-only", 2: "workspace-write", 3: "danger-full-access"}
        return names[self.value]


# 两个特殊模式（不属于等级，是行为模式）
PROMPT_MODE = "prompt"  # 每次使用工具都询问用户
ALLOW_MODE = "allow"    # 全部允许，不询问


# ============================================================
# 第二步：定义 PermissionPrompter（询问用户的方式）
# ============================================================
# 当权限不够时，系统需要"问用户"："这个操作你同意吗？"
# 但"怎么问"取决于你用的是什么界面：
#   - 命令行界面(CLI)：在终端里打印提示，等用户输入 y/n
#   - IDE 插件(VS Code)：弹出一个对话框
#   - 测试环境：自动回答（不需要人）
#
# 为了让同一套权限逻辑适配不同的界面，我们用 Protocol。
#
# Protocol 是什么？
# Protocol 就是一个"契约"或"规格说明"。它说：
#   "任何实现了 decide() 方法的类，都可以当作 PermissionPrompter 使用"
# 你不需要继承它，只要你的类有 decide() 方法就行。
# 这叫做"鸭子类型"（如果它走起来像鸭子，叫起来像鸭子，那它就是鸭子）。

@dataclass(frozen=True)
class PermissionRequest:
    """权限请求 —— 告诉用户 "谁要做什么"。"""
    tool_name: str               # 哪个工具在请求
    input_preview: str           # 工具要做什么（参数预览）
    current_mode: PermissionMode # 当前的权限等级
    required_mode: PermissionMode # 需要的权限等级


@dataclass(frozen=True)
class PermissionDecision:
    """用户的决定 —— 允许还是拒绝。"""
    allowed: bool
    reason: str = ""


class PermissionPrompter(Protocol):
    """
    询问用户权限的接口（Protocol）。

    任何有 decide() 方法的类都可以当作 PermissionPrompter。
    对应源码: permissions.rs:39-41
    """
    def decide(self, request: PermissionRequest) -> PermissionDecision:
        ...


# ============================================================
# 第三步：实现几种不同的 Prompter
# ============================================================

class AlwaysAllowPrompter:
    """总是允许 —— 用于测试或信任环境。"""
    def decide(self, request: PermissionRequest) -> PermissionDecision:
        return PermissionDecision(allowed=True, reason="auto-approved")


class AlwaysDenyPrompter:
    """总是拒绝 —— 用于测试。"""
    def decide(self, request: PermissionRequest) -> PermissionDecision:
        return PermissionDecision(allowed=False, reason="auto-denied for safety")


class InteractivePrompter:
    """
    交互式询问 —— 在终端里问用户。
    这就是你在 Claude Code CLI 里看到的那个确认提示。
    """
    def decide(self, request: PermissionRequest) -> PermissionDecision:
        print(f"\n  [权限请求] 工具 '{request.tool_name}' 需要 {request.required_mode} 权限")
        print(f"  [权限请求] 当前权限: {request.current_mode}")
        print(f"  [权限请求] 操作内容: {request.input_preview}")
        answer = input("  允许执行吗？(y/n): ").strip().lower()
        if answer == "y":
            return PermissionDecision(allowed=True, reason="user approved")
        return PermissionDecision(allowed=False, reason="user denied")


# ============================================================
# 第四步：实现 PermissionPolicy（权限策略）
# ============================================================
# PermissionPolicy 是权限判断的核心。它包含：
#   1. 当前的权限等级（用户选择的）
#   2. 每个工具需要什么权限
#   3. authorize() 方法：判断某个工具能否执行

class PermissionPolicy:
    """
    权限策略 —— 决定哪些工具可以执行。

    对应源码: permissions.rs:49-134
    """

    def __init__(self, active_mode: PermissionMode | str):
        """
        参数:
            active_mode: 当前的权限模式
                可以是 PermissionMode 枚举值
                或 "prompt"（每次询问） / "allow"（全部允许）
        """
        self.active_mode = active_mode
        # 每个工具需要的权限等级
        # 默认所有工具都需要最高权限，然后逐个设置
        self._tool_requirements: dict[str, PermissionMode] = {}

    def with_tool_requirement(self, tool_name: str, required_mode: PermissionMode) -> "PermissionPolicy":
        """
        设置某个工具需要的权限等级。

        链式调用：policy.with_tool_requirement("bash", DANGER).with_tool_requirement("read", READ)
        """
        self._tool_requirements[tool_name] = required_mode
        return self

    def required_mode_for(self, tool_name: str) -> PermissionMode:
        """查询某个工具需要什么权限"""
        return self._tool_requirements.get(tool_name, PermissionMode.DANGER_FULL_ACCESS)

    def authorize(
        self,
        tool_name: str,
        tool_input: str,
        prompter: Optional[PermissionPrompter] = None,
    ) -> tuple[bool, str]:
        """
        判断某个工具是否可以执行。

        核心逻辑（和 Claude Code 源码一致）：
        1. 如果是 "allow" 模式 → 直接允许
        2. 如果当前权限 >= 需要的权限 → 允许
        3. 如果当前权限不够但可以询问 → 问用户
        4. 否则 → 拒绝

        返回:
            (是否允许, 原因说明)

        对应源码: permissions.rs:89-134
        """
        # 情况 1: allow 模式，全部放行
        if self.active_mode == ALLOW_MODE:
            return (True, "allow mode: all tools permitted")

        required = self.required_mode_for(tool_name)

        # 情况 2: prompt 模式，每次都询问
        if self.active_mode == PROMPT_MODE:
            if prompter is not None:
                request = PermissionRequest(
                    tool_name=tool_name,
                    input_preview=tool_input[:80],
                    current_mode=PermissionMode.READ_ONLY,  # prompt 模式默认最低
                    required_mode=required,
                )
                decision = prompter.decide(request)
                return (decision.allowed, decision.reason)
            return (False, f"tool '{tool_name}' requires approval but no prompter available")

        # 情况 3: 正常的等级比较
        current = self.active_mode
        if not isinstance(current, PermissionMode):
            return (False, f"unknown permission mode: {current}")

        # 当前权限 >= 需要的权限 → 允许
        if current >= required:
            return (True, f"current mode {current} meets requirement {required}")

        # 当前权限不够，但可以询问升级
        # （WorkspaceWrite 模式下遇到需要 DangerFullAccess 的工具）
        if (current == PermissionMode.WORKSPACE_WRITE
                and required == PermissionMode.DANGER_FULL_ACCESS
                and prompter is not None):
            request = PermissionRequest(
                tool_name=tool_name,
                input_preview=tool_input[:80],
                current_mode=current,
                required_mode=required,
            )
            decision = prompter.decide(request)
            return (decision.allowed, decision.reason)

        # 权限不够，且无法询问 → 拒绝
        return (
            False,
            f"tool '{tool_name}' requires {required} permission; current mode is {current}",
        )


# ============================================================
# 第五步：演示
# ============================================================

def main():
    print("=" * 60)
    print("Tutorial 04: Permission System 权限系统演示")
    print("=" * 60)

    # 创建一个 WorkspaceWrite 模式的策略
    # 并设置每个工具的权限要求
    policy = (
        PermissionPolicy(PermissionMode.WORKSPACE_WRITE)
        .with_tool_requirement("read_file", PermissionMode.READ_ONLY)
        .with_tool_requirement("write_file", PermissionMode.WORKSPACE_WRITE)
        .with_tool_requirement("bash", PermissionMode.DANGER_FULL_ACCESS)
        .with_tool_requirement("grep", PermissionMode.READ_ONLY)
    )

    # 准备不同的 Prompter
    auto_allow = AlwaysAllowPrompter()
    auto_deny = AlwaysDenyPrompter()

    print("\n--- 场景 1: 权限足够的工具 ---")
    for tool in ["read_file", "write_file", "grep"]:
        allowed, reason = policy.authorize(tool, '{"path": "test.py"}')
        print(f"  {tool}: {'ALLOW' if allowed else 'DENY'} — {reason}")

    print("\n--- 场景 2: 权限不够，没有 Prompter ---")
    allowed, reason = policy.authorize("bash", '{"command": "rm -rf /"}')
    print(f"  bash: {'ALLOW' if allowed else 'DENY'} — {reason}")

    print("\n--- 场景 3: 权限不够，用 AlwaysAllow Prompter 自动批准 ---")
    allowed, reason = policy.authorize("bash", '{"command": "echo hello"}', auto_allow)
    print(f"  bash: {'ALLOW' if allowed else 'DENY'} — {reason}")

    print("\n--- 场景 4: 权限不够，用 AlwaysDeny Prompter 自动拒绝 ---")
    allowed, reason = policy.authorize("bash", '{"command": "echo hello"}', auto_deny)
    print(f"  bash: {'ALLOW' if allowed else 'DENY'} — {reason}")

    print("\n--- 场景 5: Allow 模式（全部放行）---")
    allow_policy = PermissionPolicy(ALLOW_MODE)
    allowed, reason = allow_policy.authorize("bash", '{"command": "rm -rf /"}')
    print(f"  bash (allow mode): {'ALLOW' if allowed else 'DENY'} — {reason}")

    print("\n--- 场景 6: ReadOnly 模式（最严格）---")
    readonly_policy = (
        PermissionPolicy(PermissionMode.READ_ONLY)
        .with_tool_requirement("read_file", PermissionMode.READ_ONLY)
        .with_tool_requirement("bash", PermissionMode.DANGER_FULL_ACCESS)
    )
    allowed, reason = readonly_policy.authorize("read_file", '{"path": "test.py"}')
    print(f"  read_file: {'ALLOW' if allowed else 'DENY'} — {reason}")
    allowed, reason = readonly_policy.authorize("bash", '{"command": "ls"}')
    print(f"  bash: {'ALLOW' if allowed else 'DENY'} — {reason}")

    # 解说
    print("\n" + "=" * 60)
    print("关键理解要点：")
    print("=" * 60)
    print("""
    1. 权限等级（从低到高）:
       ReadOnly(1) < WorkspaceWrite(2) < DangerFullAccess(3)

    2. 每个工具有一个"最低权限要求":
       - read_file, grep → ReadOnly（安全的，只看不改）
       - write_file, edit_file → WorkspaceWrite（可以改文件）
       - bash → DangerFullAccess（能做任何事，最危险）

    3. 授权逻辑:
       当前权限 >= 需要的权限 → 直接允许
       当前权限 < 需要的权限 → 询问用户（如果有 Prompter）
       当前权限 < 需要的权限且无法询问 → 拒绝

    4. PermissionPrompter 是一个 Protocol（接口）:
       - 在 CLI 里：弹出 y/n 确认
       - 在 IDE 里：弹出对话框
       - 在测试中：自动批准或拒绝
       不同的实现，同样的逻辑 —— 这就是 Protocol 的价值

    5. 这个权限系统在 Agentic Loop 里的位置:
       AI 要用工具 → 先检查权限 → 通过了才执行 → 没通过就拒绝
       （回顾 Tutorial 01 的 run_turn 函数，权限检查插在工具执行之前）

    对应 Claude Code 源码:
    - permissions.rs:3-9    →  PermissionMode 枚举
    - permissions.rs:25-37  →  PermissionRequest / PermissionDecision
    - permissions.rs:39-41  →  PermissionPrompter trait
    - permissions.rs:49-57  →  PermissionPolicy 结构
    - permissions.rs:89-134 →  authorize() 核心逻辑
    """)


if __name__ == "__main__":
    main()
