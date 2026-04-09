# 04 - 配置系统 (config.py)

## 问题背景

一个 CLI 工具的配置来自很多地方：用户全局设置、项目设置、本地个人设置、环境变量。当多个来源对同一个 key 有不同值时，谁赢？

## CC 的做法

CC 按固定顺序扫描 5 个配置文件，用**递归深度合并**逐个叠加，最后一次性解析成强类型。
源码: `rust/crates/runtime/src/config.rs`

## 你要练习的工程模式

| 模式 | 说明 |
|------|------|
| **5 源发现链** | 固定 5 个路径按顺序扫描，后面覆盖前面 (config.rs:185-212) |
| **递归深度合并** | 双方都是 dict 才递归；否则 last-write-wins (config.rs:777-791) |
| **Eager Feature Parsing** | load() 返回前就解析成强类型，不等运行时 (config.rs:230-239) |
| **叶模块** | config.py 不 import 项目内其他模块，防循环依赖 |
| **Legacy 容错** | 旧格式 .claude.json 解析失败时静默跳过 |

## 你需要写的东西

```
ConfigSource(Enum)     — USER / PROJECT / LOCAL
ConfigEntry(BaseModel) — source + path
ConfigError(Exception) — kind: "io" | "parse"

deep_merge(target, source) -> new_dict
  只有双方都是 dict 才递归，否则 source 覆盖

RuntimeFeatureConfig(BaseModel)
  hooks_pre_tool_use, hooks_post_tool_use
  model, permission_mode, timeout, max_iterations, token_budget

RuntimeConfig(BaseModel)
  merged: dict              — 原始合并结果（forward compatibility）
  loaded_entries: list       — 哪些文件被加载了（调试用）
  feature_config             — 解析后的强类型（快速访问）
  便捷方法: get(), model(), timeout(), hooks_pre() 等

ConfigLoader
  __init__(cwd, config_home) — 依赖注入，不硬编码路径
  discover() -> 5 个 ConfigEntry
  load() -> RuntimeConfig
    遍历 → 读 JSON → deep_merge → 环境变量覆盖 → eager parse
```

## deep_merge 示例

```python
a = {"model": "sonnet", "hooks": {"PreToolUse": ["echo hi"]}}
b = {"model": "opus",   "hooks": {"PostToolUse": ["prettier"]}, "timeout": 60}
result = deep_merge(a, b)
# {"model": "opus",                      ← source 覆盖 (都是 str)
#  "hooks": {"PreToolUse": ["echo hi"],   ← 递归合并 (都是 dict)
#            "PostToolUse": ["prettier"]},
#  "timeout": 60}                         ← 新 key
```

## 5 个配置路径（按优先级从低到高）

```
1. ~/.claude.json                        (User - 旧格式)
2. ~/.claude/settings.json               (User)
3. <project>/.claude.json                (Project)
4. <project>/.claude/settings.json       (Project)
5. <project>/.claude/settings.local.json (Local - 不提交 git)
```

## 易错点

- `deep_merge` 要返回新 dict，不要修改原 dict（immutability）
- legacy `.claude.json` 解析失败要静默跳过，不报错
- 空文件应返回空 dict `{}`，不是 None
- 环境变量类型转换要 try/except（比如 `CLAUDE_TIMEOUT=abc` 应该报错）
- permission mode 支持多个别名（如 `"auto"` = `"workspace-write"`）
