"""
multi_agent.py — 泛型 spawn_fn + Manifest 追踪 + 工具白名单

忠实还原 Claude Code 的多 agent 协作系统。
源码对照: rust/crates/tools/src/lib.rs:1340-1660

核心工程要点:
1. 泛型 spawn_fn: 策略模式，解耦"怎么派"和"怎么执行" (lib.rs:1347-1350)
2. Manifest JSON: 追踪 agent 生命周期，崩溃后可检测未完成任务 (lib.rs:1392-1405)
3. 工具白名单: 按 subagent_type 限制可用工具，所有 subagent 都不能 spawn Agent (lib.rs:1503-1582)
4. Panic 防护: catch_unwind / try-except，worker 崩溃不影响 leader (lib.rs:1430)
5. 文件邮箱: agent 间通过文件通信，不需要进程间通道 (lib.rs:1361-1362)
"""

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field


# ============================================================
# AgentManifest — Agent 生命周期追踪
# 源码: lib.rs:1392-1405 (AgentOutput struct)
#
# 对应 CC 的 AgentOutput。每个 agent 有一个 JSON manifest 文件，
# 记录 status (running/completed/failed)、时间戳、输出文件路径。
# Leader 随时可以读 manifest 查看 worker 状态。
# ============================================================

class AgentManifest(BaseModel):
    agent_id: str
    name: str
    description: str
    subagent_type: str
    status: str = "running"  # running → completed / failed
    output_file: str = ""
    manifest_file: str = ""
    created_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None


# ============================================================
# AgentJob — spawn_fn 的参数包
# 源码: lib.rs:1409-1414 (AgentJob struct)
#
# 把 manifest + prompt + allowed_tools 打包传给 spawn_fn。
# spawn_fn 只需要看这一个对象就够了，不需要知道其他上下文。
# ============================================================

class AgentJob(BaseModel):
    manifest: AgentManifest
    prompt: str
    allowed_tools: set[str] = Field(default_factory=set)


# ============================================================
# 工具白名单 — 按 subagent_type 限制可用工具
# 源码: lib.rs:1503-1582
#
# 关键:
# 1. 所有白名单都不包含 "Agent" — 防止递归 spawn
# 2. Explore 是只读 (read/grep/glob/web)
# 3. Plan 可读 + TodoWrite，但不能改代码
# 4. Verification 可以跑 bash，但不能 write_file
# 5. general-purpose (默认) 有几乎全部工具
#
# CC 用 BTreeSet (有序集合) 存储，我们用 frozenset (不可变集合)。
# 不可变是为了防止运行时意外修改白名单。
# ============================================================

TOOL_WHITELIST: dict[str, frozenset[str]] = {
    "Explore": frozenset([
        "read_file", "glob_search", "grep_search",
        "WebFetch", "WebSearch", "ToolSearch", "Skill",
    ]),
    "Plan": frozenset([
        "read_file", "glob_search", "grep_search",
        "WebFetch", "WebSearch", "ToolSearch", "Skill",
        "TodoWrite", "SendUserMessage",
    ]),
    "Verification": frozenset([
        "bash", "read_file", "glob_search", "grep_search",
        "WebFetch", "WebSearch", "ToolSearch",
        "TodoWrite", "SendUserMessage",
    ]),
    # 默认: general-purpose — 几乎全部工具，但没有 Agent
    "general-purpose": frozenset([
        "bash", "read_file", "write_file", "edit_file",
        "glob_search", "grep_search",
        "WebFetch", "WebSearch", "TodoWrite", "Skill",
        "ToolSearch", "NotebookEdit", "Sleep",
        "SendUserMessage",
    ]),
}


def allowed_tools_for_subagent(subagent_type: str) -> frozenset[str]:
    """源码: lib.rs:1503-1582

    查白名单，找不到就用 general-purpose 的默认白名单。
    注意: 返回的是不可变集合，防止调用方修改。
    """
    return TOOL_WHITELIST.get(subagent_type, TOOL_WHITELIST["general-purpose"])


# ============================================================
# subagent_type 别名归一化
# 源码: lib.rs:2077-2094
#
# 用户可能输入 "explore", "explorer", "Explore" 等变体，
# 统一归一化到标准名称。CC 先做 canonical_tool_token (转小写+去特殊字符)
# 再匹配预定义别名。
#
# 这个设计的好处: LLM 输出的 subagent_type 可能大小写不一致，
# 归一化后白名单查找就不会 miss。
# ============================================================

def _canonical_token(text: str) -> str:
    """转小写 + 只保留字母数字。源码: lib.rs 的 canonical_tool_token"""
    return re.sub(r"[^a-z0-9]", "", text.lower())


_SUBAGENT_ALIASES: dict[str, str] = {
    "general": "general-purpose",
    "generalpurpose": "general-purpose",
    "generalpurposeagent": "general-purpose",
    "explore": "Explore",
    "explorer": "Explore",
    "exploreagent": "Explore",
    "plan": "Plan",
    "planagent": "Plan",
    "verification": "Verification",
    "verificationagent": "Verification",
    "verify": "Verification",
    "verifier": "Verification",
}


def normalize_subagent_type(subagent_type: Optional[str]) -> str:
    """源码: lib.rs:2077-2094

    空/None → "general-purpose"
    已知别名 → 标准名
    未知 → 原样返回 (trim)
    """
    trimmed = (subagent_type or "").strip()
    if not trimmed:
        return "general-purpose"
    canonical = _canonical_token(trimmed)
    return _SUBAGENT_ALIASES.get(canonical, trimmed)


# ============================================================
# Agent name slug 化
# 源码: lib.rs:2057-2075
#
# 把 description 或显式 name 转成 URL-safe 的 slug:
# - 转小写
# - 非字母数字替换为 '-'
# - 合并连续 '-'
# - 两端去 '-'
# - 最多 32 字符
# ============================================================

def slugify_agent_name(name: str) -> str:
    """源码: lib.rs:2057-2075 (slugify_agent_name)"""
    slug = re.sub(r"[^a-z0-9]", "-", name.lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:32]


# ============================================================
# ISO 8601 时间戳
# 源码: lib.rs:2096-2101
# ============================================================

def _iso8601_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# AgentOrchestrator — 多 agent 编排器
# 源码: lib.rs:1347-1421 (execute_agent_with_spawn) + 1424-1448 (spawn_agent_job)
#
# 核心设计:
# 1. spawn_fn 是可注入的 Callable — 策略模式
#    - 生产环境: 默认用 threading.Thread (对应 CC 的 std::thread::spawn)
#    - 测试时: 传入假 spawn_fn，不真正启线程
#
# 2. manifest 在 spawn_fn 之前写入
#    这样即使 spawn 失败 (线程创建失败)，manifest 也已存在，
#    leader 能知道有个 agent 试图启动但失败了。
#
# 3. spawn_fn 崩溃时自动标记 agent 为 failed
#    CC 用 catch_unwind (Rust 的 panic 捕获)，我们用 try/except。
# ============================================================

# 默认 spawn_fn 的类型签名 (对应 CC 的 FnOnce(AgentJob) -> Result<(), String>)
SpawnFn = Callable[[AgentJob], None]


class AgentOrchestrator:
    def __init__(
        self,
        store_dir: Path,
        spawn_fn: Optional[SpawnFn] = None,
    ):
        """
        参数:
            store_dir: agent 文件存储目录 (manifest JSON + output MD)
            spawn_fn: 自定义 spawn 策略。None 时使用默认的 threading.Thread。

        CC 对照:
        - store_dir: 环境变量 CLAWD_AGENT_STORE 或默认 ~/.clawd/agents/
        - spawn_fn: lib.rs:1347 的泛型 F: FnOnce(AgentJob) -> Result<(), String>
        """
        self._store_dir = store_dir
        self._spawn_fn = spawn_fn or self._default_spawn

    # --------------------------------------------------------
    # spawn_agent — 创建并启动一个 subagent
    # 源码: lib.rs:1347-1421 (execute_agent_with_spawn)
    #
    # 流程:
    # 1. 验证输入 (description/prompt 非空)
    # 2. 生成 agent_id、文件路径
    # 3. 归一化 subagent_type
    # 4. 查工具白名单
    # 5. 写输出文件 (.md) — agent 任务描述
    # 6. 写 manifest (.json) — 生命周期追踪
    # 7. 调 spawn_fn — 具体执行由调用方决定
    # 8. spawn 失败 → 标记 failed，抛异常
    # --------------------------------------------------------

    def spawn_agent(
        self,
        description: str,
        prompt: str,
        subagent_type: Optional[str] = None,
        name: Optional[str] = None,
    ) -> AgentManifest:
        """源码: lib.rs:1347-1421"""
        # 步骤 1: 验证 — CC 在 lib.rs:1351-1356 做同样的检查
        if not description.strip():
            raise ValueError("description must not be empty")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")

        # 步骤 2: 生成 ID 和路径
        agent_id = uuid.uuid4().hex[:12]
        self._store_dir.mkdir(parents=True, exist_ok=True)
        output_file = self._store_dir / f"{agent_id}.md"
        manifest_file = self._store_dir / f"{agent_id}.json"

        # 步骤 3: 归一化 subagent_type (lib.rs:1363)
        normalized_type = normalize_subagent_type(subagent_type)

        # 步骤 4: 查白名单 (lib.rs:1373)
        tools = allowed_tools_for_subagent(normalized_type)

        # Agent name: 显式 name 或从 description 生成 slug (lib.rs:1365-1370)
        agent_name = slugify_agent_name(name if name else description)

        created_at = _iso8601_now()

        # 步骤 5: 写输出文件 — agent 任务描述 (lib.rs:1375-1390)
        output_contents = (
            f"# Agent Task\n\n"
            f"- id: {agent_id}\n"
            f"- name: {agent_name}\n"
            f"- description: {description}\n"
            f"- subagent_type: {normalized_type}\n"
            f"- created_at: {created_at}\n\n"
            f"## Prompt\n\n{prompt}\n"
        )
        output_file.write_text(output_contents, encoding="utf-8")

        # 步骤 6: 写 manifest — 在 spawn_fn 之前! (lib.rs:1392-1406)
        manifest = AgentManifest(
            agent_id=agent_id,
            name=agent_name,
            description=description,
            subagent_type=normalized_type,
            status="running",
            output_file=str(output_file),
            manifest_file=str(manifest_file),
            created_at=created_at,
            started_at=created_at,
        )
        self._write_manifest(manifest)

        # 步骤 7: 创建 job 并调用 spawn_fn (lib.rs:1408-1419)
        job = AgentJob(
            manifest=manifest.model_copy(),  # CC 用 manifest.clone()
            prompt=prompt,
            allowed_tools=set(tools),
        )
        try:
            self._spawn_fn(job)
        except Exception as exc:
            # 步骤 8: spawn 失败 → 标记 failed (lib.rs:1415-1418)
            error_msg = f"failed to spawn sub-agent: {exc}"
            self._persist_terminal_state(manifest, "failed", error=error_msg)
            raise RuntimeError(error_msg) from exc

        return manifest

    # --------------------------------------------------------
    # get_status — 读取 manifest JSON 查看 agent 状态
    # --------------------------------------------------------

    def get_status(self, agent_id: str) -> AgentManifest:
        """读 manifest JSON，返回 AgentManifest。"""
        manifest_file = self._store_dir / f"{agent_id}.json"
        if not manifest_file.exists():
            raise FileNotFoundError(f"agent manifest not found: {agent_id}")
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        return AgentManifest.model_validate(data)

    # --------------------------------------------------------
    # list_agents — 列出所有 agent
    # --------------------------------------------------------

    def list_agents(self) -> list[AgentManifest]:
        """列出 store_dir 下所有 agent manifest。"""
        if not self._store_dir.exists():
            return []
        manifests: list[AgentManifest] = []
        for path in sorted(self._store_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                manifests.append(AgentManifest.model_validate(data))
            except Exception:
                continue  # 跳过损坏的 manifest
        return manifests

    # --------------------------------------------------------
    # complete_agent / fail_agent — 标记终态
    # 源码: lib.rs:1599-1614 (persist_agent_terminal_state)
    #
    # 终态操作做两件事:
    # 1. 追加结果到输出文件 (.md)
    # 2. 更新 manifest (.json) 的 status/completed_at/error
    # --------------------------------------------------------

    def complete_agent(self, agent_id: str, result: str) -> None:
        """标记 agent 完成，追加结果到输出文件。"""
        manifest = self.get_status(agent_id)
        self._persist_terminal_state(manifest, "completed", result=result)

    def fail_agent(self, agent_id: str, error: str) -> None:
        """标记 agent 失败，记录错误信息。"""
        manifest = self.get_status(agent_id)
        self._persist_terminal_state(manifest, "failed", error=error)

    # --------------------------------------------------------
    # 内部方法
    # --------------------------------------------------------

    def _write_manifest(self, manifest: AgentManifest) -> None:
        """写 manifest JSON 到文件。源码: lib.rs:1591-1597"""
        path = Path(manifest.manifest_file)
        path.write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def _persist_terminal_state(
        self,
        manifest: AgentManifest,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """更新 manifest 终态 + 追加输出文件。源码: lib.rs:1599-1614

        CC 的做法:
        1. 先追加输出文件 (结果或错误信息)
        2. 再更新 manifest (status + completed_at + error)

        注意: 不是直接修改传入的 manifest (不可变原则)，
        而是创建新的 manifest 写入文件。
        """
        # 追加到输出文件 (lib.rs:1616-1625 append_agent_output)
        output_path = Path(manifest.output_file)
        suffix = _format_terminal_output(status, result, error)
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(suffix)

        # 创建更新后的 manifest (不可变: model_copy 创建新对象)
        updated = manifest.model_copy(update={
            "status": status,
            "completed_at": _iso8601_now(),
            "error": error,
        })
        self._write_manifest(updated)

    # --------------------------------------------------------
    # 默认 spawn_fn — threading.Thread + 异常防护
    # 源码: lib.rs:1424-1449 (spawn_agent_job)
    #
    # CC 的实现:
    # 1. std::thread::Builder::new().name(thread_name).spawn(...)
    # 2. 线程内用 catch_unwind 捕获 panic
    # 3. 正常完成 → persist completed
    # 4. 返回 Err → persist failed + error message
    # 5. panic → persist failed + "thread panicked"
    #
    # Python 版本:
    # - threading.Thread(target=..., daemon=True)
    # - try/except Exception 替代 catch_unwind
    # - daemon=True 这样主进程退出时线程自动结束
    # --------------------------------------------------------

    def _default_spawn(self, job: AgentJob) -> None:
        """默认 spawn: 启动后台线程运行 agent。源码: lib.rs:1424-1449"""

        def _worker() -> None:
            try:
                # 这里是 agent 实际执行的地方
                # CC 的 run_agent_job (lib.rs:1451-1458) 会:
                # 1. build_agent_runtime → 创建独立的 ConversationRuntime
                # 2. runtime.run_turn(prompt) → 执行对话循环
                # 3. 提取最终文本 → persist completed
                #
                # 我们的 mini 版目前没有完整 runtime，
                # 所以这里只是占位 — 实际集成时替换为 runtime 调用。
                #
                # 重要: 即使是占位，错误处理框架已经完整:
                # - 正常完成 → complete_agent
                # - 异常 → fail_agent
                pass  # TODO: 集成 runtime 后替换
            except Exception as exc:
                # 对应 CC 的 Ok(Err(error)) 分支 (lib.rs:1433-1436)
                try:
                    self._persist_terminal_state(
                        job.manifest, "failed", error=str(exc),
                    )
                except Exception:
                    pass  # manifest 写入失败也不能 crash 线程

        thread = threading.Thread(
            target=_worker,
            name=f"agent-{job.manifest.agent_id}",
            daemon=True,
        )
        thread.start()


# ============================================================
# 辅助: 格式化终态输出
# 源码: lib.rs:1627-1636 (format_agent_terminal_output)
# ============================================================

def _format_terminal_output(
    status: str,
    result: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    """源码: lib.rs:1627-1636"""
    sections = [f"\n## Result\n\n- status: {status}\n"]
    if result and result.strip():
        sections.append(f"\n### Final response\n\n{result.strip()}\n")
    if error and error.strip():
        sections.append(f"\n### Error\n\n{error.strip()}\n")
    return "".join(sections)
