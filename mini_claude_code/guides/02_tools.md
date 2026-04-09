# 02 - 工具系统 (tools.py)

## 问题背景

Agent 需要执行工具（读文件、跑命令等），但 agent loop 不应该关心具体有哪些工具。怎么让工具可以动态注册、统一执行？

## CC 的做法

CC 定义了一个工具注册表，通过名字查找并执行工具。每个工具是一个函数，输入 JSON 字符串，输出字符串结果。Agent loop 只需调用 `execute(name, input)`。
源码: `rust/crates/tools/src/lib.rs`

## 你要练习的工程模式

| 模式 | 说明 |
|------|------|
| **注册表模式 (Registry)** | `dict[str, Callable]`，按名字查找处理函数，像电话簿 |
| **链式调用 (Builder)** | `register()` 返回 `self`，可以 `.register(A).register(B)` |
| **依赖注入 (DI)** | 工具在外部注册，不在 registry 内部硬编码 |
| **统一接口** | 所有工具签名相同: `(input_json: str) -> str` |

## 你需要写的东西

```
ToolRegistry
  _handlers: dict[str, Callable]
  register(name, handler) -> self     # 链式调用
  execute(name, tool_input_json) -> str

bash_tool(input_json) -> str    # subprocess 执行命令
read_tool(input_json) -> str    # 读文件
write_tool(input_json) -> str   # 写文件
```

## 易错点

- `register()` 要返回 `self` 才能链式调用
- 不要用 `input` 做变量名（Python 内置函数）
- `bash_tool` 要设 `timeout`，防止死循环命令卡住
- stderr 要和 stdout 一起返回，不能丢掉
- 文件不存在时要 catch `FileNotFoundError`，返回错误信息而不是抛异常
