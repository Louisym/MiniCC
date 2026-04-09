# Mini Claude Code

用 Python 手写一个生产级 mini agent runtime，忠实还原 [Claude Code](https://claude.ai/code) Rust 源码中的核心工程模式。

**这不是玩具。** 每个模块都对照 CC 真实源码中的工程技巧。目标是学习世界级的生产工程，而不是"能跑就行"。

## 适合谁

- 会 Python，想学真实 agent runtime 的工程设计
- 写过基础 agent 编排，想进阶到生产级
- 想理解每个设计决策背后的 WHY，不只是 HOW

## 使用方法

每个模块在 `guides/` 目录下有对应引导。推荐流程：

1. 读引导（理解问题 & 工程模式）
2. 自己动手写代码
3. 和参考实现对比
4. 进入下一个模块

## 模块依赖顺序

```
models.py       → 消息模型（无依赖）
tools.py        → 工具注册与执行（无依赖）
api_client.py   → API 客户端（依赖 models）
config.py       → 配置系统（叶模块，无内部依赖）
permissions.py  → 权限系统（依赖 config）
hooks.py        → Hook 系统（依赖 permissions）
retry.py        → 重试引擎（依赖 api_client）
prompt.py       → 提示词构建（依赖 config）
compact.py      → 上下文压缩（依赖 models, prompt）
storage.py      → 持久化存储（依赖 models）
multi_agent.py  → 多 agent 协作（依赖 tools, permissions）
runtime.py      → 运行时整合（整合以上所有）
main.py         → CLI 入口
```

## 核心工程模式速查

| 模式 | 模块 | CC 源码 |
|------|------|---------|
| Pydantic + Literal 类型锁定 | models.py | conversation.rs |
| 注册表 + 依赖注入 | tools.py | Tool.ts |
| ABC 接口 + 流式事件分类 | api_client.py | client.rs |
| 5 源发现链 + 递归深度合并 | config.py | config.rs |
| IntEnum 权限层级 + Policy 对象 | permissions.py | permissions.rs |
| 退出码协议 + 双通道 Hook | hooks.py | hooks.rs |
| 错误分类 + 溢出保护退避 | retry.py | error.rs, client.rs |
| 静态/动态边界 + 字符预算 | prompt.py | prompt.rs |
| Token 估算 + 双触发压缩 | compact.py | compact.rs |
| JSONL 追加写入 + UUID 链 | storage.py | session persistence |
| 泛型 spawn_fn + manifest 追踪 | multi_agent.py | lib.rs |
