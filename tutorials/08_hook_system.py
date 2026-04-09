"""
Tutorial 08: Hook System — 工具执行的"保安和监控"
===================================================

什么是 Hook？
-------------
Hook 直译是"钩子"，在编程里的意思是：
  "在某个事件发生的前后，自动运行你预设的代码"

生活类比：
  你网购下单后（事件），快递公司会自动发短信通知你（Hook）。
  你进公司大门前（事件），保安会自动检查你的工牌（Hook）。

Claude Code 的 Hook 系统：
  - PreToolUse Hook: 工具执行前运行。可以阻止工具执行（像保安拦人）。
  - PostToolUse Hook: 工具执行后运行。可以检查结果、追加反馈（像质检员）。

Hook 本质上就是一段 shell 命令。Claude Code 在工具执行前后自动运行它。

使用场景举例：
  - 工具执行前：检查是否有危险命令（比如 rm -rf）
  - 工具执行后：自动格式化代码（比如 prettier）
  - 工具执行后：检查是否有 console.log 残留

对应源码：rust/crates/runtime/src/hooks.rs

运行方式：python tutorials/08_hook_system.py
"""

import subprocess
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ============================================================
# 第一步：Hook 事件类型
# ============================================================

class HookEvent(Enum):
    """
    Hook 可以挂载在哪些时间点。

    对应源码: hooks.rs:8-12
    """
    PRE_TOOL_USE = "PreToolUse"    # 工具执行前
    POST_TOOL_USE = "PostToolUse"  # 工具执行后


# ============================================================
# 第二步：Hook 执行结果
# ============================================================

@dataclass
class HookRunResult:
    """
    Hook 执行后的结果。

    denied: 是否阻止了工具执行
    messages: Hook 输出的消息列表（会显示给用户看）

    对应源码: hooks.rs:24-47
    """
    denied: bool = False
    messages: list[str] = field(default_factory=list)

    @staticmethod
    def allow(messages: list[str] | None = None) -> "HookRunResult":
        return HookRunResult(denied=False, messages=messages or [])


# ============================================================
# 第三步：理解 Hook 的退出码协议
# ============================================================
# Hook 是一个 shell 命令。它的退出码（exit code）决定了行为：
#
#   退出码 0 → 允许 (Allow)
#     工具可以执行。如果 hook 有标准输出（stdout），会作为附加消息。
#
#   退出码 2 → 拒绝 (Deny)
#     工具被阻止！不会执行。stdout 作为拒绝原因。
#
#   其他退出码 → 警告 (Warn)
#     工具照常执行，但会记录一条警告。
#
# 为什么用退出码？因为这样 Hook 可以是任何语言写的脚本，
# 不需要是 Python。只要能返回退出码就行。

class HookOutcome(Enum):
    ALLOW = "allow"
    DENY = "deny"
    WARN = "warn"


# ============================================================
# 第四步：HookRunner — 执行 Hook 的引擎
# ============================================================

@dataclass
class HookConfig:
    """
    Hook 配置 —— 告诉 HookRunner 有哪些 hook 命令。

    pre_tool_use: 工具执行前要运行的 shell 命令列表
    post_tool_use: 工具执行后要运行的 shell 命令列表

    对应源码: config.rs:48-52 (RuntimeHookConfig)
    """
    pre_tool_use: list[str] = field(default_factory=list)
    post_tool_use: list[str] = field(default_factory=list)


class HookRunner:
    """
    Hook 执行引擎。

    负责在工具执行前后运行 hook 命令，收集结果。
    对应源码: hooks.rs:49-206
    """

    def __init__(self, config: HookConfig):
        self.config = config

    def run_pre_tool_use(self, tool_name: str, tool_input: str) -> HookRunResult:
        """
        运行所有 PreToolUse hook。

        如果任何一个 hook 返回"拒绝"，工具就不会被执行。
        """
        return self._run_commands(
            event=HookEvent.PRE_TOOL_USE,
            commands=self.config.pre_tool_use,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=None,
            is_error=False,
        )

    def run_post_tool_use(
        self, tool_name: str, tool_input: str,
        tool_output: str, is_error: bool,
    ) -> HookRunResult:
        """
        运行所有 PostToolUse hook。

        如果任何一个 hook 返回"拒绝"，工具结果会被标记为错误。
        """
        return self._run_commands(
            event=HookEvent.POST_TOOL_USE,
            commands=self.config.post_tool_use,
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
    ) -> HookRunResult:
        """
        逐个运行 hook 命令。

        重要：如果任何一个 hook 返回"拒绝"(exit code 2)，
        后续的 hook 不再运行，直接返回拒绝结果。

        对应源码: hooks.rs:95-150
        """
        if not commands:
            return HookRunResult.allow()

        messages = []

        for command in commands:
            outcome, message = self._run_one_command(
                command=command,
                event=event,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=tool_output,
                is_error=is_error,
            )

            if outcome == HookOutcome.ALLOW:
                if message:
                    messages.append(message)

            elif outcome == HookOutcome.DENY:
                # 一个 hook 拒绝了 → 立刻返回，不再运行后续 hook
                deny_message = message or f"{event.value} hook denied tool `{tool_name}`"
                messages.append(deny_message)
                return HookRunResult(denied=True, messages=messages)

            elif outcome == HookOutcome.WARN:
                messages.append(message)

        return HookRunResult.allow(messages)

    def _run_one_command(
        self,
        command: str,
        event: HookEvent,
        tool_name: str,
        tool_input: str,
        tool_output: Optional[str],
        is_error: bool,
    ) -> tuple[HookOutcome, str]:
        """
        执行单个 hook 命令。

        通过环境变量和 stdin 传递上下文信息给 hook 脚本：
        - 环境变量：HOOK_EVENT, HOOK_TOOL_NAME, HOOK_TOOL_INPUT, ...
        - stdin：JSON 格式的完整上下文

        对应源码: hooks.rs:152-206
        """
        # 构造传给 hook 的 JSON 数据（通过 stdin 传入）
        payload = json.dumps({
            "hook_event_name": event.value,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_output": tool_output,
            "tool_result_is_error": is_error,
        })

        # 设置环境变量（hook 脚本可以直接读取）
        env = os.environ.copy()
        env["HOOK_EVENT"] = event.value
        env["HOOK_TOOL_NAME"] = tool_name
        env["HOOK_TOOL_INPUT"] = tool_input
        env["HOOK_TOOL_IS_ERROR"] = "1" if is_error else "0"
        if tool_output is not None:
            env["HOOK_TOOL_OUTPUT"] = tool_output

        try:
            result = subprocess.run(
                ["sh", "-lc", command],   # 用 shell 执行命令
                input=payload,            # 通过 stdin 传入 JSON
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )

            stdout = result.stdout.strip()
            exit_code = result.returncode

            if exit_code == 0:
                # 退出码 0 → 允许
                return (HookOutcome.ALLOW, stdout)

            elif exit_code == 2:
                # 退出码 2 → 拒绝
                return (HookOutcome.DENY, stdout)

            else:
                # 其他退出码 → 警告（允许执行，但记录警告）
                warning = f"Hook `{command}` exited with status {exit_code}; allowing tool execution to continue"
                if stdout:
                    warning += f": {stdout}"
                return (HookOutcome.WARN, warning)

        except subprocess.TimeoutExpired:
            return (HookOutcome.WARN, f"Hook `{command}` timed out")
        except Exception as e:
            return (HookOutcome.WARN, f"Hook `{command}` failed: {e}")


# ============================================================
# 第五步：Hook 在 Agentic Loop 中的位置
# ============================================================
# 回顾 Tutorial 05 的 run_turn()，Hook 插在权限检查之后、工具执行前后：
#
#   权限检查 → 通过
#     ↓
#   PreToolUse Hook → 如果拒绝 → 返回错误结果，不执行工具
#     ↓ （允许）
#   执行工具 → 得到输出
#     ↓
#   PostToolUse Hook → 追加反馈信息到输出
#     ↓
#   返回工具结果

def merge_hook_feedback(hook_messages: list[str], output: str, denied: bool) -> str:
    """
    把 Hook 的反馈信息合并到工具输出中。

    对应源码: conversation.rs:408-424
    """
    if not hook_messages:
        return output

    sections = []
    if output.strip():
        sections.append(output)

    label = "Hook feedback (denied)" if denied else "Hook feedback"
    sections.append(f"{label}:\n" + "\n".join(hook_messages))

    return "\n\n".join(sections)


# ============================================================
# 第六步：演示
# ============================================================

def main():
    print("=" * 60)
    print("Tutorial 08: Hook System 演示")
    print("=" * 60)

    # --- 1. 允许型 Hook（退出码 0）---
    print("\n--- 场景 1: 允许型 Hook ---")
    print("  Hook 命令: printf 'hook says hello'")
    print("  退出码: 0 → 允许执行")

    runner1 = HookRunner(HookConfig(
        pre_tool_use=["printf 'pre-hook: checked tool'"],
        post_tool_use=["printf 'post-hook: tool finished'"],
    ))

    pre_result = runner1.run_pre_tool_use("bash", '{"command": "ls"}')
    print(f"  PreToolUse 结果: denied={pre_result.denied}, messages={pre_result.messages}")

    post_result = runner1.run_post_tool_use("bash", '{"command": "ls"}', "file1.py", False)
    print(f"  PostToolUse 结果: denied={post_result.denied}, messages={post_result.messages}")

    # --- 2. 拒绝型 Hook（退出码 2）---
    print("\n--- 场景 2: 拒绝型 Hook ---")
    print("  Hook 命令: printf 'dangerous command blocked'; exit 2")
    print("  退出码: 2 → 拒绝执行")

    runner2 = HookRunner(HookConfig(
        pre_tool_use=["printf 'dangerous command blocked'; exit 2"],
    ))

    pre_result = runner2.run_pre_tool_use("bash", '{"command": "rm -rf /"}')
    print(f"  PreToolUse 结果: denied={pre_result.denied}, messages={pre_result.messages}")

    # --- 3. 多个 Hook 串联 ---
    print("\n--- 场景 3: 多个 Hook 串联（第二个拒绝）---")
    print("  Hook 1: printf 'first check ok'     → exit 0")
    print("  Hook 2: printf 'second check failed' → exit 2")
    print("  Hook 3: printf 'never reached'       → exit 0")

    runner3 = HookRunner(HookConfig(
        pre_tool_use=[
            "printf 'first check ok'",
            "printf 'second check failed'; exit 2",
            "printf 'never reached'",  # 这个不会执行！
        ],
    ))

    pre_result = runner3.run_pre_tool_use("bash", '{"command": "echo hello"}')
    print(f"  结果: denied={pre_result.denied}")
    print(f"  消息: {pre_result.messages}")
    print("  注意：第三个 hook 没有执行（因为第二个已经拒绝了）")

    # --- 4. Hook 读取环境变量 ---
    print("\n--- 场景 4: Hook 读取环境变量 ---")
    runner4 = HookRunner(HookConfig(
        pre_tool_use=["printf \"tool=%s input=%s\" \"$HOOK_TOOL_NAME\" \"$HOOK_TOOL_INPUT\""],
    ))

    pre_result = runner4.run_pre_tool_use("read_file", '{"path": "main.py"}')
    print(f"  Hook 输出: {pre_result.messages}")

    # --- 5. 合并 Hook 反馈到工具输出 ---
    print("\n--- 场景 5: 合并 Hook 反馈 ---")
    tool_output = "file1.py\nfile2.py"
    merged = merge_hook_feedback(
        ["pre-hook: verified", "post-hook: formatted"],
        tool_output,
        denied=False,
    )
    print(f"  原始输出:\n    {tool_output}")
    print(f"  合并后:\n    {merged.replace(chr(10), chr(10) + '    ')}")

    # --- 6. 没有 Hook 的情况 ---
    print("\n--- 场景 6: 没有配置 Hook ---")
    runner_empty = HookRunner(HookConfig())
    pre_result = runner_empty.run_pre_tool_use("bash", '{"command": "ls"}')
    print(f"  结果: denied={pre_result.denied}, messages={pre_result.messages}")
    print("  （没有 hook 就直接允许，messages 为空）")

    # 解说
    print("\n" + "=" * 60)
    print("关键理解要点：")
    print("=" * 60)
    print("""
    1. Hook 是工具执行前后自动运行的 shell 命令
       - PreToolUse: 执行前检查，可以阻止执行
       - PostToolUse: 执行后检查，可以追加反馈

    2. 退出码协议:
       0 → 允许 (Allow)     stdout 作为附加消息
       2 → 拒绝 (Deny)      stdout 作为拒绝原因，工具不执行
       其他 → 警告 (Warn)    允许执行，但记录警告

    3. 多个 Hook 串联:
       按顺序执行。一个拒绝 → 后面的都不运行。
       就像机场安检有多道关卡，第一关没过就不用去第二关了。

    4. Hook 接收信息的两种方式:
       - 环境变量: HOOK_TOOL_NAME, HOOK_TOOL_INPUT, ...
       - stdin: JSON 格式的完整上下文

    5. Hook 在 Agentic Loop 中的位置:
       权限检查 → PreToolUse Hook → 工具执行 → PostToolUse Hook
       Hook 的反馈会合并到工具输出中，AI 能看到

    6. 实际使用场景 (Claude Code settings.json):
       - tmux 提醒: 长命令建议用 tmux
       - git push 审查: push 前打开编辑器让你审查
       - console.log 警告: 编辑文件后检查是否有 console.log
       - Prettier 格式化: 编辑 JS/TS 后自动格式化

    对应 Claude Code 源码:
    - hooks.rs:8-12   →  HookEvent 枚举
    - hooks.rs:24-47  →  HookRunResult
    - hooks.rs:49-206 →  HookRunner
    - hooks.rs:152    →  run_command (单个 hook 的执行)
    - conversation.rs:228-255 →  Hook 在 Agentic Loop 中的调用
    """)


if __name__ == "__main__":
    main()
