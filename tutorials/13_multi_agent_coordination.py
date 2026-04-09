"""
Tutorial 13: 多 Agent 协调 — 从独奏到交响乐
=============================================

为什么需要多 Agent？

    一个 Agent 的上下文窗口是有限的（比如 200K tokens）。
    当任务复杂到一个 Agent 装不下时，怎么办？
    答案是：分身术 — 派多个 Agent 各自处理一部分。

    单 Agent = 一个人做所有事（容易过载）
    多 Agent = 一个领导 + 多个工人（分工合作）

生活类比：
    单 Agent = 你自己一个人装修房子。
        你又要设计、又要买材料、又要施工、又要验收...

    多 Agent = 你当包工头（Leader），雇了几个工人（Worker）：
        - 你分配任务："张三去买材料，李四去刷墙"
        - 工人各干各的，互不干扰
        - 工人完成后向你汇报结果
        - 你综合所有结果，做出最终决策
        - 核心原则：你负责思考，工人负责执行

Claude Code 的多 Agent 设计：
    1. Leader-Worker 模型 — Leader 思考 + 分配，Worker 执行
    2. 上下文隔离 — Worker 看不到 Leader 的对话（强制写清任务）
    3. 文件邮箱 — Agent 间通过 JSON 文件通信（简单、可调试、崩溃安全）
    4. 权限委托 — Worker 无法直接问用户，要通过 Leader 转达

对应源码与 Reference：
    - reference EP03 → 协调器模式（Coordinator）
    - reference EP08 → Swarm 多 Agent 系统（邮箱、后端、权限）
    - rust/crates/runtime/src/conversation.rs → ConversationRuntime（单 Agent 基础）

运行方式：python tutorials/13_multi_agent_coordination.py
"""

import json
import os
import time
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ============================================================
# 第一步：理解 Leader-Worker 模型
# ============================================================
# Claude Code 的多 Agent 架构：
#
#   ┌──────────────────────────────┐
#   │         Leader (领导)         │
#   │  - 拥有完整上下文             │
#   │  - 负责理解、规划、综合       │
#   │  - 不直接执行文件/命令操作    │
#   │  - 只能调度 Worker            │
#   └──────────┬───────────────────┘
#              │ 分配任务
#     ┌────────┼────────┐
#     ▼        ▼        ▼
#  ┌──────┐ ┌──────┐ ┌──────┐
#  │Worker│ │Worker│ │Worker│
#  │研究员│ │实现者│ │测试员│
#  └──────┘ └──────┘ └──────┘
#  各自独立   各自独立   各自独立
#  零上下文   零上下文   零上下文
#
# 关键原则（来自 Reference EP03）：
#   "永远不要委托'理解'过程"
#   Leader 必须亲自综合 Worker 的发现，不能说"根据你的发现去做"。
#   Leader 要写出自包含的任务描述（包含所有必要信息）。


# ============================================================
# 第二步：Agent 身份
# ============================================================

@dataclass
class AgentIdentity:
    """
    Agent 的身份信息。

    格式：{name}@{team_name}
    例如：researcher@my-project, team-lead@my-project

    对应 Reference: EP08 §5
    """
    name: str
    team_name: str
    role: str  # "leader" or "worker"

    @property
    def agent_id(self) -> str:
        return f"{self.name}@{self.team_name}"


# ============================================================
# 第三步：消息类型 — Agent 间的通信协议
# ============================================================

class MessageType(Enum):
    """
    Agent 间的消息类型。

    对应 Reference: EP08 §2
        | 类型                 | 方向        | 用途              |
        | 纯文本私信           | 任意 → 任意 | 直接消息           |
        | idle_notification    | 工人 → 领导 | "我完成了/被阻塞了" |
        | permission_request   | 工人 → 领导 | 工具权限委托        |
        | permission_response  | 领导 → 工人 | 权限授予/拒绝       |
        | shutdown_request     | 领导 → 工人 | 优雅关闭           |
    """
    TASK = "task"                     # 任务指令（Leader → Worker）
    RESULT = "result"                 # 任务结果（Worker → Leader）
    IDLE = "idle_notification"        # 空闲通知（Worker → Leader）
    PERMISSION_REQ = "permission_request"   # 权限请求
    PERMISSION_RESP = "permission_response"  # 权限响应
    SHUTDOWN = "shutdown_request"      # 关闭请求
    BROADCAST = "broadcast"            # 广播（Leader → All）


@dataclass
class AgentMessage:
    """Agent 间传递的消息"""
    from_agent: str
    to_agent: str
    msg_type: MessageType
    content: str
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "from": self.from_agent,
            "to": self.to_agent,
            "type": self.msg_type.value,
            "content": self.content,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_dict(data: dict) -> "AgentMessage":
        return AgentMessage(
            from_agent=data["from"],
            to_agent=data["to"],
            msg_type=MessageType(data["type"]),
            content=data["content"],
            timestamp=data.get("timestamp", 0.0),
        )


# ============================================================
# 第四步：文件邮箱系统 — Agent 间的通信骨干
# ============================================================
# 为什么用文件而不是内存队列/WebSocket？
#
#   1. 跨进程 — Worker 可能是独立进程（tmux 窗格）
#   2. 崩溃安全 — 消息持久化在磁盘上
#   3. 可调试 — cat ~/.claude/teams/my-team/inboxes/researcher.json
#   4. 简单 — 无守护进程，无端口，无服务发现
#
# 对应 Reference: EP08 §2
#   ~/.claude/teams/{team-name}/
#   ├── config.json
#   └── inboxes/
#       ├── team-lead.json
#       ├── researcher.json
#       └── test-runner.json

class FileMailbox:
    """
    基于文件的邮箱系统。

    每个 Agent 有一个 JSON 文件作为收件箱。
    发送消息 = 写入对方的收件箱文件。
    接收消息 = 读取自己的收件箱文件。

    对应 Reference: EP08 §2
        teammateMailbox.ts (1,184 行)
        基于文件的邮箱 + 锁文件并发控制
    """

    def __init__(self, team_dir: str):
        self.team_dir = team_dir
        self.inbox_dir = os.path.join(team_dir, "inboxes")
        os.makedirs(self.inbox_dir, exist_ok=True)

    def _inbox_path(self, agent_name: str) -> str:
        return os.path.join(self.inbox_dir, f"{agent_name}.json")

    def send(self, message: AgentMessage):
        """
        发送消息到目标 Agent 的收件箱。

        真实实现中有锁文件保护（防止并发写入冲突）。
        简化版直接写文件。

        对应 Reference: EP08 §2
            多个 Claude 实例可以并发写入 ——
            锁文件通过指数退避重试序列化访问
        """
        inbox_path = self._inbox_path(message.to_agent)

        # 读取现有消息
        messages = []
        if os.path.exists(inbox_path):
            with open(inbox_path, "r") as f:
                try:
                    messages = json.load(f)
                except json.JSONDecodeError:
                    messages = []

        # 追加新消息
        messages.append(message.to_dict())

        # 写回文件
        with open(inbox_path, "w") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)

    def receive(self, agent_name: str) -> list[AgentMessage]:
        """
        接收（并清空）收件箱中的所有消息。
        """
        inbox_path = self._inbox_path(agent_name)
        if not os.path.exists(inbox_path):
            return []

        with open(inbox_path, "r") as f:
            try:
                raw_messages = json.load(f)
            except json.JSONDecodeError:
                return []

        # 清空收件箱
        with open(inbox_path, "w") as f:
            json.dump([], f)

        return [AgentMessage.from_dict(m) for m in raw_messages]

    def peek(self, agent_name: str) -> int:
        """查看收件箱中有多少消息（不取出）"""
        inbox_path = self._inbox_path(agent_name)
        if not os.path.exists(inbox_path):
            return 0
        with open(inbox_path, "r") as f:
            try:
                return len(json.load(f))
            except json.JSONDecodeError:
                return 0


# ============================================================
# 第五步：Worker — 执行具体任务的 Agent
# ============================================================

class Worker:
    """
    Worker Agent — 接收任务，执行，汇报结果。

    关键设计：Worker 从零上下文开始！
    它不知道 Leader 之前聊了什么。
    Leader 必须在任务描述中包含所有必要信息。

    对应 Reference: EP03 §2
        "Worker 无法看到协调者的对话历史。"
        "每个 Worker 启动时都是零上下文的。"
    """

    def __init__(self, identity: AgentIdentity, mailbox: FileMailbox,
                 executor=None):
        self.identity = identity
        self.mailbox = mailbox
        self.executor = executor or self._default_executor

    def _default_executor(self, task: str) -> str:
        """模拟执行任务"""
        if "search" in task.lower() or "find" in task.lower():
            return "Found 3 relevant files: auth.py, login.py, session.py"
        elif "test" in task.lower():
            return "All 12 tests passed. Coverage: 87%"
        elif "fix" in task.lower() or "implement" in task.lower():
            return "Changes applied to 2 files. See diff above."
        return f"Task completed: {task[:50]}"

    def process_messages(self):
        """
        处理收件箱中的消息。

        对应 Reference: EP08 §1
            Worker 的生命周期：接收任务 → 执行 → 汇报
        """
        messages = self.mailbox.receive(self.identity.name)
        results = []

        for msg in messages:
            if msg.msg_type == MessageType.TASK:
                print(f"    [{self.identity.name}] 收到任务: "
                      f"{msg.content[:40]}...")
                # 执行任务
                result = self.executor(msg.content)
                print(f"    [{self.identity.name}] 完成: {result[:40]}...")

                # 发送结果给 Leader
                self.mailbox.send(AgentMessage(
                    from_agent=self.identity.name,
                    to_agent=msg.from_agent,
                    msg_type=MessageType.RESULT,
                    content=result,
                ))
                results.append(result)

            elif msg.msg_type == MessageType.SHUTDOWN:
                print(f"    [{self.identity.name}] 收到关闭请求，退出")
                return "shutdown"

        return results


# ============================================================
# 第六步：Leader — 思考、规划、分配、综合
# ============================================================

class Leader:
    """
    Leader Agent — 协调多个 Worker 完成复杂任务。

    Leader 的四阶段工作流（来自 Reference EP03 §4）：
    1. 研究 (Research) — 并行分发 Worker 调研
    2. 综合 (Synthesis) — Leader 亲自理解和整合发现
    3. 实现 (Implementation) — Worker 根据 Leader 的具体规范执行
    4. 验证 (Verification) — 新 Worker 独立验证

    对应 Reference: EP03 §4
        核心信条："永远不要委托'理解'过程"
    """

    def __init__(self, team_name: str, mailbox: FileMailbox):
        self.identity = AgentIdentity(
            name="team-lead", team_name=team_name, role="leader"
        )
        self.mailbox = mailbox
        self.workers: dict[str, Worker] = {}

    def spawn_worker(self, name: str, executor=None) -> Worker:
        """
        生成一个新 Worker。

        对应 Reference: EP08 §1 生成队友
            spawnMultiAgent.ts (1,094 行)
            1. 解析模型  2. 生成唯一名称  3. 检测后端
            4. 创建窗格/进程  5. 注册到团队  6. 发送初始消息
        """
        worker_identity = AgentIdentity(
            name=name, team_name=self.identity.team_name, role="worker"
        )
        worker = Worker(worker_identity, self.mailbox, executor)
        self.workers[name] = worker
        print(f"  [Leader] 生成 Worker: {name}")
        return worker

    def assign_task(self, worker_name: str, task: str):
        """
        给 Worker 分配任务。

        关键：任务描述必须是自包含的！
        不能说"根据之前的讨论去做"，因为 Worker 不知道之前讨论了什么。

        对应 Reference: EP03 §2
            "协调者必须编写自包含的 Prompt，
             包括 Worker 所需的一切：文件路径、行号、错误信息
             以及'完成'的标准。"
        """
        self.mailbox.send(AgentMessage(
            from_agent=self.identity.name,
            to_agent=worker_name,
            msg_type=MessageType.TASK,
            content=task,
        ))
        print(f"  [Leader] 分配任务给 {worker_name}: {task[:40]}...")

    def collect_results(self) -> list[AgentMessage]:
        """收集 Worker 的结果"""
        return self.mailbox.receive(self.identity.name)

    def synthesize(self, results: list[str]) -> str:
        """
        综合 Worker 的发现。这是 Leader 的核心价值。

        对应 Reference: EP03 §4
            "协调者拥有综合权 —— 不做'基于你的发现'式委派"
        """
        return "\n".join([f"  - {r}" for r in results])

    def shutdown_all(self):
        """关闭所有 Worker"""
        for name in self.workers:
            self.mailbox.send(AgentMessage(
                from_agent=self.identity.name,
                to_agent=name,
                msg_type=MessageType.SHUTDOWN,
                content="Task complete, shutting down",
            ))
            print(f"  [Leader] 发送关闭请求给 {name}")


# ============================================================
# 第七步：完整的四阶段工作流演示
# ============================================================

def demo_four_phase_workflow():
    """
    演示 Leader-Worker 四阶段工作流。

    对应 Reference: EP03 §4
        1. 研究 → 2. 综合 → 3. 实现 → 4. 验证
    """
    tmpdir = tempfile.mkdtemp(prefix="team_demo_")
    mailbox = FileMailbox(tmpdir)
    leader = Leader("fix-auth-bug", mailbox)

    print("\n  === 第 1 阶段：研究 (Research) ===")
    print("  Leader 并行派出研究员调查问题")

    # 生成研究 Worker
    researcher_a = leader.spawn_worker("researcher-a")
    researcher_b = leader.spawn_worker("researcher-b")

    # 分配研究任务（注意：任务描述是自包含的！）
    leader.assign_task("researcher-a",
        "Search the codebase for all authentication-related files. "
        "Look in src/auth/, src/login/, src/session/. "
        "Report which files handle password validation.")
    leader.assign_task("researcher-b",
        "Find all test files related to authentication. "
        "Look in tests/auth/, tests/integration/. "
        "Report which tests are failing and their error messages.")

    # Worker 处理任务
    researcher_a.process_messages()
    researcher_b.process_messages()

    print("\n  === 第 2 阶段：综合 (Synthesis) ===")
    print("  Leader 亲自理解和整合发现")

    results = leader.collect_results()
    findings = [r.content for r in results]

    # Leader 综合（不是委托给 Worker！）
    synthesis = leader.synthesize(findings)
    print(f"  [Leader] 综合分析结果:")
    print(synthesis)
    print(f"  [Leader] 结论: auth.py 中的密码验证函数有空指针问题")

    print("\n  === 第 3 阶段：实现 (Implementation) ===")
    print("  Leader 给出具体规范，Worker 执行")

    implementer = leader.spawn_worker("implementer")
    leader.assign_task("implementer",
        "Fix the null pointer bug in src/auth/password.py line 42. "
        "The variable 'user_record' can be None when the user "
        "doesn't exist. Add a None check before accessing "
        "user_record.password_hash. "
        "Done when: the function returns False for non-existent users "
        "instead of crashing.")
    implementer.process_messages()

    print("\n  === 第 4 阶段：验证 (Verification) ===")
    print("  Leader 派出全新的 Worker 独立验证")
    print("  （不用实现者，因为要'旁观者清'）")

    # 故意用新 Worker 验证（Reference EP03: "验证任务通常产生全新 Worker"）
    verifier = leader.spawn_worker("verifier")
    leader.assign_task("verifier",
        "Run the authentication test suite: "
        "python -m pytest tests/auth/ -v. "
        "Report pass/fail counts and any remaining failures.")
    verifier.process_messages()

    # 收集验证结果
    verify_results = leader.collect_results()
    for r in verify_results:
        print(f"  [Leader] 验证结果: {r.content}")

    print("\n  === 完成！Leader 做最终总结 ===")
    print("  [Leader] Bug 已修复并验证通过。")

    # 关闭所有 Worker
    leader.shutdown_all()
    for w in leader.workers.values():
        w.process_messages()

    # 查看邮箱文件（可调试性）
    print("\n  === 邮箱文件内容（可调试性）===")
    for f in os.listdir(mailbox.inbox_dir):
        path = os.path.join(mailbox.inbox_dir, f)
        size = os.path.getsize(path)
        print(f"    {f}: {size} bytes")

    # 清理
    import shutil
    shutil.rmtree(tmpdir)


def main():
    print("=" * 60)
    print("Tutorial 13: 多 Agent 协调")
    print("=" * 60)

    # --- 1. 基础概念 ---
    print("\n--- 1. Leader-Worker 模型 ---")
    print("""
  单 Agent 模式:
    用户 ↔ Agent (一个人干所有事)

  多 Agent 模式 (Coordinator):
    用户 ↔ Leader (思考、规划、综合)
              ├─→ Worker-A (研究)
              ├─→ Worker-B (实现)
              └─→ Worker-C (验证)

  核心原则: "Leader 负责理解，Worker 负责执行"
    """)

    # --- 2. 四阶段工作流 ---
    print("--- 2. 四阶段工作流演示 ---")
    demo_four_phase_workflow()

    # --- 3. 全景图 ---
    print("\n" + "=" * 60)
    print("多 Agent 协调全景图")
    print("=" * 60)
    print("""
    用户请求: "修复 auth 模块的 bug"
        │
        ▼
    ┌─────────────────────────────────────┐
    │  Leader (协调者)                     │
    │  - 拥有用户上下文                    │
    │  - 不执行文件/命令操作               │
    │  - 只能 spawn Worker + send message  │
    └───────────────┬─────────────────────┘
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
    ┌────────┐ ┌────────┐ ┌────────┐
    │ Worker │ │ Worker │ │ Worker │
    │  研究   │ │  实现   │ │  验证   │
    │        │ │        │ │        │
    │ 零上下文│ │ 零上下文│ │ 零上下文│
    │ 独立进程│ │ 独立进程│ │ 独立进程│
    └────┬───┘ └────┬───┘ └────┬───┘
         │          │          │
         └──────────┼──────────┘
                    │ 结果通过文件邮箱返回
                    ▼
    ┌─────────────────────────────────────┐
    │  Leader 综合所有结果                  │
    │  "研究发现 auth.py:42 有空指针"       │
    │  "实现者已修复"                       │
    │  "验证者确认测试通过"                 │
    │  → 最终回复用户                       │
    └─────────────────────────────────────┘

    通信方式: 文件邮箱
    ┌─────────────────────────────────────┐
    │  ~/.claude/teams/{team}/inboxes/     │
    │  ├── team-lead.json  (Leader 收件箱) │
    │  ├── researcher.json                 │
    │  ├── implementer.json                │
    │  └── verifier.json                   │
    │                                     │
    │  优点: 跨进程、崩溃安全、可调试       │
    │  (cat researcher.json 就能看消息)    │
    └─────────────────────────────────────┘

    五条核心设计原则（来自 Reference）:

    1. 上下文隔离（EP03）
       Worker 看不到 Leader 的对话。这不是 bug，是设计。
       强制 Leader 写出自包含的任务描述。

    2. 永不委托理解（EP03）
       Leader 必须自己综合 Worker 的发现。
       不能说"根据你的发现去实现"。

    3. 读写隔离（EP08）
       研究任务并行（只读），写操作按文件集串行。
       防止多个 Worker 同时修改同一个文件。

    4. 新 Worker 验证（EP03）
       验证阶段用全新 Worker，不用实现者。
       因为实现者可能有偏见（"我写的代码当然没 bug"）。

    5. 权限委托（EP08）
       Worker 没有终端，不能直接问用户。
       需要权限时: Worker → Leader → 用户 → Leader → Worker。

    对应源码 / Reference:
    - EP03: coordinator/coordinatorMode.ts (370 行)
    - EP08: utils/swarm/ (~30 文件, ~6,800 行)
    - EP08: tools/SendMessageTool/ (918 行)
    - EP08: utils/teammateMailbox.ts (1,184 行)
    """)


if __name__ == "__main__":
    main()
