"""
Tutorial 09: Config System — 多层配置加载与合并
================================================

Claude Code 的配置来自多个地方，按优先级从低到高：
  1. 用户全局配置 (~/.claude.json, ~/.claude/settings.json)
  2. 项目配置 (.claude.json, .claude/settings.json)
  3. 本地配置 (.claude/settings.local.json)

就像穿衣服：
  - 全局配置是"校服"（所有项目都穿）
  - 项目配置是"工服"（只在这个项目穿，覆盖校服）
  - 本地配置是"个人装饰"（只在你的电脑上生效，不提交到 git）

如果同一个设置在多层都有定义，优先级高的覆盖低的。

本教程会教你：
1. 配置文件在哪里，怎么发现
2. 多层配置怎么合并
3. 配置里有哪些东西（hooks、MCP、权限等）
4. "深度合并"是什么意思

对应源码：rust/crates/runtime/src/config.rs

运行方式：python tutorials/09_config_system.py
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any
from enum import Enum


# ============================================================
# 第一步：配置来源（三层优先级）
# ============================================================

class ConfigSource(Enum):
    """
    配置来自哪里。

    优先级：User < Project < Local
    后面的会覆盖前面的。

    对应源码: config.rs:11-16
    """
    USER = "user"         # 用户全局 (~/.claude/settings.json)
    PROJECT = "project"   # 项目级别 (.claude/settings.json)
    LOCAL = "local"       # 本地个人 (.claude/settings.local.json)


@dataclass(frozen=True)
class ConfigEntry:
    """一个配置文件的位置和来源"""
    source: ConfigSource
    path: str


# ============================================================
# 第二步：ConfigLoader — 发现和加载配置
# ============================================================

class ConfigLoader:
    """
    配置加载器。

    职责：
    1. 发现所有可能的配置文件路径
    2. 读取存在的配置文件
    3. 按优先级合并它们

    对应源码: config.rs:159-247
    """

    def __init__(self, cwd: str, config_home: str):
        """
        参数:
            cwd: 当前工作目录（项目根目录）
            config_home: 用户配置目录（通常是 ~/.claude）
        """
        self.cwd = cwd
        self.config_home = config_home

    @classmethod
    def default_for(cls, cwd: str) -> "ConfigLoader":
        """使用默认路径创建加载器"""
        # 配置目录的查找顺序：
        # 1. 环境变量 CLAUDE_CONFIG_HOME
        # 2. ~/.claude
        config_home = os.environ.get("CLAUDE_CONFIG_HOME")
        if not config_home:
            home = os.path.expanduser("~")
            config_home = os.path.join(home, ".claude")
        return cls(cwd, config_home)

    def discover(self) -> list[ConfigEntry]:
        """
        发现所有可能的配置文件路径。

        注意：返回的是所有"可能"的路径，不管文件是否存在。
        实际加载时会跳过不存在的文件。

        对应源码: config.rs:185-212
        """
        # ~/.claude.json（旧版路径，向后兼容）
        user_legacy = os.path.join(os.path.dirname(self.config_home), ".claude.json")

        return [
            # 用户全局配置（两个位置）
            ConfigEntry(ConfigSource.USER, user_legacy),
            ConfigEntry(ConfigSource.USER, os.path.join(self.config_home, "settings.json")),
            # 项目配置（两个位置）
            ConfigEntry(ConfigSource.PROJECT, os.path.join(self.cwd, ".claude.json")),
            ConfigEntry(ConfigSource.PROJECT, os.path.join(self.cwd, ".claude", "settings.json")),
            # 本地配置（一个位置）
            ConfigEntry(ConfigSource.LOCAL, os.path.join(self.cwd, ".claude", "settings.local.json")),
        ]

    def load(self) -> "RuntimeConfig":
        """
        加载并合并所有配置。

        算法：
        1. 遍历所有可能的配置文件路径
        2. 跳过不存在的文件
        3. 读取存在的文件（JSON）
        4. 按优先级深度合并

        对应源码: config.rs:214-247
        """
        merged: dict[str, Any] = {}
        loaded_entries: list[ConfigEntry] = []

        for entry in self.discover():
            # 尝试读取文件
            content = self._read_json(entry.path)
            if content is None:
                continue  # 文件不存在或不是有效 JSON，跳过

            # 深度合并
            deep_merge(merged, content)
            loaded_entries.append(entry)

        # 从合并后的字典中解析出具体的配置项
        return RuntimeConfig(
            merged=merged,
            loaded_entries=loaded_entries,
            hooks=self._parse_hooks(merged),
            model=merged.get("model"),
            permission_mode=merged.get("permissionMode"),
        )

    @staticmethod
    def _read_json(path: str) -> Optional[dict]:
        """读取 JSON 文件，不存在返回 None"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            pass
        return None

    @staticmethod
    def _parse_hooks(merged: dict) -> dict[str, list[str]]:
        """从配置中解析 hooks"""
        hooks_raw = merged.get("hooks", {})
        result = {"pre_tool_use": [], "post_tool_use": []}

        for hook_def in hooks_raw.get("PreToolUse", []):
            if isinstance(hook_def, dict) and "command" in hook_def:
                result["pre_tool_use"].append(hook_def["command"])
            elif isinstance(hook_def, str):
                result["pre_tool_use"].append(hook_def)

        for hook_def in hooks_raw.get("PostToolUse", []):
            if isinstance(hook_def, dict) and "command" in hook_def:
                result["post_tool_use"].append(hook_def["command"])
            elif isinstance(hook_def, str):
                result["post_tool_use"].append(hook_def)

        return result


# ============================================================
# 第三步：深度合并（Deep Merge）
# ============================================================
# 什么是深度合并？
#
# 简单合并（浅合并）：
#   a = {"name": "Alice", "settings": {"theme": "dark"}}
#   b = {"name": "Bob",   "settings": {"font": "14px"}}
#   result = {**a, **b}
#   → {"name": "Bob", "settings": {"font": "14px"}}
#   注意 settings.theme 丢了！因为 b 的 settings 整个覆盖了 a 的
#
# 深度合并：
#   result = deep_merge(a, b)
#   → {"name": "Bob", "settings": {"theme": "dark", "font": "14px"}}
#   settings 里的字段被分别合并了，theme 没有丢！
#
# Claude Code 用深度合并，这样项目配置可以只写需要覆盖的部分，
# 其他部分继承自全局配置。

def deep_merge(base: dict, override: dict) -> dict:
    """
    深度合并两个字典。override 中的值覆盖 base 中的值。
    如果两边都是字典，递归合并。

    对应源码: config.rs 中的 deep_merge_objects 函数
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            # 两边都是字典 → 递归合并
            deep_merge(base[key], value)
        else:
            # 否则直接覆盖
            base[key] = value
    return base


# ============================================================
# 第四步：RuntimeConfig — 最终的配置对象
# ============================================================

@dataclass
class RuntimeConfig:
    """
    运行时配置 —— 合并后的最终配置。

    对应源码: config.rs:31-36
    """
    merged: dict[str, Any]                    # 合并后的完整字典
    loaded_entries: list[ConfigEntry]          # 实际加载了哪些文件
    hooks: dict[str, list[str]]               # 解析出的 hook 命令
    model: Optional[str] = None               # 使用的模型
    permission_mode: Optional[str] = None     # 权限模式

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        return self.merged.get(key, default)

    def summary(self) -> str:
        """输出配置摘要"""
        lines = [f"加载了 {len(self.loaded_entries)} 个配置文件:"]
        for entry in self.loaded_entries:
            lines.append(f"  [{entry.source.value}] {entry.path}")
        if self.model:
            lines.append(f"模型: {self.model}")
        if self.permission_mode:
            lines.append(f"权限模式: {self.permission_mode}")
        if self.hooks["pre_tool_use"] or self.hooks["post_tool_use"]:
            lines.append(f"Hooks: pre={len(self.hooks['pre_tool_use'])}, post={len(self.hooks['post_tool_use'])}")
        return "\n".join(lines)


# ============================================================
# 第五步：演示
# ============================================================

def main():
    print("=" * 60)
    print("Tutorial 09: Config System 配置系统演示")
    print("=" * 60)

    # --- 创建临时目录模拟项目结构 ---
    tmp_root = tempfile.mkdtemp(prefix="tutorial_config_")
    project_dir = os.path.join(tmp_root, "my-project")
    claude_dir = os.path.join(project_dir, ".claude")
    home_claude = os.path.join(tmp_root, "fake_home", ".claude")
    os.makedirs(claude_dir)
    os.makedirs(home_claude)

    # --- 1. 写入全局用户配置 ---
    user_config = {
        "model": "claude-sonnet-4-20250514",
        "permissionMode": "workspace-write",
        "theme": "dark",
        "hooks": {
            "PostToolUse": [
                {"command": "printf 'global post hook'"}
            ]
        }
    }
    with open(os.path.join(home_claude, "settings.json"), "w") as f:
        json.dump(user_config, f, indent=2)

    # --- 2. 写入项目配置 ---
    project_config = {
        "model": "claude-opus-4-6",  # 覆盖全局的 model
        "hooks": {
            "PreToolUse": [
                {"command": "printf 'project pre hook'"}
            ]
        },
        "projectRules": "Follow PEP 8 style guide"
    }
    with open(os.path.join(claude_dir, "settings.json"), "w") as f:
        json.dump(project_config, f, indent=2)

    # --- 3. 写入本地配置 ---
    local_config = {
        "theme": "light",  # 覆盖全局的 theme
        "debugMode": True,
    }
    with open(os.path.join(claude_dir, "settings.local.json"), "w") as f:
        json.dump(local_config, f, indent=2)

    # --- 4. 加载配置 ---
    print("\n--- 配置文件发现 ---")
    loader = ConfigLoader(project_dir, home_claude)

    for entry in loader.discover():
        exists = os.path.exists(entry.path)
        filename = os.path.basename(entry.path)
        marker = " [EXISTS]" if exists else ""
        print(f"  [{entry.source.value:7s}] {filename}{marker}")

    print("\n--- 加载并合并 ---")
    config = loader.load()
    print(config.summary())

    # --- 5. 查看合并结果 ---
    print("\n--- 合并后的完整配置 ---")
    print(json.dumps(config.merged, indent=2, ensure_ascii=False))

    # --- 6. 验证覆盖关系 ---
    print("\n--- 覆盖关系验证 ---")
    print(f"  model: {config.get('model')}")
    print(f"    (全局是 sonnet，项目覆盖为 opus)")
    print(f"  theme: {config.get('theme')}")
    print(f"    (全局是 dark，本地覆盖为 light)")
    print(f"  permissionMode: {config.get('permissionMode')}")
    print(f"    (全局设置了，项目和本地没覆盖，所以保留)")
    print(f"  projectRules: {config.get('projectRules')}")
    print(f"    (只在项目级别定义)")
    print(f"  debugMode: {config.get('debugMode')}")
    print(f"    (只在本地级别定义)")

    # --- 7. 深度合并演示 ---
    print("\n--- 深度合并 vs 浅合并 ---")
    a = {"settings": {"theme": "dark", "font": "12px"}, "name": "Alice"}
    b = {"settings": {"theme": "light"}, "name": "Bob"}

    shallow = {**a, **b}
    print(f"  浅合并: {shallow}")
    print(f"    注意: settings.font 丢了！")

    deep = deep_merge(
        {"settings": {"theme": "dark", "font": "12px"}, "name": "Alice"},
        {"settings": {"theme": "light"}, "name": "Bob"},
    )
    print(f"  深度合并: {deep}")
    print(f"    settings.font 保留了，theme 被覆盖了")

    # --- 8. Hooks 解析结果 ---
    print("\n--- Hooks 解析结果 ---")
    print(f"  PreToolUse hooks: {config.hooks['pre_tool_use']}")
    print(f"  PostToolUse hooks: {config.hooks['post_tool_use']}")
    print("  注意: hooks 来自两层配置的深度合并")

    # 清理
    import shutil
    shutil.rmtree(tmp_root)

    # 解说
    print("\n" + "=" * 60)
    print("关键理解要点：")
    print("=" * 60)
    print("""
    1. 配置文件的三个层级（优先级从低到高）:
       User (全局)  → ~/.claude.json, ~/.claude/settings.json
       Project (项目) → .claude.json, .claude/settings.json
       Local (本地)   → .claude/settings.local.json

    2. 深度合并 vs 浅合并:
       浅合并: 整个字段被覆盖（嵌套的字典会丢失子字段）
       深度合并: 递归合并嵌套字典（只覆盖有变化的子字段）
       Claude Code 用深度合并，这样你只需要在项目配置里
       写你要覆盖的部分，其他继承全局配置

    3. settings.local.json 不应提交到 git:
       它是个人偏好（像字体大小、调试开关），不影响其他人

    4. 配置中的关键字段:
       - model: 使用的 AI 模型
       - permissionMode: 权限模式
       - hooks: PreToolUse / PostToolUse / Stop 钩子
       - mcpServers: MCP 外部工具服务器
       - allowedTools: 允许自动执行的工具

    5. ConfigLoader 的工作流:
       discover() → 列出所有可能的路径
       load()     → 读取存在的文件 → 深度合并 → 解析特定字段 → RuntimeConfig

    对应 Claude Code 源码:
    - config.rs:11-16   →  ConfigSource 枚举
    - config.rs:31-46   →  RuntimeConfig, RuntimeFeatureConfig
    - config.rs:159-247 →  ConfigLoader (discover + load)
    - config.rs deep_merge_objects →  深度合并
    """)


if __name__ == "__main__":
    main()
