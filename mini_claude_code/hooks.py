"""
hooks.py — 退出码协议 + 双通道输入 + 熔断执行

忠实还原 Claude Code 的 Hook 系统。
源码对照: rust/crates/runtime/src/hooks.rs (350 行)

Hook 是权限系统之外的第二层防御。用户自定义 shell 命令，
在工具执行前后自动运行。核心设计:

1. 退出码协议: 0=允许, 2=拒绝, 其他=警告 (hooks.rs:179-196)
2. 双通道输入: 环境变量 + stdin JSON (hooks.rs:166-174)
3. 顺序执行 + 熔断: deny 时立刻停止后续 hook (hooks.rs:135-143)
"""

import json
import os
import subprocess
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ============================================================
# HookEvent — hook 触发时机
# 源码: hooks.rs:8-12
# ============================================================

class HookEvent(Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"


# ============================================================
# HookResult — hook 执行结果
# 源码: hooks.rs:24-47
#
# denied=True 时，工具不会执行。
# messages 收集所有 hook 的 stdout 输出（允许/警告/拒绝原因）。
# ============================================================

class HookResult(BaseModel):
    denied: bool = False
    messages: list[str] = Field(default_factory=list)

    @staticmethod
    def allow(messages: Optional[list[str]] = None) -> "HookResult":
        return HookResult(denied=False, messages=messages or [])

    @staticmethod
    def deny(messages: Optional[list[str]] = None) -> "HookResult":
        return HookResult(denied=True, messages=messages or [])


# ============================================================
# _HookCommandOutcome — 单个 hook 命令的三元结果
# 源码: hooks.rs:208-212
#
# 内部类型，不对外暴露。三种结局:
#   Allow: 命令允许，可能带 message (stdout)
#   Deny:  命令拒绝，可能带 message (stdout)
#   Warn:  命令异常但不阻止，带警告 message
# ============================================================

class _Outcome(Enum):
    ALLOW = "allow"
    DENY = "deny"
    WARN = "warn"


class _HookCommandOutcome:
    def __init__(self, kind: _Outcome, message: Optional[str] = None):
        self.kind = kind
        self.message = message


# ============================================================
# HookRunner — hook 执行器
# 源码: hooks.rs:49-206
#
# 从 config 加载 hook 命令列表，执行时:
# 1. 构建 stdin JSON payload (hooks.rs:108-116)
# 2. 逐个执行命令 (hooks.rs:120-149)
# 3. 设置环境变量 + stdin 管道 (hooks.rs:162-174)
# 4. 按退出码分类结果 (hooks.rs:179-196)
# 5. deny 时熔断，不执行后续 hook (hooks.rs:135-143)
# ============================================================

class HookRunner:
    def __init__(
        self,
        pre_tool_use: Optional[list[str]] = None,
        post_tool_use: Optional[list[str]] = None,
    ):
        self._pre_tool_use = pre_tool_use or []
        self._post_tool_use = post_tool_use or []

    @classmethod
    def from_config(cls, config) -> "HookRunner":
        """从 RuntimeConfig 加载。源码: hooks.rs:61-63"""
        return cls(
            pre_tool_use=config.hooks_pre(),
            post_tool_use=config.hooks_post(),
        )

    def run_pre_tool_use(self, tool_name: str, tool_input: str) -> HookResult:
        """执行所有 PreToolUse hook。源码: hooks.rs:66-75"""
        return self._run_commands(
            event=HookEvent.PRE_TOOL_USE,
            commands=self._pre_tool_use,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=None,
            is_error=False,
        )

    def run_post_tool_use(
        self,
        tool_name: str,
        tool_input: str,
        tool_output: str,
        is_error: bool = False,
    ) -> HookResult:
        """执行所有 PostToolUse hook。源码: hooks.rs:78-93"""
        return self._run_commands(
            event=HookEvent.POST_TOOL_USE,
            commands=self._post_tool_use,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            is_error=is_error,
        )

    def _run_commands(
        self,
        event: HookEvent,
        commands: list[str],
        tool_name: str,
        tool_input: str,
        tool_output: Optional[str],
        is_error: bool,
    ) -> HookResult:
        """核心: 顺序执行命令，deny 时熔断。源码: hooks.rs:95-150

        CC 的设计: 遍历命令列表，每个命令执行后检查结果。
        - Allow: 收集 message，继续下一个
        - Deny: 收集 message，立刻返回 denied=True（熔断）
        - Warn: 收集 message，继续下一个
        """
        if not commands:
            return HookResult.allow()

        # 构建 stdin JSON payload — 源码: hooks.rs:108-116
        # tool_input 尝试解析为对象，失败则包在 {"raw": ...} 里
        try:
            tool_input_parsed = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            tool_input_parsed = {"raw": tool_input}

        payload = json.dumps({
            "hook_event_name": event.value,
            "tool_name": tool_name,
            "tool_input": tool_input_parsed,
            "tool_input_json": tool_input,
            "tool_output": tool_output,
            "tool_result_is_error": is_error,
        })

        messages: list[str] = []

        for command in commands:
            outcome = self._run_command(
                command=command,
                event=event,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=tool_output,
                is_error=is_error,
                payload=payload,
            )

            if outcome.kind == _Outcome.ALLOW:
                if outcome.message:
                    messages.append(outcome.message)

            elif outcome.kind == _Outcome.DENY:
                # 熔断: deny 时加入消息并立即返回
                deny_msg = outcome.message or f"{event.value} hook denied tool `{tool_name}`"
                messages.append(deny_msg)
                return HookResult.deny(messages)

            elif outcome.kind == _Outcome.WARN:
                if outcome.message:
                    messages.append(outcome.message)

        return HookResult.allow(messages)

    @staticmethod
    def _run_command(
        command: str,
        event: HookEvent,
        tool_name: str,
        tool_input: str,
        tool_output: Optional[str],
        is_error: bool,
        payload: str,
    ) -> _HookCommandOutcome:
        """执行单个 hook 命令。源码: hooks.rs:152-205

        1. 用 sh -lc 执行 shell 命令 (hooks.rs:239-244)
        2. 设环境变量 (hooks.rs:166-172)
        3. stdin 管道传 JSON payload (hooks.rs:174, 282-288)
        4. 按退出码分类 (hooks.rs:179-196):
           0 → Allow, 2 → Deny, 其他 → Warn
        """
        # 构建环境变量 — 源码: hooks.rs:166-172
        env = dict(os.environ)
        env["HOOK_EVENT"] = event.value
        env["HOOK_TOOL_NAME"] = tool_name
        env["HOOK_TOOL_INPUT"] = tool_input
        env["HOOK_TOOL_IS_ERROR"] = "1" if is_error else "0"
        if tool_output is not None:
            env["HOOK_TOOL_OUTPUT"] = tool_output

        try:
            # sh -lc: login shell 执行命令 — 源码: hooks.rs:239-244
            result = subprocess.run(
                ["sh", "-lc", command],
                input=payload,
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
        except FileNotFoundError:
            # 命令不存在 → Warn — 源码: hooks.rs:198-204
            return _HookCommandOutcome(
                _Outcome.WARN,
                f"{event.value} hook `{command}` failed to start for `{tool_name}`: command not found",
            )
        except subprocess.TimeoutExpired:
            return _HookCommandOutcome(
                _Outcome.WARN,
                f"{event.value} hook `{command}` timed out for `{tool_name}`",
            )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        message = stdout if stdout else None

        code = result.returncode

        # 退出码协议 — 源码: hooks.rs:179-196
        if code == 0:
            return _HookCommandOutcome(_Outcome.ALLOW, message)

        if code == 2:
            return _HookCommandOutcome(_Outcome.DENY, message)

        # 其他非零: 警告但继续执行
        warn_msg = f"Hook `{command}` exited with status {code}; allowing tool execution to continue"
        if stdout:
            warn_msg += f": {stdout}"
        elif stderr:
            warn_msg += f": {stderr}"
        return _HookCommandOutcome(_Outcome.WARN, warn_msg)
