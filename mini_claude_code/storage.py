"""
storage.py — JSONL 追加写入 + UUID 链 + 崩溃恢复

忠实还原 Claude Code 的会话持久化系统。
源码对照:
  - rust/crates/runtime/src/session.rs — Session 序列化/反序列化
  - utils/sessionStorage.ts (TS层) — JSONL 追加 + UUID 链 + 恢复

核心工程要点:
1. JSONL 追加写入: 只 append 不 rewrite，崩溃安全
2. UUID 链: parent_uuid 链表，支持分叉和回溯
3. 中断检测: 根据最后消息的 role 判断中断类型
4. 懒物化: 第一条消息时才创建文件
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from mini_claude_code.models import Message


# ============================================================
# StorageEntry — JSONL 中的一行
#
# 每行是一个独立 JSON 对象，包含:
# - uuid/parent_uuid: 形成链表
# - message: 序列化的 Message 对象
# - timestamp: UTC ISO format
#
# CC 的 TS 层把 role/content 等直接平铺在 entry 中。
# 我们用嵌套的 message 字段，保持和 models.py 的一致性。
# ============================================================

class StorageEntry(BaseModel):
    uuid: str
    parent_uuid: Optional[str] = None
    message: dict  # Message.model_dump() 的结果
    timestamp: str = ""


# ============================================================
# SessionStore — 会话存储管理器
#
# 职责:
# - 按 session_id 管理 JSONL 文件
# - 追加消息 (append)
# - 恢复完整对话链 (UUID 链回溯)
# - 列出所有会话
# - 检测中断类型
# ============================================================

class SessionStore:
    def __init__(self, storage_dir: Path):
        """
        参数:
            storage_dir: 存储目录，如 ~/.claude/projects/<sanitized-cwd>/

        CC 用 sanitizePath() 把 cwd 转成合法文件名。
        我们简化为直接接收 storage_dir。
        """
        self._storage_dir = storage_dir

    def _session_path(self, session_id: str) -> Path:
        """JSONL 文件路径。CC: ~/.claude/projects/{cwd}/{session_id}.jsonl"""
        return self._storage_dir / f"{session_id}.jsonl"

    def save_message(
        self,
        session_id: str,
        message: Message,
        parent_uuid: Optional[str] = None,
    ) -> str:
        """追加一条消息到 JSONL 文件。返回生成的 uuid。

        CC 的两层设计:
        - TS 层用 async queue + 100ms 合并 (高性能)
        - Rust 层用 save_to_path (整体写入)

        我们用简单的同步 append — 保持 JSONL 追加语义。
        """
        path = self._session_path(session_id)

        entry = StorageEntry(
            uuid=str(uuid.uuid4()),
            parent_uuid=parent_uuid,
            message=message.model_dump(),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        self._append_entry(path, entry)
        return entry.uuid

    def load_session(self, session_id: str) -> list[Message]:
        """从 JSONL 恢复完整对话。

        流程:
        1. 逐行解析 JSONL
        2. 构建 UUID → entry 映射
        3. 找到叶节点（没有被任何 entry 的 parent_uuid 引用的 entry）
        4. 从叶节点沿 parent_uuid 回溯
        5. 反转得到时间序
        """
        path = self._session_path(session_id)
        entries = self._read_entries(path)
        if not entries:
            return []

        chain = self._rebuild_chain(entries)
        return [Message.model_validate(e.message) for e in chain]

    def list_sessions(self) -> list[str]:
        """列出所有会话 ID。"""
        if not self._storage_dir.exists():
            return []
        return sorted(
            p.stem for p in self._storage_dir.glob("*.jsonl")
        )

    def detect_interruption(self, session_id: str) -> Optional[str]:
        """检测中断类型。

        CC 的恢复逻辑: 根据最后一条消息的 role 判断:
        - "user" → 用户发了消息但 AI 没回复
        - "tool" → 工具执行完但 AI 没继续
        - "assistant" → 正常结束，无中断
        - None → 空会话
        """
        path = self._session_path(session_id)
        entries = self._read_entries(path)
        if not entries:
            return None

        chain = self._rebuild_chain(entries)
        if not chain:
            return None

        last_role = chain[-1].message.get("role", "")
        if last_role in ("user", "tool"):
            return last_role
        return None

    def _append_entry(self, path: Path, entry: StorageEntry) -> None:
        """追加一行 JSONL。

        JSONL 核心: 每行一个 JSON + 换行符。
        用 'a' 模式 (append)，不修改已有内容。
        懒物化: 目录不存在时自动创建。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")

    @staticmethod
    def _read_entries(path: Path) -> list[StorageEntry]:
        """逐行解析 JSONL。

        崩溃安全: 跳过空行和解析失败的行。
        CC 的保证: 最多丢失最后一行（写入被中断的行）。
        """
        if not path.exists():
            return []

        entries: list[StorageEntry] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(StorageEntry.model_validate_json(line))
                except Exception:
                    continue  # 跳过损坏的行
        return entries

    @staticmethod
    def _rebuild_chain(entries: list[StorageEntry]) -> list[StorageEntry]:
        """从 UUID 链重建时间序对话。

        算法:
        1. 构建 uuid → entry 映射
        2. 找到所有 uuid 的集合
        3. 找到被引用的 parent_uuid 集合
        4. 叶节点 = uuid集合 - 被引用集合（没有子节点的 entry）
        5. 取最后一个叶节点（最新的分支）
        6. 从叶节点沿 parent_uuid 回溯
        7. 反转得到时间序

        CC 的 TS 层用 64KB 头尾窗口快速扫描叶节点。
        我们简化为全量扫描（mini 版不需要优化到那个程度）。
        """
        if not entries:
            return []

        uuid_map: dict[str, StorageEntry] = {e.uuid: e for e in entries}

        # 找叶节点: 没有被任何 entry 作为 parent 引用的 uuid
        referenced_as_parent: set[str] = set()
        for e in entries:
            if e.parent_uuid:
                referenced_as_parent.add(e.parent_uuid)

        all_uuids = set(uuid_map.keys())
        leaf_uuids = all_uuids - referenced_as_parent

        if not leaf_uuids:
            # 循环引用或异常情况 → 返回最后一个 entry
            return [entries[-1]]

        # 取最后出现的叶节点（最新的分支）
        leaf_uuid = None
        for e in reversed(entries):
            if e.uuid in leaf_uuids:
                leaf_uuid = e.uuid
                break

        # 从叶节点回溯
        chain: list[StorageEntry] = []
        cursor: Optional[str] = leaf_uuid
        visited: set[str] = set()  # 防循环
        while cursor and cursor in uuid_map and cursor not in visited:
            visited.add(cursor)
            entry = uuid_map[cursor]
            chain.append(entry)
            cursor = entry.parent_uuid

        chain.reverse()
        return chain
