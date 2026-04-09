"""
Tutorial 06: System Prompt Builder — 给 AI 写"角色说明书"
==========================================================

当你打开 Claude Code，AI 怎么知道自己是个编程助手？
怎么知道当前目录是什么？怎么知道有哪些规则要遵守？

答案是：System Prompt（系统提示词）。

打个比方：
  - 你去餐厅，服务员知道自己要端盘子、推荐菜品 —— 因为经理给了他"岗位说明书"
  - AI 知道自己要帮你写代码、遵守安全规则 —— 因为我们给了它"系统提示词"

System Prompt 是 AI 看到的第一段文字，在用户说话之前就已经存在。
它告诉 AI：你是谁、你能做什么、当前环境是什么、有哪些规则。

本教程会教你：
1. System Prompt 的分层结构
2. CLAUDE.md 文件是怎么被发现和加载的
3. 项目上下文（git status 等）是怎么注入的
4. Builder 模式 —— 一步步构造复杂对象

对应源码：rust/crates/runtime/src/prompt.rs

运行方式：python tutorials/06_system_prompt_builder.py
"""

import os
import subprocess
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ============================================================
# 第一步：理解 System Prompt 的分层结构
# ============================================================
# Claude Code 的 system prompt 不是一段硬编码的文字，
# 而是由很多"层"拼接起来的。就像搭积木：
#
#  ┌──────────────────────────────────────┐
#  │ 1. 角色介绍 (Intro)                  │  ← 固定的：你是编程助手
#  ├──────────────────────────────────────┤
#  │ 2. 系统规则 (System)                  │  ← 固定的：工具使用规则等
#  ├──────────────────────────────────────┤
#  │ 3. 任务准则 (Doing Tasks)             │  ← 固定的：怎么做任务
#  ├──────────────────────────────────────┤
#  │ 4. 操作安全 (Actions)                 │  ← 固定的：注意安全
#  ├──────────────────────────────────────┤
#  │ ═══ 动态分界线 (DYNAMIC BOUNDARY) ══ │  ← 分界线：上面固定，下面动态
#  ├──────────────────────────────────────┤
#  │ 5. 环境信息 (Environment)             │  ← 动态：OS、日期、目录
#  ├──────────────────────────────────────┤
#  │ 6. 项目上下文 (Project Context)       │  ← 动态：git status
#  ├──────────────────────────────────────┤
#  │ 7. 指令文件 (CLAUDE.md 等)           │  ← 动态：用户的自定义规则
#  ├──────────────────────────────────────┤
#  │ 8. 运行时配置 (Runtime Config)        │  ← 动态：settings.json
#  └──────────────────────────────────────┘
#
# 为什么分"固定"和"动态"？
# 因为 AI API 有一个叫"缓存"的优化：固定不变的部分可以被缓存，
# 不需要每次都重新处理，这样速度更快、成本更低。
# DYNAMIC_BOUNDARY 就是告诉 API："这条线以上的部分可以缓存"。


# ============================================================
# 第二步：CLAUDE.md 文件发现机制
# ============================================================
# Claude Code 会从你的项目目录开始，一路向上查找 CLAUDE.md 文件。
#
# 比如你在 /home/user/projects/my-app/src/ 目录下工作：
#   先找 /home/user/projects/my-app/src/CLAUDE.md
#   再找 /home/user/projects/my-app/src/CLAUDE.local.md
#   再找 /home/user/projects/my-app/src/.claude/CLAUDE.md
#   再找 /home/user/projects/my-app/src/.claude/instructions.md
#   然后往上一级 /home/user/projects/my-app/ 继续找...
#   一直到根目录 /
#
# 这样做的好处：
# - 根目录的 CLAUDE.md 可以设置全局规则（整个项目都遵守）
# - 子目录的 CLAUDE.md 可以覆盖或补充规则（某个模块的特殊规则）

MAX_INSTRUCTION_FILE_CHARS = 4000    # 单个文件最多 4000 字符
MAX_TOTAL_INSTRUCTION_CHARS = 12000  # 所有文件总共最多 12000 字符


@dataclass(frozen=True)
class ContextFile:
    """一个被发现的指令文件"""
    path: str      # 文件路径
    content: str   # 文件内容


def discover_instruction_files(cwd: str) -> list[ContextFile]:
    """
    从当前目录到根目录，逐级查找 CLAUDE.md 等指令文件。

    对应源码: prompt.rs:192-213 (discover_instruction_files)
    """
    # 构建从当前目录到根目录的路径链
    directories = []
    current = Path(cwd).resolve()
    while True:
        directories.append(current)
        parent = current.parent
        if parent == current:  # 到达根目录
            break
        current = parent
    directories.reverse()  # 从根目录开始（最外层先加载）

    files: list[ContextFile] = []
    for directory in directories:
        # 每个目录下查找这 4 种文件
        candidates = [
            directory / "CLAUDE.md",
            directory / "CLAUDE.local.md",
            directory / ".claude" / "CLAUDE.md",
            directory / ".claude" / "instructions.md",
        ]
        for candidate in candidates:
            if candidate.is_file():
                try:
                    content = candidate.read_text(encoding="utf-8")
                    if content.strip():  # 跳过空文件
                        files.append(ContextFile(
                            path=str(candidate),
                            content=content,
                        ))
                except (PermissionError, OSError):
                    pass

    # 去重：如果两个文件内容一样（忽略空行差异），只保留第一个
    return _dedupe_instruction_files(files)


def _dedupe_instruction_files(files: list[ContextFile]) -> list[ContextFile]:
    """
    去重：内容相同的文件只保留第一个。

    为什么需要去重？
    有时候项目根目录和子目录都有 CLAUDE.md，但内容完全一样。
    重复加载浪费 token（花更多钱），所以要去重。

    对应源码: prompt.rs:326-341
    """
    seen_hashes: set[str] = set()
    deduped: list[ContextFile] = []

    for file in files:
        # "标准化"：去掉多余空行和首尾空白，然后算哈希
        normalized = _normalize(file.content)
        content_hash = hashlib.sha256(normalized.encode()).hexdigest()

        if content_hash not in seen_hashes:
            seen_hashes.add(content_hash)
            deduped.append(file)

    return deduped


def _normalize(content: str) -> str:
    """标准化内容：连续空行压成一行，去掉首尾空白"""
    lines = content.splitlines()
    result = []
    last_blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and last_blank:
            continue
        result.append(line)
        last_blank = is_blank
    return "\n".join(result).strip()


def _truncate(content: str, max_chars: int) -> str:
    """截断过长的内容"""
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "\n\n[truncated]"


# ============================================================
# 第三步：读取 Git 状态
# ============================================================
# Claude Code 启动时会自动获取 git status 和 git diff，
# 这样 AI 就知道你当前改了哪些文件、有没有未提交的修改。

def read_git_status(cwd: str) -> Optional[str]:
    """读取 git status 快照"""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "status", "--short", "--branch"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        return output if output else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def read_git_diff(cwd: str) -> Optional[str]:
    """读取 git diff 快照（暂存和未暂存的修改）"""
    sections = []

    # 暂存的修改（git add 过的）
    try:
        staged = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if staged.returncode == 0 and staged.stdout.strip():
            sections.append(f"Staged changes:\n{staged.stdout.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # 未暂存的修改
    try:
        unstaged = subprocess.run(
            ["git", "diff"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if unstaged.returncode == 0 and unstaged.stdout.strip():
            sections.append(f"Unstaged changes:\n{unstaged.stdout.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "\n\n".join(sections) if sections else None


# ============================================================
# 第四步：ProjectContext — 收集项目信息
# ============================================================

@dataclass
class ProjectContext:
    """
    项目上下文 —— 描述当前项目的环境信息。

    对应源码: prompt.rs:48-55
    """
    cwd: str                                         # 当前工作目录
    current_date: str                                 # 今天日期
    git_status: Optional[str] = None                  # git 状态快照
    git_diff: Optional[str] = None                    # git diff 快照
    instruction_files: list[ContextFile] = field(default_factory=list)  # CLAUDE.md 文件列表

    @classmethod
    def discover(cls, cwd: str, current_date: str) -> "ProjectContext":
        """自动发现项目上下文"""
        instruction_files = discover_instruction_files(cwd)
        return cls(
            cwd=cwd,
            current_date=current_date,
            instruction_files=instruction_files,
        )

    @classmethod
    def discover_with_git(cls, cwd: str, current_date: str) -> "ProjectContext":
        """自动发现项目上下文（包含 git 信息）"""
        ctx = cls.discover(cwd, current_date)
        ctx.git_status = read_git_status(cwd)
        ctx.git_diff = read_git_diff(cwd)
        return ctx


# ============================================================
# 第五步：SystemPromptBuilder — 构造器（Builder 模式）
# ============================================================
# 什么是 Builder 模式？
#
# 想象你在外卖 APP 上点餐：
#   1. 选主食（汉堡）
#   2. 加配菜（薯条）
#   3. 加饮料（可乐）
#   4. 选酱料（番茄酱）
#   5. 下单！
#
# 每一步都是可选的，你可以选或不选。最后"下单"时把所有选择组合起来。
#
# Builder 模式就是这样：
#   builder = SystemPromptBuilder()
#   builder.with_os("linux", "6.8")           # 加操作系统信息
#   builder.with_project_context(context)      # 加项目上下文
#   prompt = builder.build()                   # 组装成最终结果
#
# 好处：
# - 每个部分都是可选的
# - 调用顺序无所谓
# - 不需要一个有 20 个参数的构造函数

DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


class SystemPromptBuilder:
    """
    System Prompt 构造器。

    通过链式调用逐步添加内容，最后 build() 组装成完整的 prompt。
    对应源码: prompt.rs:84-185
    """

    def __init__(self):
        self._os_name: Optional[str] = None
        self._os_version: Optional[str] = None
        self._project_context: Optional[ProjectContext] = None
        self._extra_sections: list[str] = []

    def with_os(self, os_name: str, os_version: str) -> "SystemPromptBuilder":
        """添加操作系统信息"""
        self._os_name = os_name
        self._os_version = os_version
        return self

    def with_project_context(self, ctx: ProjectContext) -> "SystemPromptBuilder":
        """添加项目上下文"""
        self._project_context = ctx
        return self

    def append_section(self, section: str) -> "SystemPromptBuilder":
        """追加自定义段落"""
        self._extra_sections.append(section)
        return self

    def build(self) -> list[str]:
        """
        组装所有部分，生成最终的 system prompt。

        返回一个字符串列表，每个元素是一个"段落"。
        对应源码: prompt.rs:134-156
        """
        sections = []

        # 第 1 层：角色介绍（固定）
        sections.append(
            "You are an interactive agent that helps users with software engineering tasks. "
            "Use the instructions below and the tools available to you to assist the user."
        )

        # 第 2 层：系统规则（固定）
        sections.append(self._system_section())

        # 第 3 层：任务准则（固定）
        sections.append(self._doing_tasks_section())

        # 第 4 层：操作安全（固定）
        sections.append(self._actions_section())

        # ═══ 动态分界线 ═══
        sections.append(DYNAMIC_BOUNDARY)

        # 第 5 层：环境信息（动态）
        sections.append(self._environment_section())

        # 第 6 层：项目上下文（动态）
        if self._project_context:
            sections.append(self._project_context_section())

            # 第 7 层：指令文件（动态）
            if self._project_context.instruction_files:
                sections.append(self._instruction_files_section())

        # 第 8 层：额外段落
        sections.extend(self._extra_sections)

        return sections

    def render(self) -> str:
        """把所有段落拼成一个完整的字符串（段落之间用两个换行分隔）"""
        return "\n\n".join(self.build())

    # ---- 各层的生成逻辑 ----

    def _system_section(self) -> str:
        return "\n".join([
            "# System",
            " - All text you output outside of tool use is displayed to the user.",
            " - Tools are executed in a user-selected permission mode.",
            " - The system may automatically compress prior messages as context grows.",
        ])

    def _doing_tasks_section(self) -> str:
        return "\n".join([
            "# Doing tasks",
            " - Read relevant code before changing it.",
            " - Do not add speculative abstractions or unrelated cleanup.",
            " - If an approach fails, diagnose the failure before switching tactics.",
            " - Be careful not to introduce security vulnerabilities.",
        ])

    def _actions_section(self) -> str:
        return "\n".join([
            "# Executing actions with care",
            "Carefully consider reversibility and blast radius.",
            "Local, reversible actions like editing files are usually fine.",
            "Actions that affect shared systems should be authorized by the user.",
        ])

    def _environment_section(self) -> str:
        cwd = self._project_context.cwd if self._project_context else "unknown"
        date = self._project_context.current_date if self._project_context else "unknown"
        os_name = self._os_name or "unknown"
        os_version = self._os_version or "unknown"
        return "\n".join([
            "# Environment context",
            f" - Model family: Claude",
            f" - Working directory: {cwd}",
            f" - Date: {date}",
            f" - Platform: {os_name} {os_version}",
        ])

    def _project_context_section(self) -> str:
        ctx = self._project_context
        lines = [
            "# Project context",
            f" - Today's date is {ctx.current_date}.",
            f" - Working directory: {ctx.cwd}",
        ]
        if ctx.instruction_files:
            lines.append(f" - Claude instruction files discovered: {len(ctx.instruction_files)}.")
        if ctx.git_status:
            lines.append("")
            lines.append("Git status snapshot:")
            lines.append(ctx.git_status)
        if ctx.git_diff:
            lines.append("")
            lines.append("Git diff snapshot:")
            lines.append(ctx.git_diff[:500])  # 截断过长的 diff
        return "\n".join(lines)

    def _instruction_files_section(self) -> str:
        ctx = self._project_context
        sections = ["# Claude instructions"]
        remaining = MAX_TOTAL_INSTRUCTION_CHARS

        for file in ctx.instruction_files:
            if remaining <= 0:
                sections.append("_Additional instruction content omitted after reaching the prompt budget._")
                break

            truncated = _truncate(file.content.strip(), min(MAX_INSTRUCTION_FILE_CHARS, remaining))
            remaining -= len(truncated)

            # 显示文件路径（只显示文件名）和作用域
            filename = Path(file.path).name
            scope = str(Path(file.path).parent)
            sections.append(f"## {filename} (scope: {scope})")
            sections.append(truncated)

        return "\n\n".join(sections)


# ============================================================
# 第六步：演示
# ============================================================

def main():
    print("=" * 60)
    print("Tutorial 06: System Prompt Builder 演示")
    print("=" * 60)

    # --- 1. 发现当前项目的 CLAUDE.md ---
    cwd = str(Path(__file__).resolve().parent.parent)  # 项目根目录
    print(f"\n项目根目录: {cwd}")

    ctx = ProjectContext.discover_with_git(cwd, "2026-04-02")
    print(f"发现的指令文件: {len(ctx.instruction_files)} 个")
    for f in ctx.instruction_files:
        print(f"  - {f.path} ({len(f.content)} 字符)")

    if ctx.git_status:
        print(f"\nGit status 快照 (前 200 字符):")
        print(f"  {ctx.git_status[:200]}")

    # --- 2. 用 Builder 构造 system prompt ---
    print("\n--- 构造 System Prompt ---")
    import platform
    prompt = (
        SystemPromptBuilder()
        .with_os(platform.system(), platform.release())
        .with_project_context(ctx)
        .append_section("# Language\nAlways respond in Chinese.")
        .build()
    )

    print(f"总共 {len(prompt)} 个段落:")
    for i, section in enumerate(prompt):
        # 显示每个段落的前 60 个字符
        preview = section.replace('\n', ' ')[:60]
        is_boundary = section == DYNAMIC_BOUNDARY
        marker = " ═══ 分界线 ═══" if is_boundary else ""
        print(f"  [{i}] {preview}...{marker}")

    # --- 3. 看看完整的 prompt 有多长 ---
    full_prompt = "\n\n".join(prompt)
    print(f"\n完整 prompt 长度: {len(full_prompt)} 字符")
    print(f"估算 token 数: ~{len(full_prompt) // 4} tokens")

    # --- 4. 看看 CLAUDE.md 怎么被注入的 ---
    print("\n--- CLAUDE.md 注入位置 ---")
    for i, section in enumerate(prompt):
        if "Claude instructions" in section:
            print(f"  在第 [{i}] 段落中找到 Claude instructions:")
            print(f"  {section[:200]}...")
            break

    # 解说
    print("\n" + "=" * 60)
    print("关键理解要点：")
    print("=" * 60)
    print("""
    1. System Prompt 是分层构建的：
       固定层（角色/规则/安全） + 动态层（环境/项目/CLAUDE.md/配置）
       中间有一条 DYNAMIC_BOUNDARY 分界线

    2. CLAUDE.md 发现机制：
       从项目目录到根目录逐级查找 4 种文件：
       CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md, .claude/instructions.md
       内容相同的文件会自动去重

    3. 有预算控制：
       单个文件最多 4000 字符，所有文件总共最多 12000 字符
       超出的部分会被截断（标记 [truncated]）

    4. Builder 模式：
       .with_os() → .with_project_context() → .append_section() → .build()
       每步都是可选的，最后 build() 组装成完整结果
       好处：不需要一个有 20 个参数的构造函数

    5. Git 信息会被注入到 prompt 中：
       AI 知道你改了哪些文件、哪些修改还没提交
       这样 AI 可以给出更精准的建议（比如提醒你提交代码）

    对应 Claude Code 源码:
    - prompt.rs:37-39   →  常量定义（BOUNDARY, MAX_CHARS 等）
    - prompt.rs:48-82   →  ProjectContext 和文件发现
    - prompt.rs:84-185  →  SystemPromptBuilder
    - prompt.rs:192-213 →  discover_instruction_files()
    - prompt.rs:326-341 →  dedupe_instruction_files()
    """)


if __name__ == "__main__":
    main()
