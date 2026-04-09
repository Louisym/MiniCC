# 13 - CLI 入口 (main.py)

## 问题背景

前面 12 个模块各负其责。但用户怎么启动这个 agent？怎么输入？怎么看输出？怎么切换模型？怎么手动压缩？

main.py 是最后的"胶水层"，把所有模块组装成一个可运行的 CLI 工具。

## CC 的做法

源码: `rust/crates/rusty-claude-cli/src/main.rs` (3100+ 行)

### 1. 启动流程 (main.rs:53-93)

```
main() → run() → parse_args()
               ↓
  ┌─── CliAction::Repl    → run_repl()     ← 交互模式
  ├─── CliAction::Prompt   → run_turn()     ← 单次 prompt
  ├─── CliAction::Help     → print_help()
  └─── CliAction::Version  → print_version()
```

### 2. LiveCli — 有状态的 CLI 会话 (main.rs:989-1026)

```rust
struct LiveCli {
    model: String,
    permission_mode: PermissionMode,
    system_prompt: Vec<String>,
    runtime: ConversationRuntime<...>,
    session: SessionHandle,
}
```

LiveCli 持有完整的 runtime 和 session。它负责:
- 构建 runtime（组装所有模块）
- 运行 REPL 循环
- 处理 slash commands
- 持久化 session

### 3. REPL 循环 (main.rs:935-973)

```
打印启动 banner
loop {
    读取输入 → 
    空行 → continue
    /exit → persist + break
    /command → handle_repl_command
    普通文本 → run_turn → persist
}
```

### 4. CliPermissionPrompter — 终端权限询问 (main.rs:2338-2382)

当权限不足时，在终端打印详细信息并等待 y/N 输入：

```
Permission approval required
  Tool             bash
  Current mode     workspace-write
  Required mode    danger-full-access
  Input            {"command":"rm -rf /"}
Approve this tool call? [y/N]:
```

### 5. CliToolExecutor — 工具执行 + 输出渲染 (main.rs:3027-3077)

包装 `execute_tool`，额外功能:
- 检查 allowed_tools 白名单
- 执行成功/失败都渲染输出到终端

### 6. build_runtime — 组装 (main.rs:2318-2336)

```rust
ConversationRuntime::new_with_features(
    session,
    AnthropicRuntimeClient::new(model, ...),
    CliToolExecutor::new(allowed_tools, ...),
    permission_policy(permission_mode),
    system_prompt,
    feature_config,
)
```

一行代码把 API 客户端、工具执行器、权限策略、系统提示词、特性配置全部注入。

## 你要练习的工程模式

| 模式 | 说明 | 源码位置 |
|------|------|---------|
| **组装点** | main 是唯一知道所有模块的地方，其他模块互不导入 | main.rs:2318-2336 |
| **CliAction 分派** | 枚举 + match，不用 if-elif 链 | main.rs:64-93 |
| **REPL 循环** | 持续读输入 + 分派处理 | main.rs:935-973 |
| **Slash command** | /help, /status, /compact 等内置命令 | main.rs:1140-1225 |
| **CliPermissionPrompter** | Protocol 实现: 终端交互式权限询问 | main.rs:2338-2382 |
| **CliToolExecutor** | Protocol 实现: 包装 ToolRegistry + 输出渲染 | main.rs:3027-3077 |
| **Session 持久化** | 每次 turn 后自动保存 | main.rs:1227-1230 |

## 你需要写的东西

```python
# --- Slash command 解析 ---
SlashCommand(Enum)
  HELP, STATUS, COMPACT, EXIT, UNKNOWN

parse_slash_command(input: str) -> Optional[SlashCommand]

# --- CLI 权限询问 ---
CliPermissionPrompter
  decide(request: PermissionRequest) -> PermissionResult
  # 打印详情 → input("Approve? [y/N]") → Allow/Deny

# --- CLI 工具执行 ---
CliToolExecutor
  __init__(registry: ToolRegistry)
  execute(tool_name: str, input: str) -> str
  # 委托 registry.execute(), 打印执行过程

# --- 组装 runtime ---
build_runtime(session, api_client, registry, permission_mode, system_prompt, hooks_config)
    -> ConversationRuntime

# --- REPL ---
run_repl(model, permission_mode)
  构建 runtime → 打印 banner → loop { 读输入 → 分派 }

# --- 入口 ---
main()
  解析 sys.argv → Repl / Prompt / Help / Version
```

## 易错点

- CliPermissionPrompter 必须实现 `decide(request) -> PermissionResult`，签名要和 Protocol 匹配
- REPL 循环里 Ctrl+C 不应该退出程序（捕获 KeyboardInterrupt）
- Ctrl+D (EOF) 应该退出
- 空输入跳过，不调 API
- slash command 以 `/` 开头，和普通 prompt 区分
- 每次 `run_turn` 后保存 session（崩溃恢复）
- `build_runtime` 是唯一的组装点 — 其他模块不应该互相导入
