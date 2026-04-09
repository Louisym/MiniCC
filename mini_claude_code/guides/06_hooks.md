# 06 - Hook 系统 (hooks.py)

## 问题背景

权限系统是第一层防御，Hook 是第二层。Hook 是用户自定义的 shell 命令，在工具执行**前/后**自动运行。

场景举例：
- PreToolUse: "每次执行 Bash 前，检查命令里有没有 `rm -rf`"
- PostToolUse: "每次编辑 .ts 文件后，自动跑 `prettier` 格式化"

Hook 的强大之处：它是 shell 命令，所以**任何语言**写的脚本都能当 hook。

## CC 的做法

源码: `rust/crates/runtime/src/hooks.rs` (350 行)

CC 的 Hook 系统有三个关键设计：

### 1. 退出码协议 (Exit Code Protocol)

```
exit 0  →  允许 (stdout 作为反馈附加到 tool result)
exit 2  →  拒绝 (阻止工具执行，stdout 作为拒绝原因)
exit 其他 →  警告但继续执行 (stdout/stderr 作为警告信息)
```

为什么是 2 而不是 1？因为 exit 1 太常见了（很多程序出错就返回 1），
用 2 可以区分"hook 本身报错了"和"hook 故意拒绝"。

### 2. 双通道输入 (Dual-Channel Input)

Hook 脚本可以通过**两种方式**获取上下文：
- **环境变量**: `HOOK_EVENT`, `HOOK_TOOL_NAME`, `HOOK_TOOL_INPUT` 等
- **stdin JSON**: 完整的上下文 payload 通过 stdin 管道传入

为什么两种？简单脚本用环境变量就够了 (`echo $HOOK_TOOL_NAME`)，
复杂脚本用 stdin JSON 获取完整上下文 (`jq .tool_name`)。

### 3. 顺序执行 + 熔断 (Sequential + Short-Circuit)

多个 hook 按顺序执行。如果某个 hook 返回 deny (exit 2)，
**立即停止**，后面的 hook 不再执行。这叫"熔断"。

## 你要练习的工程模式

| 模式 | 说明 | 源码位置 |
|------|------|---------|
| **退出码协议** | 0=允许, 2=拒绝, 其他=警告 | hooks.rs:179-196 |
| **双通道传参** | 环境变量 + stdin JSON 管道 | hooks.rs:166-174 |
| **CommandWithStdin** | 自定义子进程 builder，注入 stdin | hooks.rs:249-290 |
| **短路熔断** | deny 时立即停止后续 hook | hooks.rs:135-143 |
| **三元结果** | Allow/Deny/Warn 三种结局 | hooks.rs:208-212 |

## 你需要写的东西

```python
HookEvent(Enum)       — PRE_TOOL_USE / POST_TOOL_USE

HookResult(BaseModel) — denied: bool, messages: list[str]
  静态工厂: HookResult.allow(messages), HookResult.denied(messages)

HookRunner
  __init__(pre_tool_use: list[str], post_tool_use: list[str])
  from_config(config: RuntimeConfig) -> HookRunner  # 从配置加载
  run_pre_tool_use(tool_name, tool_input) -> HookResult
  run_post_tool_use(tool_name, tool_input, tool_output, is_error) -> HookResult

  _run_commands(event, commands, tool_name, tool_input,
                tool_output, is_error) -> HookResult
    # 核心: 遍历命令，执行，按退出码分类，deny 时熔断

  _run_command(...) -> 单个命令的结果 (allow/deny/warn)
    # 构建子进程: sh -lc <command>
    # 设环境变量 + stdin JSON
    # 按退出码分类返回
```

## stdin JSON payload 结构

```json
{
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": {"command": "ls -la"},
  "tool_input_json": "{\"command\": \"ls -la\"}",
  "tool_output": null,
  "tool_result_is_error": false
}
```

注意 `tool_input` 是解析后的对象，`tool_input_json` 是原始字符串。
源码: hooks.rs:108-116

## 环境变量列表

| 变量名 | 含义 |
|--------|------|
| `HOOK_EVENT` | 事件类型: "PreToolUse" / "PostToolUse" |
| `HOOK_TOOL_NAME` | 工具名称 |
| `HOOK_TOOL_INPUT` | 工具输入 (JSON 字符串) |
| `HOOK_TOOL_OUTPUT` | 工具输出 (仅 PostToolUse) |
| `HOOK_TOOL_IS_ERROR` | "1" 或 "0" |

源码: hooks.rs:166-172

## 易错点

- 退出码是 **2** 表示拒绝，不是 1（1 是普通错误，归类为"警告"）
- hook 命令不存在时应该是 Warn，不是崩溃（hooks.rs:198-204）
- deny 时如果 stdout 为空，要用默认消息 "hook denied tool xxx"
- stdin 写入后要关闭（Python 的 `communicate()` 自动处理）
- 环境变量的值必须是字符串（bool 转 "1"/"0"）
