"""
config.py — 多层配置加载与递归深度合并

忠实还原 Claude Code 的配置系统工程模式。
源码对照: rust/crates/runtime/src/config.rs

三大核心工程要点:
1. 5-source discovery chain (config.rs:185-212)
   固定顺序扫描 5 个路径，后面覆盖前面
2. Recursive deep merge (config.rs:777-791)
   双方都是 dict 才递归；否则 last-write-wins
3. Eager feature parsing (config.rs:230-239)
   load() 返回前就把 merged dict 解析成强类型，不等运行时
"""

import os
import json
from enum import Enum
from pathlib import Path
from typing import Optional, Any

from pydantic import BaseModel, Field


# ============================================================
# ConfigSource — 配置来源层级
# 源码: config.rs:12-16
# CC 用 Rust 的 derive(Ord) 让枚举可排序。
# Python 的 Enum 没有自动排序，但我们不需要排序——
# 优先级由 discover() 返回的顺序决定（后面的覆盖前面的）。
# ============================================================

class ConfigSource(Enum):
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


# ============================================================
# ConfigEntry — 一个配置文件的位置和来源
# 源码: config.rs:26-29
# CC 用 PathBuf 存路径。Python 用 Path。
# 用 Pydantic 而不是 dataclass，和项目其他模块保持一致。
# ============================================================

class ConfigEntry(BaseModel):
    source: ConfigSource
    path: Path

    # Pydantic v2 需要这个才能放 Path 类型
    model_config = {"arbitrary_types_allowed": True}


# ============================================================
# ConfigError — 配置错误
# 源码: config.rs:136-157
# CC 区分 Io 和 Parse 两种。Python 统一用一个异常类，
# 用 kind 字段区分。
# ============================================================

class ConfigError(Exception):
    def __init__(self, message: str, kind: str = "parse"):
        self.kind = kind  # "io" or "parse"
        super().__init__(message)


# ============================================================
# deep_merge — 递归深度合并
# 源码: config.rs:777-791
#
# 原版是 in-place mutation (&mut BTreeMap)。
# 我们的 coding style 要求 immutability，所以返回新 dict。
# 但核心逻辑完全一致：
#   - 双方都是 dict → 递归合并
#   - 否则 → source 覆盖 target (last-write-wins)
# ============================================================

def deep_merge(target: dict, source: dict) -> dict:
    result = dict(target)
    for key, value in source.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ============================================================
# RuntimeFeatureConfig — 从 merged dict 解析出的强类型配置
# 源码: config.rs:39-46
#
# CC 在 load() 返回之前就一次性解析完所有 feature 字段。
# 好处: 配置格式错误在启动时就暴露，不会等到运行中才崩。
# 这叫 "Eager Parsing" — 和 "Lazy Parsing" (用到才解析) 相反。
# ============================================================

class RuntimeFeatureConfig(BaseModel):
    hooks_pre_tool_use: list[str] = Field(default_factory=list)
    hooks_post_tool_use: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    timeout: int = 30
    max_iterations: int = 10
    token_budget: int = 200_000


def parse_feature_config(merged: dict) -> RuntimeFeatureConfig:
    """从 merged dict 中解析 feature config。

    源码: config.rs:230-239
    CC 里每个字段都有独立的 parse_optional_xxx() 函数。
    我们简化为一个函数，但保持"显式提取+验证"的精神。
    """
    hooks = merged.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ConfigError("hooks: expected JSON object", kind="parse")

    pre = hooks.get("PreToolUse", [])
    post = hooks.get("PostToolUse", [])
    if not isinstance(pre, list) or not isinstance(post, list):
        raise ConfigError("hooks.PreToolUse/PostToolUse: must be arrays", kind="parse")

    # permission_mode: CC 支持多种别名 (config.rs:511-518)
    raw_mode = merged.get("permissionMode")
    permission_mode = None
    if isinstance(raw_mode, str):
        mode_map = {
            "default": "read-only", "plan": "read-only", "read-only": "read-only",
            "acceptEdits": "workspace-write", "auto": "workspace-write",
            "workspace-write": "workspace-write",
            "dontAsk": "danger-full-access", "danger-full-access": "danger-full-access",
        }
        if raw_mode not in mode_map:
            raise ConfigError(f"permissionMode: unsupported mode '{raw_mode}'", kind="parse")
        permission_mode = mode_map[raw_mode]

    return RuntimeFeatureConfig(
        hooks_pre_tool_use=pre,
        hooks_post_tool_use=post,
        model=merged.get("model"),
        permission_mode=permission_mode,
        timeout=merged.get("timeout", 30),
        max_iterations=merged.get("maxIterations", 10),
        token_budget=merged.get("tokenBudget", 200_000),
    )


# ============================================================
# RuntimeConfig — 最终的配置对象
# 源码: config.rs:32-36
#
# CC 保留了 merged (原始 BTreeMap) + loaded_entries (哪些文件被加载了)
# + feature_config (解析后的强类型)。三者都保留是因为：
#   - merged: 允许将来查询任意 key (forward compatibility)
#   - loaded_entries: 调试时能看到"配置从哪来的"
#   - feature_config: 运行时快速访问，不用每次从 dict 取
#
# CC 用 BTreeMap 保证 key 排序（确定性序列化、测试稳定性）。
# Python dict 在 3.7+ 本身就保序（插入顺序），够用了。
# ============================================================

class RuntimeConfig(BaseModel):
    merged: dict = Field(default_factory=dict)
    loaded_entries: list[ConfigEntry] = Field(default_factory=list)
    feature_config: RuntimeFeatureConfig = Field(default_factory=RuntimeFeatureConfig)

    model_config = {"arbitrary_types_allowed": True}

    # --- 便捷访问方法 ---
    # 源码: config.rs:260-312
    # CC 为每个常用字段提供 getter，避免外部直接访问内部结构。
    # 这叫 "封装" — 将来内部结构变了，外部代码不用改。

    def get(self, key: str) -> Optional[Any]:
        return self.merged.get(key)

    def hooks_pre(self) -> list[str]:
        return self.feature_config.hooks_pre_tool_use

    def hooks_post(self) -> list[str]:
        return self.feature_config.hooks_post_tool_use

    def model(self) -> Optional[str]:
        return self.feature_config.model

    def permission_mode(self) -> Optional[str]:
        return self.feature_config.permission_mode

    def timeout(self) -> int:
        return self.feature_config.timeout

    def token_budget(self) -> int:
        return self.feature_config.token_budget

    @staticmethod
    def empty() -> "RuntimeConfig":
        """空配置 — 用于测试或默认场景。源码: config.rs:251-257"""
        return RuntimeConfig()


# ============================================================
# ConfigLoader — 发现、读取、合并配置
# 源码: config.rs:159-247
#
# 核心设计:
# - __init__ 只接收 cwd 和 config_home（依赖注入，不硬编码路径）
# - discover() 返回固定的 5 个候选路径
# - load() 按顺序读取存在的文件，deep_merge，然后 eager parse
# ============================================================

class ConfigLoader:
    def __init__(self, cwd: Path, config_home: Path):
        """
        参数:
            cwd: 当前工作目录（项目根目录）
            config_home: 用户配置目录，通常是 ~/.claude

        源码: config.rs:166-172
        CC 也接收 cwd 和 config_home 两个参数，不依赖全局状态。
        """
        self.cwd = cwd
        self.config_home = config_home

    @classmethod
    def default_for(cls, cwd: Path) -> "ConfigLoader":
        """用默认路径创建 loader。源码: config.rs:175-182

        CC 先查 CLAUDE_CONFIG_HOME 环境变量，没有就用 $HOME/.claude。
        """
        config_home = os.environ.get("CLAUDE_CONFIG_HOME")
        if config_home:
            return cls(cwd, Path(config_home))
        home = Path.home()
        return cls(cwd, home / ".claude")

    def discover(self) -> list[ConfigEntry]:
        """发现所有候选配置文件路径。

        源码: config.rs:185-212
        固定返回 5 个路径（不管文件存不存在）。
        顺序决定优先级：后面的覆盖前面的。
        """
        # legacy 路径: config_home 的父目录下的 .claude.json
        # 例: ~/.claude 的父目录是 ~，所以 legacy = ~/.claude.json
        legacy_path = self.config_home.parent / ".claude.json"

        return [
            # --- User 层 (全局) ---
            ConfigEntry(source=ConfigSource.USER, path=legacy_path),
            ConfigEntry(source=ConfigSource.USER, path=self.config_home / "settings.json"),
            # --- Project 层 ---
            ConfigEntry(source=ConfigSource.PROJECT, path=self.cwd / ".claude.json"),
            ConfigEntry(source=ConfigSource.PROJECT, path=self.cwd / ".claude" / "settings.json"),
            # --- Local 层 (不提交 git) ---
            ConfigEntry(source=ConfigSource.LOCAL, path=self.cwd / ".claude" / "settings.local.json"),
        ]

    def load(self) -> RuntimeConfig:
        """加载并合并所有配置。

        源码: config.rs:214-246
        流程:
        1. 遍历 discover() 的 5 个路径
        2. 跳过不存在的文件
        3. 读取 JSON → deep_merge 到 merged
        4. 环境变量覆盖 (CC 源码中由其他模块处理，我们集中在这里)
        5. Eager parse 成 RuntimeFeatureConfig
        6. 返回 RuntimeConfig
        """
        merged: dict = {}
        loaded_entries: list[ConfigEntry] = []

        for entry in self.discover():
            data = self._read_json(entry.path)
            if data is None:
                continue
            merged = deep_merge(merged, data)
            loaded_entries.append(entry)

        # 环境变量最高优先级
        self._apply_env_overrides(merged)

        # ★ Eager Feature Parsing — 加载完立刻解析
        feature_config = parse_feature_config(merged)

        return RuntimeConfig(
            merged=merged,
            loaded_entries=loaded_entries,
            feature_config=feature_config,
        )

    @staticmethod
    def _read_json(path: Path) -> Optional[dict]:
        """读取 JSON 文件，不存在返回 None。

        源码: config.rs:406-435 (read_optional_json_object)
        CC 的额外细节:
        - legacy .claude.json 解析失败时静默跳过（兼容旧格式）
        - 空文件返回空 dict（不是 None）
        - 顶层必须是 object，否则报错
        """
        if not path.exists():
            return None

        is_legacy = path.name == ".claude.json"

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"{path}: {e}", kind="io") from e

        if not text.strip():
            return {}

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            if is_legacy:
                return None  # legacy 格式解析失败时静默跳过
            raise ConfigError(f"{path}: {e}", kind="parse") from e

        if not isinstance(parsed, dict):
            if is_legacy:
                return None
            raise ConfigError(f"{path}: top-level value must be a JSON object", kind="parse")

        return parsed

    @staticmethod
    def _apply_env_overrides(merged: dict) -> None:
        """环境变量覆盖。

        注意: 这里直接修改 merged dict (mutation)。
        这是故意的 — 在 load() 内部的构建阶段允许 mutation，
        对外暴露的 RuntimeConfig 是 frozen 的。
        CC 源码中环境变量由其他模块处理，我们集中在这里简化。
        """
        env_map = {
            "ANTHROPIC_API_KEY": "api_key",
            "CLAUDE_MODEL": "model",
            "CLAUDE_TIMEOUT": ("timeout", int),
            "CLAUDE_MAX_ITERATIONS": ("maxIterations", int),
            "CLAUDE_TOKEN_BUDGET": ("tokenBudget", int),
        }
        for env_key, target in env_map.items():
            value = os.environ.get(env_key)
            if value is None:
                continue
            if isinstance(target, tuple):
                config_key, converter = target
                try:
                    merged[config_key] = converter(value)
                except ValueError:
                    raise ConfigError(
                        f"env {env_key}={value!r}: cannot convert to {converter.__name__}",
                        kind="parse",
                    )
            else:
                merged[target] = value
