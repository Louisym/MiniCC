"""
Tutorial 03: Tool System — AI 的工具箱
=======================================

上两个教程里，我们硬编码了一个 add 工具。
但真正的 Claude Code 有一套完整的工具系统：
  - 每个工具有名称、描述、参数定义
  - 有一个注册表管理所有工具
  - 工具的执行通过统一接口

本教程会教你：
1. 怎么定义一个工具（ToolSpec）
2. 怎么把工具注册到注册表里
3. 怎么通过统一接口执行工具
4. 真实的 Claude Code 内置了哪些工具

对应源码：rust/crates/tools/src/lib.rs

运行方式：python tutorials/03_tool_system.py
"""

import json
from dataclasses import dataclass, field
from typing import Callable, Any


# ============================================================
# 第一步：定义 ToolSpec（工具的"说明书"）
# ============================================================
# 每个工具需要告诉 AI 三件事：
#   1. 我叫什么名字 (name)
#   2. 我能干什么 (description)
#   3. 我需要什么参数 (input_schema)
#
# input_schema 用的是 JSON Schema 格式 —— 别被这个名词吓到，
# 它其实就是"参数说明书"，告诉 AI 每个参数是什么类型、是否必填。

@dataclass(frozen=True)
class ToolSpec:
    """
    工具的"说明书"。

    这个东西会被发送给 AI，让 AI 知道有哪些工具可以用。
    对应源码: tools/src/lib.rs 的 ToolSpec 结构体
    """
    name: str             # 工具名称，如 "bash"
    description: str      # 工具描述，告诉 AI 这个工具干什么
    input_schema: dict    # 参数说明（JSON Schema 格式）
    required_permission: str = "read_only"  # 需要什么权限（下一个教程讲）


# ============================================================
# 第二步：定义几个真实的工具
# ============================================================
# 以下是 Claude Code 真实存在的几个核心工具的简化版。

# 工具 1: Bash（执行 shell 命令）
BASH_SPEC = ToolSpec(
    name="bash",
    description="在当前工作目录执行一条 shell 命令。",
    input_schema={
        "type": "object",                    # 参数整体是一个"对象"（字典）
        "properties": {                      # 里面有哪些字段：
            "command": {"type": "string"},    #   command 字段，类型是字符串
            "timeout": {"type": "integer", "minimum": 1},  # 可选的超时时间
        },
        "required": ["command"],             # command 是必填的
    },
    required_permission="danger_full_access",  # 执行命令很危险，需要最高权限
)

# 工具 2: ReadFile（读取文件）
READ_FILE_SPEC = ToolSpec(
    name="read_file",
    description="读取工作目录中的一个文本文件。",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},                 # 文件路径
            "offset": {"type": "integer", "minimum": 0}, # 从第几行开始读
            "limit": {"type": "integer", "minimum": 1},  # 读多少行
        },
        "required": ["path"],
    },
    required_permission="read_only",  # 只是读文件，最低权限就行
)

# 工具 3: WriteFile（写入文件）
WRITE_FILE_SPEC = ToolSpec(
    name="write_file",
    description="在工作目录中写入一个文本文件。",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    required_permission="workspace_write",  # 需要写入权限
)

# 工具 4: Grep（搜索文件内容）
GREP_SPEC = ToolSpec(
    name="grep",
    description="在文件中搜索匹配的文本内容。",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},    # 搜索的正则表达式
            "path": {"type": "string"},       # 搜索的目录或文件
        },
        "required": ["pattern"],
    },
    required_permission="read_only",
)


# ============================================================
# 第三步：工具的实际执行函数
# ============================================================
# 每个工具除了"说明书"，还需要一个真正干活的函数。
# 在 Claude Code 里，这些函数在 file_ops.rs 和 bash.rs 中实现。
# 这里我们用 Python 写简化版。

import os
import subprocess
import tempfile
import glob as glob_module


def execute_bash(params: dict) -> str:
    """
    执行 shell 命令（简化版）。

    真实的 Claude Code 还有沙盒(sandbox)保护、超时控制、后台执行等功能。
    对应源码: rust/crates/runtime/src/bash.rs
    """
    command = params["command"]
    timeout = params.get("timeout", 5)  # 默认 5 秒超时

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,   # 捕获标准输出和标准错误
            text=True,             # 以文本模式（不是字节）
            timeout=timeout,
            cwd=os.getcwd(),
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]: {result.stderr}"
        return output.strip() if output.strip() else "(no output)"
    except subprocess.TimeoutExpired:
        return f"[error] 命令超时 ({timeout}s)"
    except Exception as e:
        return f"[error] {e}"


def execute_read_file(params: dict) -> str:
    """
    读取文件内容（简化版）。
    对应源码: rust/crates/runtime/src/file_ops.rs
    """
    path = params["path"]
    offset = params.get("offset", 0)
    limit = params.get("limit", 100)

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        selected = lines[offset:offset + limit]
        # 加上行号（和 cat -n 类似）
        numbered = [f"{i + offset + 1}\t{line}" for i, line in enumerate(selected)]
        return "".join(numbered)
    except FileNotFoundError:
        return f"[error] 文件不存在: {path}"
    except Exception as e:
        return f"[error] {e}"


def execute_write_file(params: dict) -> str:
    """
    写入文件内容（简化版）。
    对应源码: rust/crates/runtime/src/file_ops.rs
    """
    path = params["path"]
    content = params["content"]

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"[error] {e}"


def execute_grep(params: dict) -> str:
    """
    搜索文件内容（简化版）。
    对应源码: rust/crates/runtime/src/file_ops.rs
    """
    import re
    pattern = params["pattern"]
    search_path = params.get("path", ".")

    matches = []
    try:
        if os.path.isfile(search_path):
            files = [search_path]
        else:
            files = []
            for root, dirs, filenames in os.walk(search_path):
                for fname in filenames:
                    files.append(os.path.join(root, fname))
                if len(files) > 100:  # 限制搜索范围
                    break

        for fpath in files[:50]:
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for line_no, line in enumerate(f, 1):
                        if re.search(pattern, line):
                            matches.append(f"{fpath}:{line_no}: {line.rstrip()}")
                            if len(matches) >= 20:
                                break
            except (PermissionError, IsADirectoryError):
                continue
            if len(matches) >= 20:
                break
    except Exception as e:
        return f"[error] {e}"

    if not matches:
        return f"No matches found for pattern: {pattern}"
    return "\n".join(matches)


# ============================================================
# 第四步：ToolExecutor（工具执行器）—— 统一接口
# ============================================================
# 在 Claude Code 里，所有工具都通过一个统一的接口来执行。
# 这个接口叫 ToolExecutor。
#
# 为什么要统一接口？
# 因为 Agentic Loop 不关心具体是什么工具，它只需要：
#   "给我工具名和参数，我帮你执行，返回结果"
# 这样加新工具的时候，Agentic Loop 的代码完全不用改！

class ToolExecutor:
    """
    工具执行器 —— 管理和执行所有工具。

    它做两件事：
    1. 注册工具（把工具的名称和执行函数绑定起来）
    2. 执行工具（根据名称找到函数，传入参数，返回结果）

    对应源码: conversation.rs 中的 ToolExecutor trait
    以及 tools/src/lib.rs 中的 StaticToolExecutor
    """

    def __init__(self):
        # _handlers 是一个字典：工具名 → 执行函数
        # 就像一本电话簿：名字 → 电话号码
        self._handlers: dict[str, Callable[[dict], str]] = {}
        # _specs 保存工具的说明书
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec, handler: Callable[[dict], str]) -> "ToolExecutor":
        """
        注册一个工具。

        参数:
            spec: 工具说明书
            handler: 工具的执行函数
        返回:
            self（返回自身是为了支持链式调用，后面演示）
        """
        self._handlers[spec.name] = handler
        self._specs[spec.name] = spec
        return self  # 返回自身，这样可以连续调用 .register().register()

    def execute(self, tool_name: str, input_json: str) -> tuple[str, bool]:
        """
        执行一个工具。

        参数:
            tool_name: 工具名称
            input_json: 参数（JSON 字符串）
        返回:
            (输出结果, 是否出错)
        """
        if tool_name not in self._handlers:
            return (f"Unknown tool: {tool_name}", True)

        try:
            params = json.loads(input_json) if input_json else {}
        except json.JSONDecodeError:
            return (f"Invalid JSON input: {input_json}", True)

        try:
            result = self._handlers[tool_name](params)
            return (result, False)
        except Exception as e:
            return (f"Tool execution error: {e}", True)

    def get_specs(self) -> list[ToolSpec]:
        """获取所有已注册工具的说明书"""
        return list(self._specs.values())

    def get_spec(self, name: str) -> ToolSpec | None:
        """获取某个工具的说明书"""
        return self._specs.get(name)


# ============================================================
# 第五步：演示
# ============================================================

def main():
    print("=" * 60)
    print("Tutorial 03: Tool System 工具系统演示")
    print("=" * 60)

    # --- 1. 创建工具执行器并注册工具 ---
    # 注意这里的"链式调用"：.register().register().register()
    # 这就是"Builder 模式"的一种简单形式 —— 一行代码完成多步配置
    executor = (
        ToolExecutor()
        .register(BASH_SPEC, execute_bash)
        .register(READ_FILE_SPEC, execute_read_file)
        .register(WRITE_FILE_SPEC, execute_write_file)
        .register(GREP_SPEC, execute_grep)
    )

    print(f"\n已注册 {len(executor.get_specs())} 个工具:")
    for spec in executor.get_specs():
        print(f"  - {spec.name}: {spec.description} [权限: {spec.required_permission}]")

    # --- 2. 通过统一接口执行工具 ---
    print("\n--- 执行工具演示 ---")

    # 执行 bash 工具
    print("\n[1] 执行 bash: echo hello")
    output, is_error = executor.execute("bash", '{"command": "echo hello"}')
    print(f"    结果: {output}")
    print(f"    出错: {is_error}")

    # 执行 read_file 工具（读取当前教程文件的前 3 行）
    print("\n[2] 执行 read_file: 读取本文件前 3 行")
    this_file = os.path.abspath(__file__)
    output, is_error = executor.execute(
        "read_file",
        json.dumps({"path": this_file, "limit": 3}),
    )
    print(f"    结果:\n{output}")

    # 执行 write_file 工具
    print("\n[3] 执行 write_file: 写一个临时文件")
    tmp = os.path.join(tempfile.gettempdir(), "tutorial_test.txt")
    output, is_error = executor.execute(
        "write_file",
        json.dumps({"path": tmp, "content": "Hello from Tutorial 03!\n"}),
    )
    print(f"    结果: {output}")

    # 执行一个不存在的工具
    print("\n[4] 执行未知工具: unknown_tool")
    output, is_error = executor.execute("unknown_tool", '{}')
    print(f"    结果: {output}")
    print(f"    出错: {is_error}")

    # 清理
    if os.path.exists(tmp):
        os.remove(tmp)

    # --- 3. 查看工具的 input_schema ---
    print("\n--- 工具参数说明书 (input_schema) ---")
    bash_spec = executor.get_spec("bash")
    if bash_spec:
        print(f"  工具: {bash_spec.name}")
        print(f"  描述: {bash_spec.description}")
        print(f"  参数说明:")
        print(f"    {json.dumps(bash_spec.input_schema, indent=4)}")

    # 解说
    print("\n" + "=" * 60)
    print("关键理解要点：")
    print("=" * 60)
    print("""
    1. ToolSpec（工具说明书）告诉 AI 有哪些工具、怎么用
       - name: 工具名称
       - description: 干什么的
       - input_schema: 需要什么参数（JSON Schema 格式）

    2. ToolExecutor（工具执行器）是统一接口
       - register(): 注册工具
       - execute(name, input): 根据名称执行工具
       Agentic Loop 只需要调用 execute()，不关心具体工具实现

    3. 链式调用：executor.register(a).register(b).register(c)
       方法返回 self，就可以一行连续调用多个方法

    4. Claude Code 真实的内置工具:
       - bash: 执行 shell 命令（最危险，需要最高权限）
       - read_file: 读文件（最安全，只需只读权限）
       - write_file: 写文件（需要写入权限）
       - edit_file: 编辑文件的一部分（find-and-replace）
       - glob: 按文件名模式搜索文件
       - grep: 按内容搜索文件

    对应 Claude Code 源码:
    - tools/src/lib.rs:21-31   →  ToolSpec 定义
    - tools/src/lib.rs:60-170  →  mvp_tool_specs() 内置工具列表
    - conversation.rs:38-39    →  ToolExecutor trait 定义
    - conversation.rs:429-456  →  StaticToolExecutor 实现
    """)


if __name__ == "__main__":
    main()
