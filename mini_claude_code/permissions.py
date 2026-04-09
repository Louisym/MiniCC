"""
permissions.py — IntEnum 权限层级 + Policy 对象 + Prompter 回调

忠实还原 Claude Code 的权限系统。
源码对照: rust/crates/runtime/src/permissions.rs (233 行)

三件套设计:
1. PermissionMode (IntEnum) — 5 级权限，用 >= 比较 (permissions.rs:3-10)
2. PermissionPolicy — 管理当前模式 + 每个工具的权限需求 (permissions.rs:50-53)
3. PermissionPrompter (Protocol) — 解耦"询问用户"逻辑 (permissions.rs:39-41)
"""

from enum import IntEnum, Enum
from typing import Optional, Protocol

from pydantic import BaseModel


# ============================================================
# PermissionMode — 权限级别
# 源码: permissions.rs:3-10
#
# CC 用 Rust 的 derive(PartialOrd, Ord) 让枚举可比较。
# Python 用 IntEnum 达到同样效果: 直接用 >= 比较。
#
# 注意: Prompt 和 Allow 比 DangerFullAccess 更"高"，
# 但含义不同 — 它们不是"更多权限"，而是"不同的决策模式"。
#   Prompt: 所有操作都询问用户
#   Allow: 跳过所有检查（最危险）
# ============================================================

class PermissionMode(IntEnum):
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
# PermissionRequest — 权限请求
# 源码: permissions.rs:26-31
#
# 当权限不足需要询问用户时，构建一个 request 传给 prompter。
# 包含: 哪个工具、什么输入、当前权限、需要权限。
# ============================================================

class PermissionRequest(BaseModel):
    tool_name: str
    input: str
    current_mode: PermissionMode
    required_mode: PermissionMode


# ============================================================
# PermissionDecision + PermissionResult
# 源码: permissions.rs:33-37 (PermissionPromptDecision)
#        permissions.rs:43-47 (PermissionOutcome)
# ============================================================

class PermissionDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"


class PermissionResult(BaseModel):
    decision: PermissionDecision
    reason: str = ""

    @staticmethod
    def allow() -> "PermissionResult":
        return PermissionResult(decision=PermissionDecision.ALLOW)

    @staticmethod
    def deny(reason: str) -> "PermissionResult":
        return PermissionResult(decision=PermissionDecision.DENY, reason=reason)

    @property
    def is_allowed(self) -> bool:
        return self.decision == PermissionDecision.ALLOW


# ============================================================
# PermissionPrompter — 用户询问接口
# 源码: permissions.rs:39-41
#
# CC 用 Rust 的 trait。Python 用 Protocol（结构化子类型）。
# Protocol 不需要继承 — 只要你的类有 decide() 方法就自动匹配。
# 这叫"鸭子类型"的正式版: 像鸭子就是鸭子。
#
# 这样 CLI 弹终端提示、IDE 弹 GUI 对话框，都能无缝接入。
# ============================================================

class PermissionPrompter(Protocol):
    def decide(self, request: PermissionRequest) -> PermissionResult: ...


# ============================================================
# PermissionPolicy — 权限策略
# 源码: permissions.rs:50-135
#
# 集中管理:
#   - active_mode: 当前会话的权限模式
#   - tool_requirements: 每个工具需要的最低权限级别
#   - authorize(): 核心授权判断
#
# Builder 模式: with_tool_requirement() 返回 self，链式构建。
# ============================================================

class PermissionPolicy:
    def __init__(self, active_mode: PermissionMode):
        self._active_mode = active_mode
        self._tool_requirements: dict[str, PermissionMode] = {}

    def with_tool_requirement(
        self,
        tool_name: str,
        required_mode: PermissionMode,
    ) -> "PermissionPolicy":
        """链式注册工具权限需求。源码: permissions.rs:65-73

        用法:
            policy = (PermissionPolicy(PermissionMode.WORKSPACE_WRITE)
                .with_tool_requirement("read_file", PermissionMode.READ_ONLY)
                .with_tool_requirement("bash", PermissionMode.DANGER_FULL_ACCESS))
        """
        self._tool_requirements[tool_name] = required_mode
        return self

    @property
    def active_mode(self) -> PermissionMode:
        return self._active_mode

    def required_mode_for(self, tool_name: str) -> PermissionMode:
        """查询工具需要的权限。源码: permissions.rs:82-86

        默认从严 (fail-closed): 未注册工具默认需要 DANGER_FULL_ACCESS。
        这样如果忘记注册工具权限 → 需要最高权限 → 用户会被提示 → 不会静默执行。
        """
        return self._tool_requirements.get(tool_name, PermissionMode.DANGER_FULL_ACCESS)

    def authorize(
        self,
        tool_name: str,
        input_str: str,
        prompter: Optional[PermissionPrompter] = None,
    ) -> PermissionResult:
        """核心授权逻辑。源码: permissions.rs:89-134

        决策流程:
        1. Allow 模式 → 放行（跳过一切检查）
        2. current >= required → 放行（权限足够）
        3. Prompt 模式 → 询问用户
        4. WorkspaceWrite + 需要 DangerFullAccess → 询问用户
           （最常见: 普通模式下执行危险命令，弹窗确认）
        5. 其他 → 拒绝
        """
        current = self._active_mode
        required = self.required_mode_for(tool_name)

        # 1 & 2: Allow 直接放行，或权限足够
        if current == PermissionMode.ALLOW or current >= required:
            return PermissionResult.allow()

        # 3 & 4: 需要询问用户的场景
        need_prompt = (
            current == PermissionMode.PROMPT
            or (current == PermissionMode.WORKSPACE_WRITE
                and required == PermissionMode.DANGER_FULL_ACCESS)
        )

        if need_prompt:
            if prompter is not None:
                request = PermissionRequest(
                    tool_name=tool_name,
                    input=input_str,
                    current_mode=current,
                    required_mode=required,
                )
                return prompter.decide(request)
            # 没有 prompter → 需要升级但没人可问 → 拒绝
            return PermissionResult.deny(
                f"tool '{tool_name}' requires approval to escalate "
                f"from {current.as_str()} to {required.as_str()}"
            )

        # 5: 其他情况 → 拒绝
        return PermissionResult.deny(
            f"tool '{tool_name}' requires {required.as_str()} permission; "
            f"current mode is {current.as_str()}"
        )
