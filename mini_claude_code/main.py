"""
main.py — CLI 入口: 组装所有模块 + REPL 循环 + Slash 命令

忠实还原 Claude Code 的 CLI 入口。
源码对照: rust/crates/rusty-claude-cli/src/main.rs (3100+ 行)

核心工程要点:
1. 组装点: main 是唯一知道所有模块的地方 (main.rs:2318-2336)
2. CliAction 分派: 枚举 + match 替代 if-elif 链 (main.rs:64-93)
3. REPL 循环: 持续读输入 + 分派处理 (main.rs:935-973)
4. Slash command: /help, /status, /compact 等内置命令 (main.rs:1140-1225)
5. CliPermissionPrompter: 终端交互式权限询问 (main.rs:2338-2382)
6. CliToolExecutor: 包装 ToolRegistry + 输出渲染 (main.rs:3027-3077)
7. Session 持久化: 每次 turn 后自动保存 (main.rs:1227-1230)
"""

import os
import sys
import uuid
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# 加载 .env 文件 — 从 main.py 所在目录查找
load_dotenv(Path(__file__).parent / ".env")

from mini_claude_code.compact import CompactionConfig, compact_session, estimate_session_tokens
from mini_claude_code.config import ConfigLoader, RuntimeConfig
from mini_claude_code.hooks import HookRunner
from mini_claude_code.models import Message, Session, TextContentBlock
from mini_claude_code.permissions import (
    PermissionMode,
    PermissionPolicy,
    PermissionRequest,
    PermissionResult,
)
from mini_claude_code.prompt import ProjectContext, SystemPromptBuilder
from mini_claude_code.runtime import (
    ConversationRuntime,
    ToolError,
    TurnSummary,
    UsageTracker,
)
from mini_claude_code.storage import SessionStore
from mini_claude_code.tools import ToolRegistry, bash_tool, read_tool, write_tool


# ============================================================
# 常量
# 源码: main.rs:37-47
# ============================================================

DEFAULT_MODEL = "claude-sonnet-4-6"
VERSION = "0.1.0"


# ============================================================
# SlashCommand — 内置斜杠命令
# 源码: main.rs:43-70 (app.rs 中的 SlashCommand enum)
#
# CC 有 ~15 个 slash command。我们实现最核心的 5 个:
# /help, /status, /compact, /exit, /model
# ============================================================

class SlashCommand(Enum):
    HELP = auto()
    STATUS = auto()
    COMPACT = auto()
    EXIT = auto()
    UNKNOWN = auto()


def parse_slash_command(input_text: str) -> Optional[tuple[SlashCommand, Optional[str]]]:
    """源码: app.rs:52-69 (SlashCommand::parse)

    返回 (command, optional_arg) 或 None (不是 slash command)。
    """
    trimmed = input_text.strip()
    if not trimmed.startswith("/"):
        return None

    parts = trimmed[1:].split(None, 1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else None

    mapping = {
        "help": SlashCommand.HELP,
        "status": SlashCommand.STATUS,
        "compact": SlashCommand.COMPACT,
        "exit": SlashCommand.EXIT,
        "quit": SlashCommand.EXIT,
    }
    return (mapping.get(cmd, SlashCommand.UNKNOWN), arg)


# ============================================================
# CliPermissionPrompter — 终端交互式权限询问
# 源码: main.rs:2338-2382
#
# 当 PermissionPolicy.authorize() 需要用户确认时，
# 打印详细信息并等待 y/N 输入。
#
# 这是 PermissionPrompter Protocol 的"CLI 实现"。
# IDE 可以有 GUI 弹窗实现，Web 可以有 HTTP 实现。
# Protocol 的解耦让这种替换零成本。
# ============================================================

class CliPermissionPrompter:
    """源码: main.rs:2338-2382"""

    def decide(self, request: PermissionRequest) -> PermissionResult:
        """终端交互询问。源码: main.rs:2348-2381"""
        print()
        print("Permission approval required")
        print(f"  Tool             {request.tool_name}")
        print(f"  Current mode     {request.current_mode.as_str()}")
        print(f"  Required mode    {request.required_mode.as_str()}")
        print(f"  Input            {request.input}")

        try:
            response = input("Approve this tool call? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return PermissionResult.deny(
                f"tool '{request.tool_name}' denied: input interrupted"
            )

        if response in ("y", "yes"):
            return PermissionResult.allow()
        return PermissionResult.deny(
            f"tool '{request.tool_name}' denied by user approval prompt"
        )


# ============================================================
# CliToolExecutor — 工具执行器
# 源码: main.rs:3027-3077
#
# 包装 ToolRegistry，额外功能:
# 1. 执行前打印工具名
# 2. 执行后打印结果/错误
#
# 实现 ToolExecutor Protocol: execute(tool_name, input) -> str
# ============================================================

class CliToolExecutor:
    """源码: main.rs:3027-3077"""

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def execute(self, tool_name: str, input_str: str) -> str:
        """源码: main.rs:3044-3076"""
        # 打印执行信息 — CC 的 emit_output 渲染
        print(f"\n  \033[2m[tool: {tool_name}]\033[0m")

        result = self._registry.execute(tool_name, input_str)

        # 检查是否为错误
        if isinstance(result, str) and result.startswith("ERROR:"):
            print(f"  \033[31m{result}\033[0m")
            raise ToolError(result)

        # 截断长输出的显示
        display = result if len(result) <= 200 else result[:200] + "..."
        print(f"  \033[2m{display}\033[0m")
        return result


# ============================================================
# build_runtime — 组装 ConversationRuntime
# 源码: main.rs:2318-2336
#
# 这是唯一的"组装点"(Composition Root)。
# 其他模块不互相导入 — 只有 main 知道所有模块的存在。
# 这是依赖注入的最佳实践: 在最外层组装，在内层使用接口。
# ============================================================

def build_runtime(
    session: Session,
    api_client,
    registry: ToolRegistry,
    permission_mode: PermissionMode,
    system_prompt: list[str],
    hook_runner: Optional[HookRunner] = None,
) -> ConversationRuntime:
    """源码: main.rs:2318-2336"""
    # 构建权限策略 — main.rs:3079-3085
    # CC 从 tool_specs 批量注册。我们简化为手动注册常用工具权限。
    policy = (
        PermissionPolicy(permission_mode)
        .with_tool_requirement("read_file", PermissionMode.READ_ONLY)
        .with_tool_requirement("glob_search", PermissionMode.READ_ONLY)
        .with_tool_requirement("grep_search", PermissionMode.READ_ONLY)
        .with_tool_requirement("write_file", PermissionMode.WORKSPACE_WRITE)
        .with_tool_requirement("edit_file", PermissionMode.WORKSPACE_WRITE)
        .with_tool_requirement("bash", PermissionMode.DANGER_FULL_ACCESS)
    )

    tool_executor = CliToolExecutor(registry)

    return ConversationRuntime(
        session=session,
        api_client=api_client,
        tool_executor=tool_executor,
        permission_policy=policy,
        system_prompt=system_prompt,
        hook_runner=hook_runner,
    )


# ============================================================
# build_system_prompt — 加载系统提示词
# 源码: main.rs:2300-2307
# ============================================================

def build_system_prompt(cwd: Path) -> list[str]:
    """源码: main.rs:2300-2307"""
    import platform
    from datetime import date
    os_name = platform.system().lower()

    context = ProjectContext.discover(cwd, date.today().isoformat())
    return (
        SystemPromptBuilder()
        .with_os(os_name, platform.release())
        .with_project_context(context)
        .build()
    )


# ============================================================
# build_default_registry — 注册默认工具
# ============================================================

def build_default_registry() -> ToolRegistry:
    """注册所有默认工具。"""
    registry = ToolRegistry()
    registry.register("bash", bash_tool)
    registry.register("read_file", read_tool)
    registry.register("write_file", write_tool)
    return registry


# ============================================================
# 工具定义 — 传给 Anthropic API 的 JSON Schema
# 源码: main.rs:2330 + tools/lib.rs (mvp_tool_specs)
#
# LLM 需要看到工具的名称、描述、参数 schema，
# 才知道可以调哪些工具、怎么调。
# CC 从 ToolSpec 的 inputSchema 自动生成。
# 我们手动定义 3 个核心工具。
# ============================================================

DEFAULT_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "bash",
        "description": (
            "Execute a shell command and return its output. "
            "Use this for running programs, checking files, git operations, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                }
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file at the given path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file to read",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file at the given path. Creates the file if it doesn't exist, overwrites if it does.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
            "required": ["path", "content"],
        },
    },
]


# ============================================================
# format_status — /status 命令输出
# 源码: main.rs:1232-1250 (print_status)
# ============================================================

def format_status(
    model: str,
    permission_mode: PermissionMode,
    runtime: ConversationRuntime,
) -> str:
    """源码: main.rs:1232-1250"""
    cumulative = runtime.usage().cumulative_usage()
    session = runtime.session()
    estimated = estimate_session_tokens(session.messages)
    lines = [
        "Session status:",
        f"  Model              {model}",
        f"  Permission mode    {permission_mode.as_str()}",
        f"  Messages           {len(session.messages)}",
        f"  Turns              {runtime.usage().turns()}",
        f"  Estimated tokens   {estimated:,}",
        f"  Input tokens       {cumulative.input_tokens:,}",
        f"  Output tokens      {cumulative.output_tokens:,}",
    ]
    return "\n".join(lines)


# ============================================================
# startup_banner — 启动信息
# 源码: main.rs:1028-1051
# ============================================================

def startup_banner(
    model: str,
    permission_mode: PermissionMode,
    cwd: Path,
    session_id: str,
) -> str:
    """源码: main.rs:1028-1051"""
    return (
        "\n"
        "\033[38;5;33m"
        " __  __ _       _    ____ ____\n"
        "|  \\/  (_)_ __ (_)  / ___/ ___|\n"
        "| |\\/| | | '_ \\| | | |   | |\n"
        "| |  | | | | | | | | |___| |___\n"
        "|_|  |_|_|_| |_|_|  \\____\\____|\n"
        "\033[0m\n"
        f"  \033[2mModel\033[0m            {model}\n"
        f"  \033[2mPermissions\033[0m      {permission_mode.as_str()}\n"
        f"  \033[2mDirectory\033[0m        {cwd}\n"
        f"  \033[2mSession\033[0m          {session_id}\n"
        "\n"
        "  Type \033[1m/help\033[0m for commands\n"
    )


# ============================================================
# render_help — /help 命令输出
# ============================================================

def render_help() -> str:
    return (
        "Available commands:\n"
        "  /help       Show this help message\n"
        "  /status     Show session status\n"
        "  /compact    Compact conversation history\n"
        "  /exit       Exit the session\n"
    )


# ============================================================
# run_repl — REPL 主循环
# 源码: main.rs:935-973
#
# 关键细节:
# 1. Ctrl+C 不退出 — 只取消当前输入 (main.rs:964)
# 2. Ctrl+D (EOF) 退出 (main.rs:965-968)
# 3. 空行跳过 (main.rs:948-950)
# 4. /exit 持久化后退出 (main.rs:951-954)
# 5. / 开头走 slash command (main.rs:955-959)
# 6. 普通文本走 run_turn (main.rs:962)
# 7. 每次 turn 后持久化 (main.rs:1077)
# ============================================================

def run_repl(
    model: str,
    permission_mode: PermissionMode,
    api_client,
    registry: Optional[ToolRegistry] = None,
    storage_dir: Optional[Path] = None,
) -> None:
    """源码: main.rs:935-973"""
    cwd = Path.cwd()

    # 构建各组件
    if registry is None:
        registry = build_default_registry()
    system_prompt = build_system_prompt(cwd)
    session_id = uuid.uuid4().hex[:12]
    session = Session()

    # 存储
    store_dir = storage_dir or Path.home() / ".mini-claude-code" / "sessions"
    store = SessionStore(store_dir)

    # Hook
    try:
        config = ConfigLoader.default_for(cwd).load()
        hook_runner = HookRunner(
            pre_tool_use=config.hooks_pre(),
            post_tool_use=config.hooks_post(),
        )
    except Exception:
        hook_runner = HookRunner()

    # 组装 runtime — 唯一的组装点
    runtime = build_runtime(
        session=session,
        api_client=api_client,
        registry=registry,
        permission_mode=permission_mode,
        system_prompt=system_prompt,
        hook_runner=hook_runner,
    )

    prompter = CliPermissionPrompter()

    # 打印 banner — main.rs:942
    print(startup_banner(model, permission_mode, cwd, session_id))

    # REPL 循环 — main.rs:944-970
    last_uuid: Optional[str] = None

    while True:
        try:
            user_input = input("\033[1m> \033[0m")
        except KeyboardInterrupt:
            # Ctrl+C: 取消当前输入，不退出 — main.rs:964
            print()
            continue
        except EOFError:
            # Ctrl+D: 退出 — main.rs:965-968
            print("\nGoodbye!")
            break

        trimmed = user_input.strip()
        if not trimmed:
            continue

        # Slash command 分派 — main.rs:951-959
        parsed = parse_slash_command(trimmed)
        if parsed is not None:
            cmd, arg = parsed

            if cmd == SlashCommand.EXIT:
                print("Goodbye!")
                break

            elif cmd == SlashCommand.HELP:
                print(render_help())
                continue

            elif cmd == SlashCommand.STATUS:
                print(format_status(model, permission_mode, runtime))
                continue

            elif cmd == SlashCommand.COMPACT:
                # 手动压缩 — main.rs:1181-1184
                before = len(runtime.session().messages)
                result = compact_session(
                    runtime.session().messages,
                    CompactionConfig(max_estimated_tokens=0),
                )
                if result.removed_count > 0:
                    runtime.session().messages = result.compacted_messages
                    print(
                        f"Compacted: {result.removed_count} messages removed "
                        f"({before} → {len(runtime.session().messages)})"
                    )
                else:
                    print("Nothing to compact.")
                continue

            elif cmd == SlashCommand.UNKNOWN:
                print(f"Unknown command: {trimmed}")
                continue

        # 普通 prompt — main.rs:962
        try:
            summary = runtime.run_turn(trimmed, prompter=prompter)
        except RuntimeError as e:
            print(f"\n\033[31mError: {e}\033[0m")
            continue

        # 文本已由 api_client 流式输出，不需要再打印

        # 自动压缩提示 — main.rs:1071-1076
        if summary.auto_compacted:
            print(
                "\n\033[2m[Auto-compacted: conversation history was "
                "compressed to stay within context limits]\033[0m"
            )

        # 持久化 — main.rs:1077
        for msg in [Message.user_text(trimmed)] + summary.assistant_messages + summary.tool_results:
            last_uuid = store.save_message(session_id, msg, parent_uuid=last_uuid)


# ============================================================
# CliAction — CLI 动作分派
# 源码: main.rs:96-126
# ============================================================

class CliAction(Enum):
    REPL = auto()
    PROMPT = auto()
    HELP = auto()
    VERSION = auto()


def parse_args(args: list[str]) -> tuple[CliAction, dict]:
    """源码: main.rs:147-289 (parse_args)

    简化版: 只支持核心参数:
      (无参数)        → REPL
      --version       → VERSION
      --help          → HELP
      --model MODEL   → 设置模型
      "prompt text"   → PROMPT
    """
    model = DEFAULT_MODEL
    permission_mode = PermissionMode.WORKSPACE_WRITE
    prompt_parts: list[str] = []
    i = 0

    while i < len(args):
        arg = args[i]

        if arg in ("--version", "-V"):
            return CliAction.VERSION, {}

        elif arg in ("--help", "-h"):
            return CliAction.HELP, {}

        elif arg == "--model":
            if i + 1 >= len(args):
                print("error: missing value for --model", file=sys.stderr)
                sys.exit(1)
            model = _resolve_model_alias(args[i + 1])
            i += 2
            continue

        elif arg.startswith("--model="):
            model = _resolve_model_alias(arg[8:])

        elif arg == "--dangerously-skip-permissions":
            permission_mode = PermissionMode.DANGER_FULL_ACCESS

        elif arg == "--permission-mode":
            if i + 1 >= len(args):
                print("error: missing value for --permission-mode", file=sys.stderr)
                sys.exit(1)
            permission_mode = _parse_permission_mode(args[i + 1])
            i += 2
            continue

        elif not arg.startswith("-"):
            prompt_parts.append(arg)

        i += 1

    if prompt_parts:
        return CliAction.PROMPT, {
            "prompt": " ".join(prompt_parts),
            "model": model,
            "permission_mode": permission_mode,
        }

    return CliAction.REPL, {
        "model": model,
        "permission_mode": permission_mode,
    }


def _resolve_model_alias(model: str) -> str:
    """源码: main.rs:291-298"""
    aliases = {
        "opus": "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5-20251213",
    }
    return aliases.get(model, model)


def _parse_permission_mode(value: str) -> PermissionMode:
    """源码: main.rs:300+ (parse_permission_mode_arg)"""
    mapping = {
        "read-only": PermissionMode.READ_ONLY,
        "workspace-write": PermissionMode.WORKSPACE_WRITE,
        "danger-full-access": PermissionMode.DANGER_FULL_ACCESS,
    }
    mode = mapping.get(value.lower())
    if mode is None:
        print(
            f"error: unsupported permission mode '{value}'. "
            f"Use: read-only, workspace-write, danger-full-access",
            file=sys.stderr,
        )
        sys.exit(1)
    return mode


# ============================================================
# main — 入口
# 源码: main.rs:53-93
# ============================================================

def main() -> None:
    """源码: main.rs:53-93"""
    args = sys.argv[1:]
    action, params = parse_args(args)

    if action == CliAction.VERSION:
        print(f"mini-claude-code v{VERSION}")
        return

    if action == CliAction.HELP:
        print(
            f"mini-claude-code v{VERSION}\n"
            "\n"
            "Usage:\n"
            "  mini-cc                          Start interactive REPL\n"
            "  mini-cc \"prompt\"                  One-shot prompt\n"
            "  mini-cc --model opus \"prompt\"     Use specific model\n"
            "\n"
            "Options:\n"
            "  --model MODEL                    Model name or alias (opus/sonnet/haiku)\n"
            "  --permission-mode MODE           read-only/workspace-write/danger-full-access\n"
            "  --dangerously-skip-permissions   Skip all permission checks\n"
            "  --version, -V                    Show version\n"
            "  --help, -h                       Show this help\n"
        )
        return

    model = params.get("model", DEFAULT_MODEL)
    permission_mode = params.get("permission_mode", PermissionMode.WORKSPACE_WRITE)

    # API 客户端: 尝试导入真实客户端，失败则提示
    api_client = _create_api_client(model)
    if api_client is None:
        return

    if action == CliAction.PROMPT:
        # 单次 prompt — main.rs:81-83
        prompt = params["prompt"]
        registry = build_default_registry()
        system_prompt = build_system_prompt(Path.cwd())
        runtime = build_runtime(
            session=Session(),
            api_client=api_client,
            registry=registry,
            permission_mode=permission_mode,
            system_prompt=system_prompt,
        )
        prompter = CliPermissionPrompter()

        try:
            summary = runtime.run_turn(prompt, prompter=prompter)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        # 文本已由 api_client 流式输出
        return

    # REPL — main.rs:90
    run_repl(
        model=model,
        permission_mode=permission_mode,
        api_client=api_client,
    )


def _create_api_client(model: str):
    """尝试创建 API 客户端，注入工具定义。"""
    try:
        from mini_claude_code.api_client import ClaudeApiClient
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            print(
                "error: ANTHROPIC_API_KEY not set. "
                "Set it with: export ANTHROPIC_API_KEY=your-key",
                file=sys.stderr,
            )
            return None
        return ClaudeApiClient(
            api_key=api_key,
            model=model,
            tools=DEFAULT_TOOL_DEFINITIONS,
        )
    except ImportError:
        print(
            "error: anthropic package not installed. "
            "Install it with: pip install anthropic",
            file=sys.stderr,
        )
        return None


if __name__ == "__main__":
    main()
