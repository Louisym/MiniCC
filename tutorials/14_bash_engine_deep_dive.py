"""
教程 14: Bash 执行引擎深度剖析
================================================================
源码对照: rust/crates/runtime/src/bash.rs + sandbox.rs

这不是"什么是子进程"的科普。这个教程还原 Claude Code 真正的
工程实现——沙箱如何隔离、超时如何控制、后台执行如何脱离。

核心问题: 当 AI 可以执行任意 shell 命令时，你怎么防止它
rm -rf / 或者 curl 恶意网站？答案不是一层防御，是四层。
================================================================
"""

import os
import sys
import json
import time
import signal
import hashlib
import tempfile
import subprocess
import asyncio
from pathlib import Path
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple


# ============================================================
# 第一部分: 沙箱系统 (sandbox.rs 完整还原)
# ============================================================
# 源码: rust/crates/runtime/src/sandbox.rs
#
# Claude Code 的沙箱不是一个简单的开关，而是一个多层决策系统:
# 1. 检测当前是否已经在容器里 (Docker/K8s/Podman)
# 2. 根据配置和请求参数决定要启用哪些隔离
# 3. 在 Linux 上用 unshare 创建命名空间隔离
# 4. 重定向 HOME/TMPDIR 到沙箱目录


class FilesystemIsolationMode(Enum):
    """文件系统隔离模式 — 源码 sandbox.rs:8-14

    三种模式对应不同的安全级别:
    - Off: 不隔离，命令可以读写任何地方
    - WorkspaceOnly: 默认值！只允许在工作目录内操作
    - AllowList: 白名单模式，只允许访问指定的目录
    """
    OFF = "off"
    WORKSPACE_ONLY = "workspace-only"      # 这是默认值
    ALLOW_LIST = "allow-list"


@dataclass
class SandboxConfig:
    """沙箱配置 — 源码 sandbox.rs:27-34

    来源: 用户的 .claude/settings.json 或项目的 .claude/settings.json
    所有字段都是 Optional，用 None 表示"使用默认值"
    """
    enabled: Optional[bool] = None           # 默认 True
    namespace_restrictions: Optional[bool] = None   # 默认 True
    network_isolation: Optional[bool] = None        # 默认 False（注意！）
    filesystem_mode: Optional[FilesystemIsolationMode] = None
    allowed_mounts: List[str] = field(default_factory=list)


@dataclass
class SandboxRequest:
    """解析后的沙箱请求 — 源码 sandbox.rs:36-43

    这是 SandboxConfig + 模型参数 合并后的最终请求。
    关键点: 模型的参数可以覆盖配置！这是一个有意的设计——
    模型可以请求 dangerouslyDisableSandbox=true 来禁用沙箱，
    但这会触发权限检查，用户需要批准。
    """
    enabled: bool = True
    namespace_restrictions: bool = True
    network_isolation: bool = False
    filesystem_mode: FilesystemIsolationMode = FilesystemIsolationMode.WORKSPACE_ONLY
    allowed_mounts: List[str] = field(default_factory=list)


@dataclass
class ContainerEnvironment:
    """容器环境检测结果 — 源码 sandbox.rs:46-49"""
    in_container: bool = False
    markers: List[str] = field(default_factory=list)


@dataclass
class SandboxStatus:
    """最终沙箱状态 — 源码 sandbox.rs:52-68

    这是一个详尽的状态报告，包含了所有决策结果。
    它会被附加到每个命令执行结果里（BashCommandOutput.sandbox_status）。
    这样你事后可以审计：这条命令到底跑在什么隔离环境里？
    """
    enabled: bool = False
    requested: Optional[SandboxRequest] = None
    supported: bool = False
    active: bool = False
    # 命名空间隔离
    namespace_supported: bool = False
    namespace_active: bool = False
    # 网络隔离
    network_supported: bool = False
    network_active: bool = False
    # 文件系统隔离
    filesystem_mode: FilesystemIsolationMode = FilesystemIsolationMode.WORKSPACE_ONLY
    filesystem_active: bool = False
    # 挂载白名单
    allowed_mounts: List[str] = field(default_factory=list)
    # 容器检测
    in_container: bool = False
    container_markers: List[str] = field(default_factory=list)
    # 降级原因（如果无法启用某些隔离）
    fallback_reason: Optional[str] = None


def resolve_request(
    config: SandboxConfig,
    # 以下参数来自模型的 BashCommandInput
    enabled_override: Optional[bool] = None,
    namespace_override: Optional[bool] = None,
    network_override: Optional[bool] = None,
    filesystem_mode_override: Optional[FilesystemIsolationMode] = None,
    allowed_mounts_override: Optional[List[str]] = None,
) -> SandboxRequest:
    """合并配置和请求参数 — 源码 sandbox.rs:86-106

    优先级: 模型请求参数 > 用户配置 > 默认值

    这个设计让模型可以在需要时请求不同的沙箱级别。
    比如模型需要安装一个 npm 包，它可以请求 network_isolation=False，
    但这个请求会经过权限系统审批。
    """
    return SandboxRequest(
        enabled=enabled_override if enabled_override is not None
                else (config.enabled if config.enabled is not None else True),
        namespace_restrictions=namespace_override if namespace_override is not None
                else (config.namespace_restrictions if config.namespace_restrictions is not None else True),
        network_isolation=network_override if network_override is not None
                else (config.network_isolation if config.network_isolation is not None else False),
        filesystem_mode=filesystem_mode_override if filesystem_mode_override is not None
                else (config.filesystem_mode if config.filesystem_mode is not None
                      else FilesystemIsolationMode.WORKSPACE_ONLY),
        allowed_mounts=allowed_mounts_override if allowed_mounts_override is not None
                else config.allowed_mounts,
    )


def detect_container_environment() -> ContainerEnvironment:
    """检测当前是否在容器中运行 — 源码 sandbox.rs:108-153

    为什么要检测容器？因为:
    1. 如果已经在 Docker 里，再用 unshare 做命名空间隔离可能不支持
    2. 容器里的权限模型不同，某些操作需要特殊处理
    3. 需要告知用户当前的安全环境

    检测方法有 5 种，不依赖任何单一信号——纵深检测:
    """
    markers = []

    # 信号 1: /.dockerenv 文件存在
    # Docker 会在容器根目录创建这个空文件
    if os.path.exists("/.dockerenv"):
        markers.append("/.dockerenv")

    # 信号 2: /run/.containerenv 文件存在
    # Podman 使用这个标记
    if os.path.exists("/run/.containerenv"):
        markers.append("/run/.containerenv")

    # 信号 3: 环境变量
    # 检查 CONTAINER, DOCKER, PODMAN, KUBERNETES_SERVICE_HOST
    for key, value in os.environ.items():
        normalized = key.lower()
        if normalized in ("container", "docker", "podman", "kubernetes_service_host"):
            if value:  # 非空
                markers.append(f"env:{key}={value}")

    # 信号 4: /proc/1/cgroup 内容
    # 在容器中，PID 1 的 cgroup 路径会包含容器运行时的标识
    try:
        with open("/proc/1/cgroup", "r") as f:
            cgroup_content = f.read()
        for needle in ["docker", "containerd", "kubepods", "podman", "libpod"]:
            if needle in cgroup_content:
                markers.append(f"/proc/1/cgroup:{needle}")
    except (FileNotFoundError, PermissionError):
        pass  # macOS 和 Windows 没有 /proc

    # 去重 + 排序（确保确定性）
    markers = sorted(set(markers))

    return ContainerEnvironment(
        in_container=len(markers) > 0,
        markers=markers,
    )


def command_exists(command: str) -> bool:
    """检查系统命令是否存在 — 源码 sandbox.rs:280-283"""
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    return any(os.path.exists(os.path.join(d, command)) for d in path_dirs)


def normalize_mounts(mounts: List[str], cwd: Path) -> List[str]:
    """将相对挂载路径转为绝对路径 — 源码 sandbox.rs:264-278"""
    result = []
    for mount in mounts:
        p = Path(mount)
        if p.is_absolute():
            result.append(str(p))
        else:
            result.append(str(cwd / p))
    return result


def resolve_sandbox_status(request: SandboxRequest, cwd: Path) -> SandboxStatus:
    """决定最终的沙箱状态 — 源码 sandbox.rs:162-208

    这是整个沙箱系统的核心决策函数。它综合考虑:
    - 请求的隔离级别
    - 系统是否支持（Linux + unshare）
    - 是否在容器中
    - 降级原因

    关键设计: 即使命名空间不可用，文件系统隔离仍然可以工作。
    这就是"优雅降级"——不是全有或全无。
    """
    container = detect_container_environment()

    # Linux 上是否有 unshare 命令
    is_linux = sys.platform.startswith("linux")
    namespace_supported = is_linux and command_exists("unshare")
    network_supported = namespace_supported  # 网络隔离也依赖 unshare

    # 文件系统隔离不依赖 unshare，任何平台都可以用
    filesystem_active = (
        request.enabled and
        request.filesystem_mode != FilesystemIsolationMode.OFF
    )

    # 收集降级原因
    fallback_reasons = []
    if request.enabled and request.namespace_restrictions and not namespace_supported:
        fallback_reasons.append(
            "namespace isolation unavailable (requires Linux with `unshare`)"
        )
    if request.enabled and request.network_isolation and not network_supported:
        fallback_reasons.append(
            "network isolation unavailable (requires Linux with `unshare`)"
        )
    if (request.enabled
        and request.filesystem_mode == FilesystemIsolationMode.ALLOW_LIST
        and not request.allowed_mounts):
        fallback_reasons.append(
            "filesystem allow-list requested without configured mounts"
        )

    # 最终判断: 沙箱是否真正生效
    active = (
        request.enabled
        and (not request.namespace_restrictions or namespace_supported)
        and (not request.network_isolation or network_supported)
    )

    return SandboxStatus(
        enabled=request.enabled,
        requested=request,
        supported=namespace_supported,
        active=active,
        namespace_supported=namespace_supported,
        namespace_active=request.enabled and request.namespace_restrictions and namespace_supported,
        network_supported=network_supported,
        network_active=request.enabled and request.network_isolation and network_supported,
        filesystem_mode=request.filesystem_mode,
        filesystem_active=filesystem_active,
        allowed_mounts=normalize_mounts(request.allowed_mounts, cwd),
        in_container=container.in_container,
        container_markers=container.markers,
        fallback_reason="; ".join(fallback_reasons) if fallback_reasons else None,
    )


@dataclass
class LinuxSandboxCommand:
    """Linux 沙箱启动命令 — 源码 sandbox.rs:78-83"""
    program: str
    args: List[str]
    env: Dict[str, str]


def build_linux_sandbox_command(
    command: str,
    cwd: Path,
    status: SandboxStatus
) -> Optional[LinuxSandboxCommand]:
    """构建 Linux 命名空间隔离命令 — 源码 sandbox.rs:211-262

    真正的隔离是靠 Linux 的 unshare 命令实现的。
    unshare 创建新的 namespace（命名空间），让进程运行在
    一个"假的"隔离环境中，类似轻量级容器。

    各 flag 的含义:
    --user          创建新的用户命名空间（进程觉得自己是 root，但实际没有特权）
    --map-root-user 将容器内的 root 映射到宿主机当前用户
    --mount         创建新的挂载命名空间（看不到宿主的挂载点）
    --ipc           创建新的 IPC 命名空间（隔离共享内存、信号量）
    --pid           创建新的 PID 命名空间（看不到宿主的进程）
    --uts           创建新的 UTS 命名空间（可以有不同的主机名）
    --fork          fork 后在新 namespace 中执行
    --net           创建新的网络命名空间（完全隔离网络，可选）
    """
    # 不是 Linux、没启用、或者命名空间和网络都没激活 → 不用沙箱
    if sys.platform != "linux":
        return None  # macOS 用 seatbelt (sandbox-exec)，这里不覆盖
    if not status.enabled:
        return None
    if not status.namespace_active and not status.network_active:
        return None

    args = [
        "--user",          # 用户命名空间
        "--map-root-user", # root 映射
        "--mount",         # 挂载命名空间
        "--ipc",           # IPC 命名空间
        "--pid",           # PID 命名空间
        "--uts",           # UTS 命名空间
        "--fork",          # fork 执行
    ]
    if status.network_active:
        args.append("--net")  # 网络命名空间（完全断网）

    args.extend(["sh", "-lc", command])

    # 环境变量——重定向 HOME 和 TMPDIR 到沙箱目录
    sandbox_home = str(cwd / ".sandbox-home")
    sandbox_tmp = str(cwd / ".sandbox-tmp")

    env = {
        "HOME": sandbox_home,
        "TMPDIR": sandbox_tmp,
        "CLAWD_SANDBOX_FILESYSTEM_MODE": status.filesystem_mode.value,
        "CLAWD_SANDBOX_ALLOWED_MOUNTS": ":".join(status.allowed_mounts),
    }
    # 保留 PATH
    if "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]

    return LinuxSandboxCommand(
        program="unshare",
        args=args,
        env=env,
    )


# ============================================================
# 第二部分: Bash 命令执行 (bash.rs 完整还原)
# ============================================================
# 源码: rust/crates/runtime/src/bash.rs
#
# 这个模块把沙箱、超时、后台执行三种策略组合在一起。
# 关键设计: 每种能力都是独立的层，可以任意组合。


@dataclass
class BashCommandInput:
    """Bash 命令输入 — 源码 bash.rs:18-34

    这是模型（AI）发送过来的工具调用参数。
    注意有多个沙箱相关的字段——模型可以精细控制沙箱行为。
    """
    command: str
    timeout: Optional[int] = None                  # 毫秒
    description: Optional[str] = None
    run_in_background: Optional[bool] = None
    dangerously_disable_sandbox: Optional[bool] = None
    namespace_restrictions: Optional[bool] = None
    isolate_network: Optional[bool] = None
    filesystem_mode: Optional[FilesystemIsolationMode] = None
    allowed_mounts: Optional[List[str]] = None


@dataclass
class BashCommandOutput:
    """Bash 命令输出 — 源码 bash.rs:36-65

    注意这个输出有多丰富。不只是 stdout/stderr，
    还有中断标记、后台任务ID、沙箱状态、持久化路径等。

    这些信息让模型能理解命令执行的完整上下文:
    - interrupted=True → 超时了，可能需要重试或换策略
    - background_task_id → 可以后续查询任务状态
    - sandbox_status → 知道命令在什么安全级别下运行的
    """
    stdout: str = ""
    stderr: str = ""
    interrupted: bool = False
    background_task_id: Optional[str] = None
    sandbox_status: Optional[SandboxStatus] = None
    return_code_interpretation: Optional[str] = None
    no_output_expected: Optional[bool] = None


def prepare_sandbox_dirs(cwd: Path):
    """创建沙箱目录 — 源码 bash.rs:236-239

    为什么需要创建目录？
    因为沙箱把 HOME 和 TMPDIR 重定向到这些目录。
    如果目录不存在，命令执行时 HOME 就是无效路径，
    很多程序（git, npm 等）会崩溃。
    """
    (cwd / ".sandbox-home").mkdir(exist_ok=True)
    (cwd / ".sandbox-tmp").mkdir(exist_ok=True)


def prepare_command(
    command: str,
    cwd: Path,
    sandbox_status: SandboxStatus,
    create_dirs: bool = True,
) -> Tuple[List[str], Dict[str, str]]:
    """构建最终要执行的命令 — 源码 bash.rs:182-207

    这里有一个关键的决策树:
    1. 如果 Linux + 命名空间可用 → 用 unshare 包装
    2. 否则 → 直接用 sh -lc 执行，但仍然做文件系统隔离

    即使不能用 unshare，文件系统隔离仍然生效：
    通过修改 HOME 和 TMPDIR 环境变量，限制程序能写的位置。
    这不是内核级隔离，但比什么都没有好很多。
    """
    if create_dirs:
        prepare_sandbox_dirs(cwd)

    # 尝试构建 Linux 沙箱命令
    launcher = build_linux_sandbox_command(command, cwd, sandbox_status)
    if launcher is not None:
        return [launcher.program] + launcher.args, launcher.env

    # 降级: 直接用 sh 执行，但重定向 HOME/TMPDIR
    cmd_args = ["sh", "-lc", command]
    env = dict(os.environ)
    if sandbox_status.filesystem_active:
        env["HOME"] = str(cwd / ".sandbox-home")
        env["TMPDIR"] = str(cwd / ".sandbox-tmp")

    return cmd_args, env


def execute_bash(inp: BashCommandInput) -> BashCommandOutput:
    """执行 Bash 命令 — 源码 bash.rs:67-100

    这是入口函数。它处理三种情况:
    1. 后台执行 → spawn 后立即返回 PID
    2. 有超时 → 用 asyncio.wait_for 包装
    3. 普通执行 → 同步等待完成

    在 Rust 源码中，它创建一个 tokio runtime 来支持异步超时。
    Python 中我们用 asyncio 模拟。
    """
    cwd = Path.cwd()

    # 第一步: 解析沙箱配置
    config = SandboxConfig()  # 实际从 ConfigLoader 加载
    request = resolve_request(
        config,
        enabled_override=(not inp.dangerously_disable_sandbox
                         if inp.dangerously_disable_sandbox is not None else None),
        namespace_override=inp.namespace_restrictions,
        network_override=inp.isolate_network,
        filesystem_mode_override=inp.filesystem_mode,
        allowed_mounts_override=inp.allowed_mounts,
    )
    sandbox_status = resolve_sandbox_status(request, cwd)

    # ========== 路径 1: 后台执行 ==========
    # 源码 bash.rs:71-96
    if inp.run_in_background:
        cmd_args, env = prepare_command(inp.command, cwd, sandbox_status, create_dirs=False)
        # 关键: stdin/stdout/stderr 全部设为 DEVNULL
        # 这让进程完全脱离——不占用终端、不阻塞管道
        process = subprocess.Popen(
            cmd_args,
            stdin=subprocess.DEVNULL,    # 不接受输入
            stdout=subprocess.DEVNULL,   # 不捕获输出
            stderr=subprocess.DEVNULL,   # 不捕获错误
            cwd=str(cwd),
            env=env,
        )
        return BashCommandOutput(
            background_task_id=str(process.pid),
            sandbox_status=sandbox_status,
            no_output_expected=True,
        )

    # ========== 路径 2 & 3: 前台执行（有/无超时）==========
    return _execute_bash_sync(inp, sandbox_status, cwd)


def _execute_bash_sync(
    inp: BashCommandInput,
    sandbox_status: SandboxStatus,
    cwd: Path,
) -> BashCommandOutput:
    """同步执行命令，支持超时 — 源码 bash.rs:102-165

    超时控制的关键:
    - Rust 用 tokio::time::timeout 包装 command.output()
    - Python 用 subprocess.run(timeout=...)
    - 超时后返回 interrupted=True + 特定错误消息
    - 超时不会 kill 进程！只是放弃等待。
      （Rust 的 tokio 版本在 drop future 时会清理子进程）
    """
    cmd_args, env = prepare_command(inp.command, cwd, sandbox_status)

    timeout_seconds = inp.timeout / 1000.0 if inp.timeout else None

    try:
        result = subprocess.run(
            cmd_args,
            capture_output=True,
            cwd=str(cwd),
            env=env,
            timeout=timeout_seconds,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")

        # 解释返回码 — 源码 bash.rs:140-146
        return_code_interpretation = None
        if result.returncode != 0:
            return_code_interpretation = f"exit_code:{result.returncode}"

        return BashCommandOutput(
            stdout=stdout,
            stderr=stderr,
            interrupted=False,
            sandbox_status=sandbox_status,
            return_code_interpretation=return_code_interpretation,
            no_output_expected=(not stdout.strip() and not stderr.strip()),
        )

    except subprocess.TimeoutExpired:
        # 超时处理 — 源码 bash.rs:112-130
        # 注意: 返回的消息格式是固定的，模型会解析这个消息
        return BashCommandOutput(
            stderr=f"Command exceeded timeout of {inp.timeout} ms",
            interrupted=True,
            sandbox_status=sandbox_status,
            return_code_interpretation="timeout",
            no_output_expected=True,
        )


# ============================================================
# 第三部分: Reference 中的高级特性（TypeScript 版本有、Rust 版本还没实现）
# ============================================================
# 这些是 TypeScript 产品版本中的高级工程技巧，
# 理解它们对设计自己的系统很有价值。

def demonstrate_shell_snapshot():
    """Shell 快照 — reference/06-bash-engine.md

    问题: 每次执行命令都用 sh -l 启动，会重新加载 .bashrc/.zshrc，
    这很慢（可能 200ms+），而且可能导致副作用。

    解决方案: 第一次命令前，捕获整个 shell 环境到一个临时文件，
    后续命令 source 这个文件而不是做完整的登录初始化。
    """
    print("\n=== Shell 快照机制 ===")

    # 第一步: 捕获当前 shell 的完整环境
    snapshot_path = tempfile.mktemp(suffix=".sh")

    # 捕获: PATH, 别名, 函数, 导出变量
    # 真实实现中会执行: env; alias; declare -f
    snapshot_content = f"""
# Shell 快照 — 自动生成，不要手动编辑
export PATH="{os.environ.get('PATH', '')}"
export HOME="{os.environ.get('HOME', '')}"
# alias ll='ls -la'  # 用户的别名也会被捕获
"""
    with open(snapshot_path, "w") as f:
        f.write(snapshot_content)

    print(f"快照保存到: {snapshot_path}")

    # 第二步: 后续命令用 source 快照而不是 sh -l
    # 不用 -l 就不会重新加载 .bashrc，快很多
    actual_command = "echo hello"
    wrapped = f"source {snapshot_path} 2>/dev/null || true && eval '{actual_command}'"
    print(f"包装后的命令: {wrapped}")

    # 清理
    os.unlink(snapshot_path)

    return wrapped


def demonstrate_cwd_tracking():
    """CWD 跟踪 — reference/06-bash-engine.md

    问题: 命令里可能有 cd，但子进程的 cd 不会影响父进程。
    如果用户执行 "cd src && ls"，下一条命令应该在 src/ 目录下。

    解决方案: 每条命令结尾追加 pwd -P，把当前目录写到临时文件。
    命令执行完后读取这个文件，更新 CWD。
    """
    print("\n=== CWD 跟踪机制 ===")

    cwd_file = tempfile.mktemp(prefix="claude-cwd-")

    user_command = "cd /tmp && echo 'now in /tmp'"
    # 真实的命令包装:
    tracked_command = f"{user_command} && pwd -P >| {cwd_file}"

    print(f"用户命令: {user_command}")
    print(f"追踪命令: {tracked_command}")

    # 执行
    result = subprocess.run(
        ["sh", "-c", tracked_command],
        capture_output=True, text=True
    )
    print(f"输出: {result.stdout.strip()}")

    # 读取新的 CWD
    try:
        with open(cwd_file, "r") as f:
            new_cwd = f.read().strip()
        print(f"新的 CWD: {new_cwd}")
        os.unlink(cwd_file)
    except FileNotFoundError:
        print("CWD 文件不存在（命令可能失败了）")


def demonstrate_auto_background():
    """自动后台化 — reference/06-bash-engine.md

    问题: 模型请求执行一个命令，但这个命令跑了 30 秒还没完。
    如果一直等着，用户体验很差。

    解决方案: 四条后台化路径:
    1. 显式: 模型设置 run_in_background=true
    2. 超时: 命令超过默认超时时间
    3. 助手模式: 在主代理中阻塞 > 15 秒
    4. 用户: 按 Ctrl+B 手动后台化

    特殊规则: sleep 命令禁止自动后台化！
    """
    print("\n=== 自动后台化策略 ===")

    AUTO_BG_THRESHOLD_SECONDS = 15  # 主代理阻塞阈值
    SLEEP_COMMANDS = {"sleep"}  # 禁止自动后台化的命令

    def should_auto_background(command: str, elapsed: float) -> bool:
        """决定是否自动后台化"""
        # 检查是否是 sleep 命令
        first_word = command.strip().split()[0] if command.strip() else ""
        if first_word in SLEEP_COMMANDS:
            return False  # sleep 不允许自动后台化
        return elapsed > AUTO_BG_THRESHOLD_SECONDS

    test_cases = [
        ("npm install", 20.0, True),
        ("sleep 30", 20.0, False),      # sleep 被排除！
        ("cargo build", 5.0, False),     # 还没超时
        ("make -j8", 16.0, True),
    ]

    for cmd, elapsed, expected in test_cases:
        result = should_auto_background(cmd, elapsed)
        status = "✓" if result == expected else "✗"
        action = "后台化" if result else "继续等待"
        print(f"  {status} '{cmd}' 已运行 {elapsed}s → {action}")


def demonstrate_output_size_watchdog():
    """输出大小看门狗 — reference/06-bash-engine.md

    问题: 一个卡住的循环不断往 stdout 写数据，
    曾经有个 bug 把 768GB 磁盘写满了。

    解决方案: 每 5 秒轮询输出文件大小，
    超过限制时 SIGKILL 终止整个进程树。

    注意是 SIGKILL 不是 SIGTERM——SIGTERM 可以被忽略。
    """
    print("\n=== 输出大小看门狗 ===")

    MAX_OUTPUT_SIZE = 10 * 1024 * 1024  # 10MB 限制
    POLL_INTERVAL = 5  # 秒

    class OutputWatchdog:
        def __init__(self, output_path: str, max_size: int):
            self.output_path = output_path
            self.max_size = max_size
            self._running = False

        def check_size(self) -> Tuple[int, bool]:
            """检查输出文件大小，返回 (大小, 是否超限)"""
            try:
                size = os.path.getsize(self.output_path)
                return size, size > self.max_size
            except FileNotFoundError:
                return 0, False

        def kill_process_tree(self, pid: int):
            """杀死进程树 — 不只是主进程，还有所有子进程"""
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    wd = OutputWatchdog("/tmp/example-output.txt", MAX_OUTPUT_SIZE)
    print(f"  最大输出: {MAX_OUTPUT_SIZE / 1024 / 1024:.0f}MB")
    print(f"  轮询间隔: {POLL_INTERVAL}s")
    print(f"  超限动作: SIGKILL 整个进程树")


# ============================================================
# 第四部分: 完整流程演示
# ============================================================

def demo_full_workflow():
    """完整的 Bash 执行流程"""
    print("\n" + "=" * 60)
    print("完整 Bash 执行流程演示")
    print("=" * 60)

    # 模拟模型发送的命令
    test_cases = [
        # 场景 1: 普通命令
        BashCommandInput(
            command="echo 'hello world'",
            timeout=5000,
            description="Print hello",
        ),
        # 场景 2: 超时命令
        BashCommandInput(
            command="sleep 10",
            timeout=500,  # 500ms 超时
            description="This will timeout",
        ),
        # 场景 3: 失败命令
        BashCommandInput(
            command="exit 1",
            timeout=5000,
            description="Expected failure",
        ),
    ]

    for i, inp in enumerate(test_cases, 1):
        print(f"\n--- 场景 {i}: {inp.description} ---")
        print(f"命令: {inp.command}")

        output = execute_bash(inp)

        print(f"stdout: {output.stdout.strip() or '(empty)'}")
        if output.stderr:
            print(f"stderr: {output.stderr.strip()}")
        print(f"interrupted: {output.interrupted}")
        if output.return_code_interpretation:
            print(f"返回码: {output.return_code_interpretation}")
        if output.sandbox_status:
            print(f"沙箱启用: {output.sandbox_status.enabled}")
            print(f"沙箱生效: {output.sandbox_status.active}")
            if output.sandbox_status.fallback_reason:
                print(f"降级原因: {output.sandbox_status.fallback_reason}")


def demo_sandbox_decision_tree():
    """展示沙箱决策过程"""
    print("\n" + "=" * 60)
    print("沙箱决策树演示")
    print("=" * 60)

    cwd = Path.cwd()

    scenarios = [
        ("默认配置", SandboxConfig(), None),
        ("用户关闭沙箱", SandboxConfig(enabled=False), None),
        ("模型请求禁用沙箱", SandboxConfig(), True),
        ("网络隔离", SandboxConfig(network_isolation=True), None),
        ("白名单模式", SandboxConfig(
            filesystem_mode=FilesystemIsolationMode.ALLOW_LIST,
            allowed_mounts=["logs", "/var/data"],
        ), None),
    ]

    for name, config, disable_override in scenarios:
        request = resolve_request(
            config,
            enabled_override=(not disable_override
                            if disable_override is not None else None),
        )
        status = resolve_sandbox_status(request, cwd)

        print(f"\n--- {name} ---")
        print(f"  请求: enabled={request.enabled}, "
              f"ns={request.namespace_restrictions}, "
              f"net={request.network_isolation}, "
              f"fs={request.filesystem_mode.value}")
        print(f"  结果: active={status.active}, "
              f"ns_active={status.namespace_active}, "
              f"net_active={status.network_active}, "
              f"fs_active={status.filesystem_active}")
        if status.fallback_reason:
            print(f"  降级: {status.fallback_reason}")
        if status.in_container:
            print(f"  容器: {status.container_markers}")


def demo_container_detection():
    """演示容器环境检测"""
    print("\n" + "=" * 60)
    print("容器环境检测")
    print("=" * 60)

    result = detect_container_environment()
    print(f"当前环境是容器: {result.in_container}")
    if result.markers:
        print(f"检测到的标记: {result.markers}")
    else:
        print("未检测到任何容器标记（在宿主机上运行）")

    # 模拟各种容器环境
    print("\n模拟不同容器环境的检测结果:")
    simulated = [
        ("Docker", {"标记": ["/.dockerenv", "env:DOCKER=1"]}),
        ("Kubernetes", {"标记": ["env:KUBERNETES_SERVICE_HOST=10.0.0.1"]}),
        ("Podman", {"标记": ["/run/.containerenv"]}),
        ("裸机", {"标记": []}),
    ]
    for env_name, info in simulated:
        is_container = len(info["标记"]) > 0
        print(f"  {env_name}: in_container={is_container}, markers={info['标记']}")


# ============================================================
# 主程序
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("教程 14: Bash 执行引擎深度剖析")
    print("源码对照: rust/crates/runtime/src/bash.rs + sandbox.rs")
    print("=" * 60)

    # 1. 容器检测
    demo_container_detection()

    # 2. 沙箱决策
    demo_sandbox_decision_tree()

    # 3. Shell 快照
    demonstrate_shell_snapshot()

    # 4. CWD 跟踪
    demonstrate_cwd_tracking()

    # 5. 自动后台化
    demonstrate_auto_background()

    # 6. 输出看门狗
    demonstrate_output_size_watchdog()

    # 7. 完整流程
    demo_full_workflow()

    print("\n" + "=" * 60)
    print("关键工程要点总结:")
    print("=" * 60)
    print("""
1. 纵深防御: 沙箱不是一个开关，而是四层独立防御的组合
   - 命名空间隔离 (unshare) → 进程级隔离
   - 网络隔离 (--net) → 完全断网
   - 文件系统隔离 (HOME/TMPDIR 重定向) → 写入限制
   - 权限系统 → 用户授权

2. 优雅降级: 每一层都可以独立失败
   - unshare 不可用？文件系统隔离仍然生效
   - Linux 不支持？macOS 用 seatbelt 替代
   - 什么都不支持？至少有权限系统兜底

3. 模型可感知: 沙箱状态附加到每个输出里
   - 模型知道命令跑在什么隔离级别下
   - 如果沙箱降级了，模型可以决定是否继续

4. 超时控制: 不是简单的 timeout + kill
   - 超时返回 interrupted=True，模型可以决定下一步
   - 后台化是超时的替代方案，不是错误处理

5. 后台执行: stdin/stdout/stderr 全部 DEVNULL
   - 进程完全脱离，不会阻塞管道
   - 返回 PID，可以后续查询状态

6. Shell 快照: 一次捕获，多次复用
   - 避免重复加载 .bashrc/.zshrc
   - 保持环境一致性

7. CWD 同步: pwd -P 追加到每条命令结尾
   - 子进程的 cd 效果可以传播到下一条命令
   - -P 解析符号链接，避免路径混乱
""")
