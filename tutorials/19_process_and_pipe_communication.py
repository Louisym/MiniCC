"""
教程 19: 进程与管道通信
======================

来源: hooks.rs (stdin 传 JSON 给 hook 子进程)
      bash.rs (subprocess 执行命令, 捕获 stdout/stderr)
      conversation.rs (merge_hook_feedback: hook 输出合并到工具结果)
目标: 理解 Claude Code 如何与外部程序通信
前置: 会用 Python subprocess 或至少用过 os.system()

为什么需要进程通信?
────────────────────
Claude Code 本身是一个程序, 但它要:
1. 执行 Bash 命令 (启动子进程, 拿输出)
2. 运行 Hook 脚本 (传 JSON 数据, 读退出码)
3. 后台运行长任务 (不阻塞主循环)

这些全靠 "进程间通信" (IPC: Inter-Process Communication)。
本教程教你 5 个核心概念:
  1. 进程基础 (父子关系, PID)
  2. 标准流 (stdin/stdout/stderr — 进程的三个"嘴巴")
  3. 管道 (pipe — 连接进程的"水管")
  4. 环境变量 (传配置给子进程的"便利贴")
  5. 退出码 (进程的"遗言": 0=成功, 非0=失败)
  6. 信号 (操控进程的"遥控器": kill, timeout)
  7. 完整 Hook 系统重现
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time


# ============================================================
# 第一课: 进程基础 — 父进程与子进程
# ============================================================

def lesson_1_process_basics():
    """
    当你运行 `python script.py`, 系统创建一个"进程"。
    当这个 Python 脚本用 subprocess 运行另一个命令时, 就创建了"子进程"。

    关系:
      终端 (shell) ─→ python (父进程) ─→ ls (子进程)
                                        ─→ grep (子进程)
                                        ─→ 你的 hook 脚本 (子进程)

    在 Claude Code 中:
      Claude Code (Rust) ─→ bash -c "ls -la" (执行 Bash 工具)
                          ─→ python hook.py  (执行 Hook)
    """
    print("=" * 60)
    print("第一课: 进程基础 — 父进程与子进程")
    print("=" * 60)

    # 当前进程的 PID (Process ID, 进程编号)
    my_pid = os.getpid()
    my_ppid = os.getppid()  # 父进程的 PID
    print(f"\n  当前进程 PID: {my_pid}")
    print(f"  父进程 PPID:  {my_ppid}  (这是运行我的终端/shell)")

    # 启动一个子进程
    # subprocess.run() 是 Python 启动子进程的标准方法
    # 等同于你在终端输入 "echo hello"
    result = subprocess.run(
        ["echo", "我是子进程!"],
        capture_output=True,  # 捕获输出 (不打印到屏幕)
        text=True,            # 输出为字符串 (不是 bytes)
    )
    print(f"  子进程输出: {result.stdout.strip()}")
    print(f"  子进程退出码: {result.returncode}")

    print()
    print("    进程树 (本教程):")
    print(f"      shell (PID={my_ppid})")
    print(f"        └─ python (PID={my_pid})")
    print(f"             └─ echo (已结束)")
    print()


# ============================================================
# 第二课: stdin / stdout / stderr — 进程的三个"嘴巴"
# ============================================================

def lesson_2_standard_streams():
    """
    每个进程出生时自带三条"管子":

      stdin  (标准输入, fd=0)  ← 进程从这里读数据 (键盘/管道)
      stdout (标准输出, fd=1)  → 进程把正常结果写到这里
      stderr (标准错误, fd=2)  → 进程把错误信息写到这里

    日常类比:
      stdin  = 你的耳朵 (听输入)
      stdout = 你的嘴巴 (说正常话)
      stderr = 你的紧急电话 (报错误)

    在 Claude Code hooks.rs:162-165 中:
      child.stdin(Stdio::piped());   // 把 stdin 连到管道 (我们写数据进去)
      child.stdout(Stdio::piped());  // 把 stdout 连到管道 (我们读数据出来)
      child.stderr(Stdio::piped());  // 把 stderr 也连到管道
    """
    print("=" * 60)
    print("第二课: stdin / stdout / stderr — 进程的三个嘴巴")
    print("=" * 60)

    # ---- 例 1: 捕获 stdout 和 stderr ----
    print()
    print("  例 1: 分别捕获 stdout 和 stderr")
    print("  ────────────────────────────────")

    # 这个命令: 先往 stdout 写正常输出, 再往 stderr 写错误
    result = subprocess.run(
        ["python3", "-c", textwrap.dedent("""\
            import sys
            print("正常输出: 文件已读取")        # → stdout
            print("警告: 文件较大", file=sys.stderr)  # → stderr
        """)],
        capture_output=True,
        text=True,
    )
    print(f"    stdout: {result.stdout.strip()}")
    print(f"    stderr: {result.stderr.strip()}")
    print(f"    退出码: {result.returncode}")

    # ---- 例 2: 通过 stdin 传数据给子进程 ----
    print()
    print("  例 2: 通过 stdin 传数据 (Hook 的核心机制!)")
    print("  ─────────────────────────────────────────")

    # Claude Code hooks.rs:174 做的事:
    # child.output_with_stdin(payload.as_bytes())
    # 即: 通过 stdin 把 JSON payload 传给 hook 脚本

    hook_script = textwrap.dedent("""\
        import sys, json
        # 从 stdin 读取 JSON
        data = json.load(sys.stdin)
        tool = data['tool_name']
        event = data['hook_event_name']
        print(f"Hook 收到: {event} -> {tool}")
    """)

    payload = json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "bash",
        "tool_input": {"command": "rm -rf /"},
    })

    result = subprocess.run(
        ["python3", "-c", hook_script],
        input=payload,      # ← 通过 stdin 传入 JSON
        capture_output=True,
        text=True,
    )
    print(f"    传入 stdin: {payload[:50]}...")
    print(f"    Hook 输出: {result.stdout.strip()}")

    print()
    print("    这就是 hooks.rs:162-174 的工作方式:")
    print("    1. Claude Code 构造 JSON payload")
    print("    2. 启动 hook 子进程")
    print("    3. 通过 stdin 管道传 JSON 给 hook")
    print("    4. 读取 hook 的 stdout (反馈信息)")
    print("    5. 读取 hook 的退出码 (允许/拒绝/警告)")
    print()


# ============================================================
# 第三课: 管道 (Pipe) — 连接进程的"水管"
# ============================================================

def lesson_3_pipe():
    """
    管道 = 一根"水管", 一端写入, 另一端读出。

    在 shell 里:  ls | grep .py | wc -l
    实际上是:
      ls 的 stdout ──pipe──→ grep 的 stdin
      grep 的 stdout ──pipe──→ wc 的 stdin

    在 Python subprocess 中:
      subprocess.PIPE = "请给我一根管道, 而不是直接打印到屏幕"

    在 Claude Code 中:
      hooks.rs:163  child.stdin(Stdio::piped())
      → "把子进程的 stdin 连到管道, 我要往里写 JSON"
      hooks.rs:164  child.stdout(Stdio::piped())
      → "把子进程的 stdout 连到管道, 我要读它的输出"
    """
    print("=" * 60)
    print("第三课: 管道 (Pipe) — 连接进程的水管")
    print("=" * 60)

    # ---- 例 1: 手动实现 shell 管道 ls | grep ----
    print()
    print("  例 1: 手动实现 'echo ... | grep py'")
    print("  ────────────────────────────────────")

    # 模拟 echo "多行文本" | grep py

    # 进程 1: 产生数据
    p1 = subprocess.Popen(
        ["printf", "main.py\nREADME.md\ntest.py\nsetup.cfg\n"],
        stdout=subprocess.PIPE,  # stdout 连到管道
    )

    # 进程 2: 过滤数据 (stdin 来自 p1 的 stdout)
    p2 = subprocess.Popen(
        ["grep", "py"],
        stdin=p1.stdout,         # ← p1 的 stdout 管道接到 p2 的 stdin
        stdout=subprocess.PIPE,  # p2 的 stdout 也连管道 (我们要读)
    )

    # 关闭 p1 的 stdout (让 p2 读完后收到 EOF)
    p1.stdout.close()

    output = p2.communicate()[0].decode()
    print(f"    结果: {output.strip()}")
    print()
    print("    数据流向:")
    print("      printf → [stdout管道] → grep的stdin → [stdout管道] → Python读取")
    print()

    # ---- 例 2: Popen 的流式读取 ----
    print("  例 2: 流式读取 (逐行读, 不等全部完成)")
    print("  ──────────────────────────────────────")
    print("    这对长运行命令很重要——Claude Code 的 Bash 工具就是这样工作的。")
    print()

    proc = subprocess.Popen(
        ["python3", "-c", textwrap.dedent("""\
            import time
            for i in range(3):
                print(f"第 {i+1} 行", flush=True)
                time.sleep(0.1)
        """)],
        stdout=subprocess.PIPE,
        text=True,
    )

    # 逐行读取, 不等全部完成
    lines = []
    for line in proc.stdout:
        line = line.strip()
        lines.append(line)
        print(f"    实时收到: {line}")

    proc.wait()
    print(f"    共 {len(lines)} 行, 退出码: {proc.returncode}")
    print()

    # ---- 例 3: subprocess.PIPE vs DEVNULL vs None ----
    print("  三种 stdout 模式:")
    print("  ─────────────────")
    print("    subprocess.PIPE    → 连到管道, Python 可以读")
    print("    subprocess.DEVNULL → 丢弃输出 (等于 > /dev/null)")
    print("    None (默认)         → 继承父进程的 stdout (打印到屏幕)")
    print()
    print("    Claude Code bash.rs 中:")
    print("    - 正常命令:   PIPE (捕获输出返回给模型)")
    print("    - 后台命令:   Stdio::null() (不阻塞, 输出丢弃)")
    print()


# ============================================================
# 第四课: 环境变量 — 传配置给子进程的"便利贴"
# ============================================================

def lesson_4_environment_variables():
    """
    对应源码 hooks.rs:166-172:
      child.env("HOOK_EVENT", event.as_str());
      child.env("HOOK_TOOL_NAME", tool_name);
      child.env("HOOK_TOOL_INPUT", tool_input);
      child.env("HOOK_TOOL_IS_ERROR", if is_error { "1" } else { "0" });
      if let Some(tool_output) = tool_output {
          child.env("HOOK_TOOL_OUTPUT", tool_output);
      }

    子进程会继承父进程的环境变量, 但父进程也可以额外设置新的。
    这是一种"不需要 stdin/参数就能传信息"的方式。

    日常类比:
      环境变量 = 贴在办公桌上的便利贴
      新员工(子进程)入职时, 能看到桌上所有便利贴(继承环境变量),
      经理(父进程)还可以额外贴几张(设置新变量)。
    """
    print("=" * 60)
    print("第四课: 环境变量 — 传配置给子进程的便利贴")
    print("=" * 60)

    # ---- 例 1: 子进程继承环境变量 ----
    print()
    print("  例 1: 子进程继承父进程的环境变量")
    print("  ────────────────────────────────")

    # 设置一个环境变量
    os.environ["MY_SECRET"] = "password123"

    result = subprocess.run(
        ["python3", "-c", "import os; print(os.environ.get('MY_SECRET', '没找到'))"],
        capture_output=True,
        text=True,
    )
    print(f"    父进程设置: MY_SECRET=password123")
    print(f"    子进程读取: {result.stdout.strip()}")

    # 清理
    del os.environ["MY_SECRET"]

    # ---- 例 2: 额外设置环境变量 (不影响父进程) ----
    print()
    print("  例 2: 给子进程设置专属环境变量 (hooks.rs 的做法)")
    print("  ───────────────────────────────────────────────")

    # 对应 hooks.rs:166-172
    hook_env = os.environ.copy()
    hook_env["HOOK_EVENT"] = "PreToolUse"
    hook_env["HOOK_TOOL_NAME"] = "bash"
    hook_env["HOOK_TOOL_INPUT"] = '{"command": "ls"}'
    hook_env["HOOK_TOOL_IS_ERROR"] = "0"

    result = subprocess.run(
        ["python3", "-c", textwrap.dedent("""\
            import os
            print(f"事件: {os.environ['HOOK_EVENT']}")
            print(f"工具: {os.environ['HOOK_TOOL_NAME']}")
            print(f"输入: {os.environ['HOOK_TOOL_INPUT']}")
            print(f"错误: {os.environ['HOOK_TOOL_IS_ERROR']}")
        """)],
        capture_output=True,
        text=True,
        env=hook_env,  # ← 使用自定义环境变量
    )
    print(f"    子进程读取:")
    for line in result.stdout.strip().split("\n"):
        print(f"      {line}")

    print()
    print("    为什么 Claude Code 用两种方式传数据?")
    print("    ────────────────────────────────────")
    print("    stdin (JSON):  传复杂结构化数据 (完整 payload)")
    print("    env vars:      传简单标量值 (事件名、工具名)")
    print("    Hook 脚本可以选择哪种方便用哪种!")
    print()


# ============================================================
# 第五课: 退出码 — 进程的"遗言"
# ============================================================

def lesson_5_exit_codes():
    """
    对应源码 hooks.rs:179-196:
      match output.status.code() {
          Some(0) => HookCommandOutcome::Allow { message },  // 成功=允许
          Some(2) => HookCommandOutcome::Deny { message },   // 2=拒绝
          Some(code) => HookCommandOutcome::Warn { ... },    // 其他=警告
          None => HookCommandOutcome::Warn { ... },          // 被信号杀死
      }

    退出码 (exit code) 是进程结束时返回给父进程的一个数字:
    - 0 = 成功
    - 非 0 = 失败 (具体含义由程序自定)

    Claude Code 的 Hook 协议:
    - 0 = Allow (允许工具执行)
    - 2 = Deny (拒绝工具执行)
    - 其他 = Warn (警告, 但不阻止)
    """
    print("=" * 60)
    print("第五课: 退出码 — 进程的遗言")
    print("=" * 60)

    # ---- 演示不同退出码 ----
    print()
    print("  Claude Code Hook 退出码协议:")
    print("  ─────────────────────────────")

    test_cases = [
        (0, "Allow", "工具执行被允许"),
        (2, "Deny", "工具执行被拒绝"),
        (1, "Warn", "警告, 但继续执行"),
        (42, "Warn", "未知退出码, 也视为警告"),
    ]

    for exit_code, outcome, meaning in test_cases:
        result = subprocess.run(
            ["python3", "-c", f"import sys; print('hook output'); sys.exit({exit_code})"],
            capture_output=True,
            text=True,
        )
        print(f"    exit({exit_code}) → returncode={result.returncode:>2}"
              f" → {outcome:<5} | {meaning}")

    # ---- 在 Python 中实现 Hook 退出码解析 ----
    print()
    print("  用 Python 重现 hooks.rs 的退出码处理:")
    print("  ─────────────────────────────────────")

    def interpret_hook_exit_code(
        returncode: int | None,
        stdout: str,
        stderr: str,
        command: str,
        tool_name: str,
    ) -> dict:
        """
        对应 hooks.rs:179-196
        """
        message = stdout.strip() if stdout.strip() else None

        if returncode == 0:
            return {"outcome": "Allow", "message": message}
        elif returncode == 2:
            return {
                "outcome": "Deny",
                "message": message or f"Hook denied tool `{tool_name}`",
            }
        elif returncode is not None:
            # 非 0 非 2 → 警告
            parts = [f"Hook `{command}` exited with code {returncode}"]
            if message:
                parts.append(f"stdout: {message}")
            if stderr.strip():
                parts.append(f"stderr: {stderr.strip()}")
            return {"outcome": "Warn", "message": " | ".join(parts)}
        else:
            # None = 被信号杀死 (没有退出码)
            return {
                "outcome": "Warn",
                "message": f"Hook `{command}` terminated by signal",
            }

    # 测试
    scenarios = [
        {"returncode": 0, "stdout": "审计通过", "stderr": "", "cmd": "audit.sh"},
        {"returncode": 2, "stdout": "危险命令!", "stderr": "", "cmd": "guard.sh"},
        {"returncode": 1, "stdout": "", "stderr": "connection error", "cmd": "check.sh"},
        {"returncode": None, "stdout": "", "stderr": "", "cmd": "slow.sh"},
    ]

    for s in scenarios:
        result = interpret_hook_exit_code(
            s["returncode"], s["stdout"], s["stderr"], s["cmd"], "bash"
        )
        print(f"    {s['cmd']:<10} exit={str(s['returncode']):>4}"
              f" → {result['outcome']:<5} | {result.get('message', '')[:50]}")

    print()


# ============================================================
# 第六课: 信号 — 操控进程的"遥控器"
# ============================================================

def lesson_6_signals():
    """
    信号 (signal) 是操作系统发给进程的"通知":
    - SIGTERM (15): "请你退出" (礼貌请求, 可以忽略)
    - SIGKILL (9):  "立刻死" (无法忽略, 强制终止)
    - SIGINT (2):   "Ctrl+C" (用户中断)
    - SIGALRM:      "闹钟响了" (超时)

    在 Claude Code bash.rs 中:
    - 超时: tokio::time::timeout → 超时后杀死子进程
    - 后台: 子进程独立运行, 不受父进程退出影响

    日常类比:
      SIGTERM = 敲门说 "请出来"
      SIGKILL = 破门而入
      SIGINT  = 按门铃 (Ctrl+C)
    """
    print("=" * 60)
    print("第六课: 信号 — 操控进程的遥控器")
    print("=" * 60)

    # ---- 例 1: 超时杀死子进程 ----
    print()
    print("  例 1: 超时杀死子进程 (对应 bash.rs 的 timeout)")
    print("  ──────────────────────────────────────────────")

    start = time.time()
    proc = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(10); print('完成')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    timeout_seconds = 0.5

    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        print(f"    正常完成: {stdout.strip()}")
    except subprocess.TimeoutExpired:
        proc.kill()           # SIGKILL: 强制终止
        proc.communicate()    # 等待进程真正结束, 回收资源
        elapsed = time.time() - start
        print(f"    超时! {elapsed:.2f}s 后杀死子进程 (设置: {timeout_seconds}s)")
        print(f"    退出码: {proc.returncode}")  # -9 = 被 SIGKILL 杀死

    # ---- 例 2: 捕获信号 (优雅退出) ----
    print()
    print("  例 2: SIGTERM vs SIGKILL")
    print("  ────────────────────────")

    # SIGTERM: 子进程可以捕获并优雅退出
    proc = subprocess.Popen(
        ["python3", "-c", textwrap.dedent("""\
            import signal, sys, time
            def handler(sig, frame):
                print("收到 SIGTERM, 正在清理...", flush=True)
                sys.exit(0)
            signal.signal(signal.SIGTERM, handler)
            time.sleep(10)
        """)],
        stdout=subprocess.PIPE,
        text=True,
    )

    time.sleep(0.2)
    proc.send_signal(signal.SIGTERM)  # 礼貌请求退出
    stdout, _ = proc.communicate(timeout=2)
    print(f"    SIGTERM 后输出: {stdout.strip()}")
    print(f"    退出码: {proc.returncode}")  # 0 (优雅退出)

    print()
    print("    Claude Code 的超时策略 (bash.rs):")
    print("    ─────────────────────────────────")
    print("    1. 用 tokio::time::timeout 设置超时")
    print("    2. 超时后发 SIGKILL (不等清理)")
    print("    3. 返回 BashOutput { interrupted: true }")
    print("    4. 错误信息告诉模型命令超时了")
    print()


# ============================================================
# 第七课: 完整 Hook 系统重现
# ============================================================

def lesson_7_full_hook_system():
    """
    把前 6 课的知识全部组合, 重现 hooks.rs 的完整工作流:

    Claude Code 内部:
      1. 构造 JSON payload (hook_event_name, tool_name, tool_input...)
      2. 设置环境变量 (HOOK_EVENT, HOOK_TOOL_NAME...)
      3. 启动 Hook 脚本子进程 (stdin=PIPE, stdout=PIPE, stderr=PIPE)
      4. 通过 stdin 传入 JSON payload
      5. 读取 stdout (反馈信息)
      6. 读取退出码 (0=Allow, 2=Deny, other=Warn)
      7. 合并 Hook 反馈到工具输出 (merge_hook_feedback)
    """
    print("=" * 60)
    print("第七课: 完整 Hook 系统重现")
    print("=" * 60)

    # ---- Hook 脚本 (写成临时字符串, 模拟外部 .py 文件) ----

    # Hook 1: 安全检查 (拒绝 rm -rf 命令)
    safety_hook = textwrap.dedent("""\
        import sys, json, os
        # 方式 1: 从 stdin 读取完整 JSON
        data = json.load(sys.stdin)
        # 方式 2: 从环境变量读取 (更方便)
        tool_name = os.environ.get('HOOK_TOOL_NAME', '')
        command = data.get('tool_input', {}).get('command', '')

        if 'rm -rf' in command:
            print("BLOCKED: 检测到危险的 rm -rf 命令!")
            sys.exit(2)  # exit(2) = Deny

        print(f"安全检查通过: {tool_name}")
        sys.exit(0)  # exit(0) = Allow
    """)

    # Hook 2: 审计日志 (总是允许, 但记录信息)
    audit_hook = textwrap.dedent("""\
        import sys, json, os
        data = json.load(sys.stdin)
        tool = os.environ.get('HOOK_TOOL_NAME', 'unknown')
        event = os.environ.get('HOOK_EVENT', 'unknown')
        print(f"[AUDIT] {event}: {tool}")
        sys.exit(0)  # Always allow
    """)

    # ---- HookRunner: 重现 hooks.rs 的核心逻辑 ----

    class HookCommandOutcome:
        def __init__(self, outcome: str, message: str | None = None):
            self.outcome = outcome
            self.message = message

    class HookRunResult:
        """对应 hooks.rs:24-47"""
        def __init__(self, denied: bool, messages: list[str]):
            self.denied = denied
            self.messages = messages

        @classmethod
        def allow(cls, messages: list[str]) -> HookRunResult:
            return cls(denied=False, messages=messages)

        def is_denied(self) -> bool:
            return self.denied

    def run_single_hook(
        script: str,
        event: str,
        tool_name: str,
        tool_input: str,
        payload: str,
    ) -> HookCommandOutcome:
        """
        对应 hooks.rs:152-200 的 run_command()
        完整流程: 启动子进程 → stdin 传 JSON → 读 stdout → 解析退出码
        """
        # 构造子进程环境变量 (hooks.rs:166-172)
        env = os.environ.copy()
        env["HOOK_EVENT"] = event
        env["HOOK_TOOL_NAME"] = tool_name
        env["HOOK_TOOL_INPUT"] = tool_input
        env["HOOK_TOOL_IS_ERROR"] = "0"

        try:
            result = subprocess.run(
                ["python3", "-c", script],
                input=payload,          # stdin 传 JSON (hooks.rs:174)
                capture_output=True,    # stdout + stderr (hooks.rs:163-165)
                text=True,
                env=env,                # 环境变量 (hooks.rs:166-172)
                timeout=5,             # 防止 hook 挂死
            )

            stdout = result.stdout.strip()
            message = stdout if stdout else None

            # 退出码协议 (hooks.rs:179-196)
            if result.returncode == 0:
                return HookCommandOutcome("Allow", message)
            elif result.returncode == 2:
                return HookCommandOutcome(
                    "Deny",
                    message or f"Hook denied tool `{tool_name}`",
                )
            else:
                return HookCommandOutcome(
                    "Warn",
                    f"Hook exited with code {result.returncode}: {stdout or result.stderr.strip()}",
                )

        except subprocess.TimeoutExpired:
            return HookCommandOutcome("Warn", "Hook 超时被杀死")
        except Exception as e:
            return HookCommandOutcome("Warn", f"Hook 执行失败: {e}")

    def run_hooks(
        hooks: list[str],
        event: str,
        tool_name: str,
        tool_input: dict,
    ) -> HookRunResult:
        """
        对应 hooks.rs:95-150 的 run_commands()
        运行所有 hook, 遇到 Deny 立即停止
        """
        tool_input_json = json.dumps(tool_input)
        payload = json.dumps({
            "hook_event_name": event,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_input_json": tool_input_json,
            "tool_output": None,
            "tool_result_is_error": False,
        })

        messages: list[str] = []

        for hook_script in hooks:
            outcome = run_single_hook(
                hook_script, event, tool_name, tool_input_json, payload
            )

            if outcome.outcome == "Allow":
                if outcome.message:
                    messages.append(outcome.message)
            elif outcome.outcome == "Deny":
                msg = outcome.message or f"{event} hook denied tool `{tool_name}`"
                messages.append(msg)
                return HookRunResult(denied=True, messages=messages)
            else:  # Warn
                if outcome.message:
                    messages.append(outcome.message)

        return HookRunResult.allow(messages)

    # ---- merge_hook_feedback (conversation.rs:408-425) ----
    def merge_hook_feedback(
        hook_messages: list[str],
        tool_output: str,
        denied: bool,
    ) -> str:
        """对应 conversation.rs:408-425"""
        if not hook_messages:
            return tool_output

        sections = []
        if tool_output.strip():
            sections.append(tool_output)

        label = "Hook feedback (denied)" if denied else "Hook feedback"
        sections.append(f"<{label}>\n" + "\n".join(hook_messages) + f"\n</{label}>")

        return "\n\n".join(sections)

    # ---- 运行完整演示 ----

    pre_hooks = [audit_hook, safety_hook]

    print()
    print("  场景 1: 正常命令 (ls)")
    print("  ─────────────────────")
    result = run_hooks(pre_hooks, "PreToolUse", "bash", {"command": "ls -la"})
    print(f"    denied: {result.is_denied()}")
    for msg in result.messages:
        print(f"    message: {msg}")

    tool_output = "main.py\ntest.py\nREADME.md"
    merged = merge_hook_feedback(result.messages, tool_output, result.is_denied())
    print(f"\n    合并后的工具输出:")
    for line in merged.split("\n"):
        print(f"      {line}")

    print()
    print("  场景 2: 危险命令 (rm -rf /)")
    print("  ──────────────────────────")
    result = run_hooks(pre_hooks, "PreToolUse", "bash", {"command": "rm -rf /"})
    print(f"    denied: {result.is_denied()}")
    for msg in result.messages:
        print(f"    message: {msg}")

    denied_output = merge_hook_feedback(result.messages, "", result.is_denied())
    print(f"\n    合并后的工具输出:")
    for line in denied_output.split("\n"):
        print(f"      {line}")

    print()
    print("  完整数据流:")
    print("  ──────────")
    print("    1. Agentic loop 决定调用 bash 工具")
    print("    2. 运行 pre_tool_use hooks:")
    print("       audit_hook → stdin:JSON → stdout:日志 → exit(0):Allow")
    print("       safety_hook → stdin:JSON → 检查命令 → exit(0/2):Allow/Deny")
    print("    3. 如果 Deny → 不执行工具, 返回拒绝信息给模型")
    print("    4. 如果 Allow → 执行工具 → 运行 post_tool_use hooks")
    print("    5. merge_hook_feedback: hook 消息追加到工具输出")
    print("    6. 模型看到完整的 '工具输出 + hook 反馈'")
    print()


# ============================================================
# 速查表
# ============================================================

def cheatsheet():
    print("=" * 60)
    print("速查表: 进程通信 → Claude Code 源码 → Python")
    print("=" * 60)
    print("""
    概念           Python                     Claude Code (Rust)
    ───────────   ──────────────────────────  ─────────────────────────
    启动子进程     subprocess.run/Popen        Command::new() / TokioCommand
    标准输入       input= / stdin=PIPE         Stdio::piped() + write
    标准输出       capture_output / stdout=PIPE Stdio::piped() + read
    环境变量       env=dict / os.environ       child.env("KEY", "val")
    退出码         result.returncode           output.status.code()
    超时           timeout= / communicate()    tokio::time::timeout
    杀死进程       proc.kill() / terminate()   drop(child) / SIGKILL
    管道链         p1.stdout → p2.stdin        Unix pipe / Stdio

    Hook 退出码协议:
    ─────────────────
      exit(0)  → Allow  (允许工具执行)
      exit(2)  → Deny   (拒绝工具执行)
      其他     → Warn   (警告但继续)
      被信号杀  → Warn   (超时等情况)

    Hook 数据传递:
    ──────────────
      stdin    → 完整 JSON payload (结构化数据)
      env vars → 简单标量 (HOOK_EVENT, HOOK_TOOL_NAME...)
      stdout   → Hook 反馈信息 (合并到工具输出)
      exit code → 允许/拒绝/警告 决定
    """)


# ============================================================
# 主程序
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("教程 19: 进程与管道通信")
    print("Claude Code 如何与外部程序 (Hook, Bash) 通信")
    print("=" * 60)

    lesson_1_process_basics()
    lesson_2_standard_streams()
    lesson_3_pipe()
    lesson_4_environment_variables()
    lesson_5_exit_codes()
    lesson_6_signals()
    lesson_7_full_hook_system()
    cheatsheet()
