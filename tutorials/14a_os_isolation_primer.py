"""
教程 14a: 操作系统隔离基础——理解沙箱的前置知识
================================================================
这是教程 14（Bash 引擎深度）的前置课。
如果你不熟悉 Linux 命名空间、进程隔离、文件系统挂载等概念，
先看这个。

所有概念用日常类比 + 可运行的 Python demo 解释。
================================================================
"""

import os
import sys
import json
import time
import signal
import tempfile
import subprocess
from pathlib import Path


# ============================================================
# 第一课: 什么是"进程"？
# ============================================================
# 你在 Python 里写 os.system("ls") 时发生了什么？

def lesson_1_process():
    """
    类比: 进程就像一个"员工"

    你（操作系统）是一家公司的老板。
    每次你要完成一个任务（比如运行 ls），你就雇一个临时工（进程）。
    这个临时工有自己的：
      - 工号（PID，进程ID）
      - 工位（内存空间）
      - 工作目录（当前目录）
      - 环境信息（环境变量，比如 HOME 在哪）

    当任务完成后，临时工就离开了（进程退出）。
    """
    print("=" * 60)
    print("第一课: 什么是进程")
    print("=" * 60)

    # 当前进程（就是你正在运行的这个 Python 脚本）
    print(f"\n当前进程的信息:")
    print(f"  PID (工号): {os.getpid()}")
    print(f"  父进程 PID (老板的工号): {os.getppid()}")
    print(f"  工作目录 (工位): {os.getcwd()}")
    print(f"  HOME (家的地址): {os.environ.get('HOME', '未设置')}")
    print(f"  用户名: {os.environ.get('USER', '未设置')}")

    # 启动一个子进程（雇一个临时工）
    print(f"\n启动子进程执行 'echo hello':")
    result = subprocess.run(
        ["echo", "hello"],
        capture_output=True, text=True,
    )
    print(f"  子进程输出: {result.stdout.strip()}")
    print(f"  子进程退出码: {result.returncode}")
    print(f"  (子进程已结束，临时工走了)")


# ============================================================
# 第二课: 环境变量——进程的"记忆"
# ============================================================

def lesson_2_environment():
    """
    类比: 环境变量就像"公司通讯录"

    每个员工（进程）入职时，都会拿到一份公司通讯录的副本。
    通讯录里写着：
      HOME = /Users/你的名字     （家在哪）
      PATH = /usr/bin:/usr/local/bin  （工具箱在哪里）
      TMPDIR = /tmp              （临时仓库在哪）
      USER = 你的名字             （你是谁）

    关键点：
    1. 每个进程拿到的是"副本"，不是原件
    2. 子进程修改自己的通讯录，不影响父进程
    3. 但父进程可以给子进程一份"改过的"通讯录

    这就是沙箱的第一个技巧：
    给子进程一份假的通讯录，让它以为 HOME 在别的地方。
    """
    print("\n" + "=" * 60)
    print("第二课: 环境变量")
    print("=" * 60)

    # 正常情况: 子进程继承父进程的环境变量
    print("\n正常情况:")
    result = subprocess.run(
        ["sh", "-c", "echo HOME=$HOME"],
        capture_output=True, text=True,
    )
    print(f"  子进程看到的 HOME: {result.stdout.strip()}")

    # 沙箱技巧: 给子进程一份修改过的环境变量
    print("\n沙箱技巧——篡改子进程的 HOME:")
    fake_home = tempfile.mkdtemp(prefix="sandbox-home-")
    modified_env = dict(os.environ)  # 复制一份
    modified_env["HOME"] = fake_home  # 改掉 HOME

    result = subprocess.run(
        ["sh", "-c", "echo HOME=$HOME && echo '我以为我的家在这里'"],
        capture_output=True, text=True,
        env=modified_env,  # 传入修改过的环境
    )
    print(f"  子进程看到的 HOME: {result.stdout.strip()}")
    print(f"  但父进程的 HOME 没变: {os.environ['HOME']}")
    print(f"\n  这就是 Claude Code 文件系统隔离的核心原理！")
    print(f"  它把 HOME 指向 .sandbox-home/，")
    print(f"  这样命令写配置文件时，写的是沙箱目录，不是你真正的 HOME。")

    # 清理
    os.rmdir(fake_home)


# ============================================================
# 第三课: HOME 和 TMPDIR 为什么重要？
# ============================================================

def lesson_3_home_and_tmp():
    """
    类比: HOME 是你家，TMPDIR 是临时仓库

    HOME 目录:
      很多程序会往 HOME 里写配置文件。比如：
      - git 读写 ~/.gitconfig
      - npm 读写 ~/.npmrc
      - ssh 读写 ~/.ssh/
      - Python pip 读写 ~/.pip/

      如果一个恶意命令修改了 ~/.gitconfig，
      可能会影响你之后所有的 git 操作。

    TMPDIR 目录:
      程序创建临时文件的地方。比如：
      - 编译器的中间文件
      - 下载缓存
      - 进程间通信的 socket 文件

      如果恶意程序在 TMPDIR 里放了个假的 socket，
      其他程序可能会连上去泄露信息。

    所以 Claude Code 的策略是:
      把 HOME → .sandbox-home/
      把 TMPDIR → .sandbox-tmp/
      这样命令写的所有配置和临时文件都在沙箱里，不会污染真实环境。
    """
    print("\n" + "=" * 60)
    print("第三课: HOME 和 TMPDIR 的重要性")
    print("=" * 60)

    # 演示: 程序会在 HOME 里写配置文件
    sandbox_home = Path(tempfile.mkdtemp(prefix="sandbox-home-"))
    sandbox_tmp = Path(tempfile.mkdtemp(prefix="sandbox-tmp-"))

    print(f"\n真实 HOME: {os.environ['HOME']}")
    print(f"沙箱 HOME: {sandbox_home}")
    print(f"沙箱 TMPDIR: {sandbox_tmp}")

    # 在沙箱环境中运行一个写配置文件的命令
    env = dict(os.environ)
    env["HOME"] = str(sandbox_home)
    env["TMPDIR"] = str(sandbox_tmp)

    # git 会尝试读 $HOME/.gitconfig
    subprocess.run(
        ["sh", "-c", "echo '[user]\\n  name = hacker' > $HOME/.gitconfig"],
        env=env, capture_output=True,
    )

    # 检查效果
    sandbox_gitconfig = sandbox_home / ".gitconfig"
    real_gitconfig = Path(os.environ["HOME"]) / ".gitconfig"

    print(f"\n沙箱里的 .gitconfig 被写入了:")
    if sandbox_gitconfig.exists():
        print(f"  {sandbox_gitconfig}: {sandbox_gitconfig.read_text().strip()}")
    print(f"\n你真实的 .gitconfig 没有被修改:")
    if real_gitconfig.exists():
        # 只读第一行，不泄露用户信息
        print(f"  {real_gitconfig}: (存在，未被修改)")
    else:
        print(f"  {real_gitconfig}: (文件不存在)")

    print(f"\n→ 恶意命令以为它修改了你的 git 配置，")
    print(f"  但实际上它改的是沙箱里的副本。你的真实配置安全无恙。")

    # 清理
    import shutil
    shutil.rmtree(sandbox_home)
    shutil.rmtree(sandbox_tmp)


# ============================================================
# 第四课: Linux 命名空间——操作系统级的"平行宇宙"
# ============================================================

def lesson_4_namespaces():
    """
    类比: 命名空间就像"平行宇宙"

    想象你在玩一个沙盒游戏（比如 Minecraft）。
    你在自己的世界里建房子、挖矿、种地。
    但你的世界和其他人的世界是完全隔离的——
    你在你的世界里炸了 TNT，不会影响别人的世界。

    Linux 命名空间就是这个概念:
    操作系统可以创建多个"平行宇宙"，
    每个宇宙里的进程看到的世界是不同的。

    Linux 提供了 7 种命名空间，Claude Code 用了其中 6 种:

    ┌──────────────────────────────────────────────────┐
    │  命名空间        隔离了什么          unshare 参数  │
    ├──────────────────────────────────────────────────┤
    │  User (用户)     用户和权限          --user       │
    │  Mount (挂载)    文件系统的挂载点    --mount      │
    │  PID (进程)      进程 ID 表          --pid        │
    │  Network (网络)  网卡、IP、端口      --net        │
    │  IPC (通信)      共享内存、消息队列  --ipc        │
    │  UTS (主机名)    主机名              --uts        │
    └──────────────────────────────────────────────────┘
    """
    print("\n" + "=" * 60)
    print("第四课: Linux 命名空间（平行宇宙）")
    print("=" * 60)

    print("""
    想象一栋大楼（操作系统），里面有很多房间（进程）。

    默认情况下:
    ┌─────────────────────────────────────┐
    │  大楼（操作系统）                      │
    │                                      │
    │  [房间A] [房间B] [房间C]              │
    │     ↕       ↕       ↕               │
    │  共享走廊、共享电梯、共享 WiFi         │
    │  所有房间可以互相看到、互相访问        │
    └─────────────────────────────────────┘

    命名空间隔离后:
    ┌─────────────────────────────────────┐
    │  大楼（操作系统）                      │
    │                                      │
    │  ┌──────────┐  ┌──────────────────┐ │
    │  │ 平行宇宙A │  │  真实世界         │ │
    │  │           │  │                  │ │
    │  │ [沙箱进程] │  │ [普通进程B] [C]  │ │
    │  │           │  │                  │ │
    │  │ 自己的网络 │  │ 共享网络         │ │
    │  │ 自己的PID  │  │ 共享PID空间      │ │
    │  │ 自己的挂载 │  │ 共享文件系统      │ │
    │  └──────────┘  └──────────────────┘ │
    └─────────────────────────────────────┘
    """)

    # 逐个解释每种命名空间
    namespaces = [
        ("User (--user)", "用户命名空间",
         "进程在里面觉得自己是 root（管理员），\n"
         "    但其实在外面只是普通用户。\n"
         "    就像小孩在自己房间里当'国王'，但出了房间还是要听爸妈的。\n"
         "    --map-root-user: 把里面的 root 映射到外面的你。"),

        ("Mount (--mount)", "挂载命名空间",
         "进程看到的文件系统和外面不同。\n"
         "    你可以让它只看到工作目录，看不到 /home、/etc。\n"
         "    就像给员工一个只有工作文件的U盘，不让他碰公司其他电脑。"),

        ("PID (--pid)", "进程命名空间",
         "进程看不到外面的其他进程。\n"
         "    它觉得自己是世界上唯一的进程（PID=1）。\n"
         "    就像把一个人关在隔音房间里，他不知道外面有多少人。"),

        ("Network (--net)", "网络命名空间",
         "进程没有网络！它看到的是一个空的网卡列表。\n"
         "    不能访问 localhost，不能 curl 任何网站。\n"
         "    就像拔掉了网线。这是防止恶意命令外传数据的杀手锏。"),

        ("IPC (--ipc)", "IPC 命名空间",
         "进程不能通过共享内存或消息队列和外面的进程通信。\n"
         "    就像不让两个房间之间打电话。\n"
         "    防止沙箱内的进程偷偷和外面的进程交换数据。"),

        ("UTS (--uts)", "UTS 命名空间",
         "进程看到的主机名可以不同。\n"
         "    这是最简单的隔离，主要用于完整性。"),
    ]

    for flag, name, explanation in namespaces:
        print(f"\n  {flag} — {name}")
        print(f"    {explanation}")


# ============================================================
# 第五课: unshare 命令——创建平行宇宙的工具
# ============================================================

def lesson_5_unshare():
    """
    类比: unshare 就像"施放结界"

    在动漫里，角色可以施放结界把一块区域与外界隔离。
    unshare 就是 Linux 的"施放结界"命令。

    用法:
      unshare --user --mount --pid --fork sh -c "你的命令"

    这条命令做了什么:
    1. 创建新的命名空间（施放结界）
    2. 把子进程放进去（把目标拉进结界）
    3. 在结界内执行命令
    4. 命令结束后结界消失（命名空间自动销毁）
    """
    print("\n" + "=" * 60)
    print("第五课: unshare 命令")
    print("=" * 60)

    print("""
    Claude Code 构建的 unshare 命令（来自 sandbox.rs:222-261）:

    unshare \\
      --user \\          # 1. 创建用户结界（进程以为自己是 root）
      --map-root-user \\ # 2. root 映射到当前用户
      --mount \\         # 3. 创建文件系统结界
      --ipc \\           # 4. 创建通信结界
      --pid \\           # 5. 创建进程结界
      --uts \\           # 6. 创建主机名结界
      --fork \\          # 7. fork 后在新结界中执行
      --net \\           # 8. [可选] 创建网络结界（完全断网）
      sh -lc "用户的命令"  # 9. 在结界内执行命令
    """)

    # 检测当前系统是否支持
    is_linux = sys.platform.startswith("linux")
    print(f"  当前系统: {sys.platform}")

    if is_linux:
        # 在 Linux 上可以实际演示
        print("  ✓ 当前是 Linux，可以使用 unshare")
        try:
            result = subprocess.run(
                ["unshare", "--user", "--map-root-user", "--fork",
                 "sh", "-c", "echo 我在结界内; id; echo PID=$$"],
                capture_output=True, text=True, timeout=5,
            )
            print(f"  结界内的输出:\n{result.stdout}")
        except FileNotFoundError:
            print("  ✗ unshare 命令不存在")
    else:
        print("  ✗ 当前不是 Linux，无法使用 unshare")
        print("  (macOS 用 sandbox-exec/seatbelt 替代，原理类似)")

    # 用 Python 模拟"平行宇宙"的效果
    print("\n  用 Python 模拟命名空间隔离的效果:")

    print("\n  --- PID 命名空间的效果 ---")
    print("  正常情况: 进程能看到系统中的其他进程")
    result = subprocess.run(
        ["sh", "-c", "ps aux 2>/dev/null | head -5 || echo '(ps 不可用)'"],
        capture_output=True, text=True,
    )
    lines = result.stdout.strip().split('\n')
    for line in lines[:4]:
        print(f"    {line[:70]}")
    print("    ... (还有很多)")
    print("  PID 命名空间内: 进程只能看到自己，PID 从 1 开始")

    print("\n  --- 网络命名空间的效果 ---")
    print("  正常情况: 进程可以访问网络")
    print("  网络命名空间内: 只有一个空的 loopback 接口")
    print("    $ ip addr  →  只有 lo (127.0.0.1)")
    print("    $ curl google.com  →  Network is unreachable")


# ============================================================
# 第六课: 容器 (Docker) 和命名空间的关系
# ============================================================

def lesson_6_containers():
    """
    类比: Docker 容器就是"全套命名空间 + 文件系统快照"

    你可能听过 Docker。Docker 容器本质上就是:
    1. 所有命名空间都启用（User + Mount + PID + Network + IPC + UTS）
    2. 一个独立的文件系统（镜像）
    3. 资源限制（cgroups，限制 CPU 和内存）

    Claude Code 的沙箱是 Docker 的"轻量版":
    - 不创建完整的文件系统镜像
    - 不做资源限制
    - 只隔离必要的命名空间
    - 用环境变量重定向代替文件系统隔离

    为什么不直接用 Docker？
    1. Docker 需要 daemon（后台服务），启动慢
    2. Docker 需要 root 权限或特殊的用户组
    3. unshare 是轻量级的，几乎零开销
    4. 只需要"够用"的隔离，不需要完整容器
    """
    print("\n" + "=" * 60)
    print("第六课: 容器和命名空间的关系")
    print("=" * 60)

    print("""
    ┌─────────────────────────────────────────────────┐
    │                Docker 容器                       │
    │  ┌───────────────────────────────────────────┐  │
    │  │  cgroups (资源限制: CPU, 内存, IO)         │  │
    │  │  ┌─────────────────────────────────────┐  │  │
    │  │  │  所有 6 种命名空间全部启用            │  │  │
    │  │  │  ┌─────────────────────────────────┐│  │  │
    │  │  │  │  独立的文件系统 (镜像)           ││  │  │
    │  │  │  │  ┌─────────────────────────────┐││  │  │
    │  │  │  │  │  你的应用程序                │││  │  │
    │  │  │  │  └─────────────────────────────┘││  │  │
    │  │  │  └─────────────────────────────────┘│  │  │
    │  │  └─────────────────────────────────────┘  │  │
    │  └───────────────────────────────────────────┘  │
    └─────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────────────┐
    │           Claude Code 沙箱 (unshare)             │
    │  ┌───────────────────────────────────────────┐  │
    │  │  选择性命名空间 (User+Mount+PID+IPC+UTS)  │  │
    │  │  可选: +Network (断网)                     │  │
    │  │  ┌─────────────────────────────────────┐  │  │
    │  │  │  共享文件系统，但 HOME/TMPDIR 重定向 │  │  │
    │  │  │  ┌─────────────────────────────────┐│  │  │
    │  │  │  │  用户的命令                      ││  │  │
    │  │  │  └─────────────────────────────────┘│  │  │
    │  │  └─────────────────────────────────────┘  │  │
    │  └───────────────────────────────────────────┘  │
    └─────────────────────────────────────────────────┘

    Docker: 重量级，完全隔离，需要 daemon
    unshare: 轻量级，选择性隔离，即用即走
    """)

    # Claude Code 如何检测自己是否在 Docker 里
    print("  Claude Code 检测容器的 5 种信号:")
    checks = [
        ("/.dockerenv 文件", "Docker 创建容器时自动放的空文件",
         os.path.exists("/.dockerenv")),
        ("/run/.containerenv 文件", "Podman 的标记",
         os.path.exists("/run/.containerenv")),
        ("CONTAINER 环境变量", "有些容器运行时会设置",
         "CONTAINER" in os.environ),
        ("KUBERNETES_SERVICE_HOST 环境变量", "K8s Pod 内自动设置",
         "KUBERNETES_SERVICE_HOST" in os.environ),
        ("/proc/1/cgroup 内容", "容器内 cgroup 路径包含 docker/kubepods",
         _check_cgroup()),
    ]
    for name, desc, detected in checks:
        status = "✓ 检测到" if detected else "✗ 未检测到"
        print(f"    {status} | {name}")
        print(f"           {desc}")


def _check_cgroup() -> bool:
    try:
        content = open("/proc/1/cgroup").read()
        return any(k in content for k in ["docker", "kubepods", "containerd"])
    except (FileNotFoundError, PermissionError):
        return False


# ============================================================
# 第七课: 实际动手——体验文件系统隔离
# ============================================================

def lesson_7_hands_on_sandbox():
    """
    让我们实际构建一个简化版沙箱，体验隔离效果。
    """
    print("\n" + "=" * 60)
    print("第七课: 动手体验文件系统隔离")
    print("=" * 60)

    # 创建沙箱目录
    workspace = Path(tempfile.mkdtemp(prefix="claude-sandbox-demo-"))
    sandbox_home = workspace / ".sandbox-home"
    sandbox_tmp = workspace / ".sandbox-tmp"
    sandbox_home.mkdir()
    sandbox_tmp.mkdir()

    print(f"\n  工作目录: {workspace}")
    print(f"  沙箱 HOME: {sandbox_home}")
    print(f"  沙箱 TMPDIR: {sandbox_tmp}")

    # 构建隔离的环境变量
    sandbox_env = dict(os.environ)
    sandbox_env["HOME"] = str(sandbox_home)
    sandbox_env["TMPDIR"] = str(sandbox_tmp)

    # 实验 1: 在沙箱里写配置文件
    print(f"\n  --- 实验 1: 写配置文件 ---")
    subprocess.run(
        ["sh", "-c", """
            echo 'secret_token = STOLEN_API_KEY' > $HOME/.evil_config
            echo '成功写入 $HOME/.evil_config'
        """],
        env=sandbox_env, capture_output=True, text=True,
    )

    evil_in_sandbox = sandbox_home / ".evil_config"
    evil_in_real = Path(os.environ["HOME"]) / ".evil_config"

    print(f"  沙箱里: {evil_in_sandbox} → 存在={evil_in_sandbox.exists()}")
    if evil_in_sandbox.exists():
        print(f"    内容: {evil_in_sandbox.read_text().strip()}")
    print(f"  真实 HOME: {evil_in_real} → 存在={evil_in_real.exists()}")
    print(f"  ✓ 恶意配置被困在沙箱里了！")

    # 实验 2: 在沙箱里创建临时文件
    print(f"\n  --- 实验 2: 创建临时文件 ---")
    result = subprocess.run(
        ["sh", "-c", """
            tmpfile=$(mktemp)
            echo "临时文件创建在: $tmpfile"
            echo "TMPDIR 是: $TMPDIR"
        """],
        env=sandbox_env, capture_output=True, text=True,
    )
    for line in result.stdout.strip().split('\n'):
        print(f"    {line}")

    sandbox_tmp_files = list(sandbox_tmp.iterdir())
    print(f"  沙箱 TMPDIR 里的文件: {len(sandbox_tmp_files)} 个")

    # 实验 3: 尝试读取真实 HOME 的内容
    print(f"\n  --- 实验 3: 命令能否访问真实文件？ ---")
    result = subprocess.run(
        ["sh", "-c", f"ls {os.environ['HOME']}/.ssh 2>&1 || echo '(访问失败)'"],
        env=sandbox_env, capture_output=True, text=True,
    )
    print(f"  尝试 ls 真实 HOME/.ssh:")
    print(f"    {result.stdout.strip()[:80]}")
    print(f"\n  ⚠ 注意: 仅靠 HOME/TMPDIR 重定向，命令仍然可以用绝对路径访问其他文件！")
    print(f"    这就是为什么需要命名空间（Mount namespace）来做更强的隔离。")
    print(f"    环境变量重定向是'君子协定'，命名空间是'物理墙壁'。")

    # 清理
    import shutil
    shutil.rmtree(workspace)


# ============================================================
# 第八课: macOS 的替代方案——sandbox-exec (seatbelt)
# ============================================================

def lesson_8_macos_seatbelt():
    """
    macOS 没有 Linux 命名空间，但有自己的沙箱机制。
    """
    print("\n" + "=" * 60)
    print("第八课: macOS 沙箱 (sandbox-exec / seatbelt)")
    print("=" * 60)

    print("""
    macOS 使用的是 Apple 的 Sandbox 框架（代号 seatbelt，安全带）。

    原理不同于 Linux 命名空间:
    - Linux: 创建"平行宇宙"，进程看到的世界是假的
    - macOS: 给进程加"规则"，违反规则的操作直接拒绝

    就像:
    - Linux 命名空间 = 把你关进一个虚拟房间，你不知道外面的世界
    - macOS seatbelt = 给你戴上手铐，你能看到外面但不能碰

    配置方式——用一个文本配置文件描述规则:
    """)

    seatbelt_example = """
    ;; macOS 沙箱配置文件示例 (Scheme 语言语法)
    (version 1)
    (deny default)                          ; 默认拒绝所有操作

    (allow process-exec)                    ; 允许执行进程
    (allow file-read* (subpath "/usr"))     ; 允许读 /usr 下的文件
    (allow file-read* (subpath "/bin"))     ; 允许读 /bin 下的文件

    (allow file-read-data                   ; 允许读工作目录
      (subpath "/Users/you/project"))
    (allow file-write-data                  ; 允许写工作目录
      (subpath "/Users/you/project"))

    (deny file-write-data                   ; 禁止写设置文件！
      (subpath "/Users/you/.claude"))
    (deny network-outbound)                 ; 禁止网络访问
    """

    for line in seatbelt_example.strip().split('\n'):
        print(f"    {line}")

    if sys.platform == "darwin":
        # 实际演示 sandbox-exec
        print("\n  在 macOS 上实际演示 sandbox-exec:")
        try:
            # 创建一个只允许读的沙箱
            profile = '(version 1)(allow default)(deny file-write*)'
            result = subprocess.run(
                ["sandbox-exec", "-p", profile, "sh", "-c",
                 "echo '读取正常'; touch /tmp/sandbox-test-file 2>&1 || echo '写入被拒绝！'"],
                capture_output=True, text=True, timeout=5,
            )
            print(f"    {result.stdout.strip()}")
            if result.stderr:
                print(f"    stderr: {result.stderr.strip()[:100]}")
        except FileNotFoundError:
            print("    sandbox-exec 不可用")
    else:
        print("\n  (当前不是 macOS，跳过实际演示)")


# ============================================================
# 第九课: 把所有知识串起来——Claude Code 的四层防御
# ============================================================

def lesson_9_putting_it_all_together():
    """
    现在你理解了所有前置知识，让我们串起来看 Claude Code 的完整沙箱。
    """
    print("\n" + "=" * 60)
    print("第九课: Claude Code 的四层沙箱防御")
    print("=" * 60)

    print("""
    当模型请求执行 Bash 命令时，经过四层防御:

    ┌─────────────────────────────────────────────────┐
    │  第 1 层: 权限系统 (permissions.rs)              │
    │  "你有资格执行这个命令吗？"                       │
    │  → ReadOnly 模式？直接拒绝                       │
    │  → 需要用户批准？弹出提示                        │
    └────────────────────┬────────────────────────────┘
                         ↓ 通过
    ┌─────────────────────────────────────────────────┐
    │  第 2 层: Hook 拦截 (hooks.rs)                   │
    │  "自定义规则允许吗？"                             │
    │  → Pre-hook exit 2？阻止执行                     │
    │  → Pre-hook exit 0？放行                         │
    └────────────────────┬────────────────────────────┘
                         ↓ 通过
    ┌─────────────────────────────────────────────────┐
    │  第 3 层: 环境变量隔离（任何平台都支持）          │
    │  HOME → .sandbox-home/                           │
    │  TMPDIR → .sandbox-tmp/                          │
    │  → 命令写的配置和临时文件被重定向到沙箱目录       │
    │  → 这是"君子协定"——命令可以用绝对路径绕过         │
    └────────────────────┬────────────────────────────┘
                         ↓
    ┌─────────────────────────────────────────────────┐
    │  第 4 层: 操作系统级沙箱（平台相关）              │
    │                                                  │
    │  Linux: unshare 命名空间                         │
    │    --user    你以为你是 root，其实不是             │
    │    --mount   你看到的文件系统和外面不同            │
    │    --pid     你看不到外面的进程                    │
    │    --ipc     你不能和外面的进程通信                │
    │    --uts     你的主机名是假的                      │
    │    --net     [可选] 你没有网络                     │
    │  → 这是"物理墙壁"——内核强制执行，无法绕过         │
    │                                                  │
    │  macOS: sandbox-exec (seatbelt)                  │
    │    deny file-write* 指定目录外                    │
    │    deny network-outbound [可选]                   │
    │  → 内核级规则，同样无法绕过                       │
    └─────────────────────────────────────────────────┘

    每一层都是独立的！
    - 第 3 层失败了？第 4 层还在
    - 第 4 层不支持（Windows）？至少还有第 1-3 层
    - 这就是"纵深防御"——永远不依赖单一防线
    """)


# ============================================================
# 主程序
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("教程 14a: 操作系统隔离基础")
    print("给教程 14 (Bash 引擎深度) 做知识铺垫")
    print("=" * 60)

    lesson_1_process()
    lesson_2_environment()
    lesson_3_home_and_tmp()
    lesson_4_namespaces()
    lesson_5_unshare()
    lesson_6_containers()
    lesson_7_hands_on_sandbox()
    lesson_8_macos_seatbelt()
    lesson_9_putting_it_all_together()

    print("\n" + "=" * 60)
    print("总结: 回到教程 14 之前你需要记住的")
    print("=" * 60)
    print("""
    1. 进程 = 运行中的程序，有自己的 PID、内存、环境变量
    2. 环境变量 = 进程的"通讯录"，子进程拿到的是副本
    3. HOME = 程序写配置文件的地方，篡改它就能重定向配置写入
    4. TMPDIR = 程序写临时文件的地方，篡改它同理
    5. 命名空间 = Linux 的"平行宇宙"，让进程看到的世界是假的
       - User: 假的用户权限
       - Mount: 假的文件系统
       - PID: 假的进程表
       - Network: 没有网络（最强隔离）
       - IPC: 不能和外面通信
       - UTS: 假的主机名
    6. unshare = 创建平行宇宙的命令
    7. Docker 容器 = 全部命名空间 + 独立文件系统 + 资源限制
    8. Claude Code 沙箱 = 选择性命名空间 + 环境变量重定向（轻量版容器）
    9. macOS 用 seatbelt（规则匹配）替代 Linux 命名空间（平行宇宙）
    10. 四层防御: 权限 → Hook → 环境变量 → 操作系统

    现在你可以回去看教程 14 了，那些参数应该不再陌生。
    """)
