# 11 - 多 Agent 协作 (multi_agent.py)

## 问题背景

一个 agent 不够用时，Leader 可以派 Worker 去做子任务。但怎么派？怎么隔离上下文？怎么追踪进度？Worker 崩了怎么办？

## CC 的做法

源码: `rust/crates/tools/src/lib.rs:1340-1660`

CC 的多 agent 系统有三个核心设计:

### 1. 泛型 spawn_fn — 解耦"怎么派"和"派什么"

```rust
fn execute_agent_with_spawn<F>(input: AgentInput, spawn_fn: F) -> Result<AgentOutput, String>
where
    F: FnOnce(AgentJob) -> Result<(), String>,
```

`execute_agent_with_spawn` 不关心 agent 是在线程、进程还是容器里跑。它只负责:
1. 验证输入
2. 生成 ID、创建输出文件
3. 写入 manifest JSON
4. 调用 `spawn_fn(job)` — 具体怎么执行由调用方决定

默认实现 `spawn_agent_job` 用 `std::thread::spawn`。
测试时可以传假 spawn_fn，不真正启动线程。

### 2. Manifest JSON — 追踪 agent 生命周期

每个 agent 有一个 JSON 文件记录状态:

```json
{
  "agent_id": "abc123",
  "name": "fix-auth-bug",
  "status": "running",          // running → completed / failed
  "output_file": "abc123.md",
  "created_at": "2026-04-08T...",
  "started_at": "2026-04-08T...",
  "completed_at": null,         // 完成时填入
  "error": null                 // 失败时填入
}
```

好处:
- Leader 随时查看 Worker 状态
- 崩溃后可以检测哪些 agent 未完成
- 结果通过文件传递，不需要进程间通信

### 3. 工具白名单 — 按角色限制能力

```
Explore agent:  只能 read/grep/glob/web (只读)
Plan agent:     只能读 + todo (不能改代码)
General agent:  几乎全部工具 (但不能递归 spawn Agent)
```

注意: **所有 subagent 都不能 spawn Agent** — 防止递归。

## 你要练习的工程模式

| 模式 | 说明 | 源码位置 |
|------|------|---------|
| **泛型 spawn_fn** | 策略模式，解耦 spawn 机制 | lib.rs:1347-1350 |
| **Manifest 追踪** | JSON 文件记录 agent 生命周期 | lib.rs:1392-1405 |
| **工具白名单** | 按 subagent_type 限制可用工具 | lib.rs:1503-1582 |
| **Panic 防护** | catch_unwind 防止 worker 崩溃影响 leader | lib.rs:1430 |
| **文件邮箱** | agent 间通过文件通信 | lib.rs:1361-1362 |

## 你需要写的东西

```python
AgentManifest(BaseModel)
  agent_id: str
  name: str
  description: str
  subagent_type: str
  status: str           # "pending" / "running" / "completed" / "failed"
  output_file: str
  created_at: str
  started_at: Optional[str]
  completed_at: Optional[str]
  error: Optional[str]

AgentJob(BaseModel)
  manifest: AgentManifest
  prompt: str
  allowed_tools: set[str]

TOOL_WHITELIST: dict[str, set[str]]   # subagent_type → 允许的工具集

allowed_tools_for_subagent(subagent_type: str) -> set[str]

AgentOrchestrator
  __init__(store_dir: Path, spawn_fn: Callable = None)
  spawn_agent(description, prompt, subagent_type="general") -> AgentManifest
    验证输入 → 创建文件 → 写 manifest → 调 spawn_fn
  get_status(agent_id) -> AgentManifest     # 读 manifest JSON
  list_agents() -> list[AgentManifest]
  complete_agent(agent_id, result)           # 标记完成
  fail_agent(agent_id, error)               # 标记失败

默认 spawn_fn: 用 threading.Thread 在后台跑
```

## 易错点

- Agent 不能递归 spawn Agent — 白名单里没有 "Agent" 工具
- manifest 要在 spawn_fn 调用前写入（这样即使 spawn 失败也有记录）
- `completed_at` 只在终态才填写
- spawn_fn 崩溃时要 catch exception 并标记 agent 为 failed
- 文件名用 agent_id 命名（.md 输出 + .json manifest）
