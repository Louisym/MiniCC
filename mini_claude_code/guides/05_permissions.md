# 05 - 权限系统 (permissions.py)

## 问题背景

当 AI 想执行 `rm -rf /` 时，谁来拦住它？

简单做法是一个 `if is_dangerous: deny` 判断。但 CC 的权限系统远比这复杂：
- 不同工具需要不同级别的权限（读文件 vs 删文件）
- 用户可以选择不同的权限模式（只读 / 可写 / 全开）
- 权限不足时，有时可以询问用户，有时直接拒绝

## CC 的做法

CC 用 **IntEnum 层级** + **Policy 对象** + **Prompter 回调** 三件套。
源码: `rust/crates/runtime/src/permissions.rs` (仅 233 行，非常精炼)

## 你要练习的工程模式

| 模式 | 说明 | 源码位置 |
|------|------|---------|
| **IntEnum + Ord 比较** | 权限级别是数字，直接用 `>=` 比较 | permissions.rs:3-10 |
| **Policy 对象** | 集中管理"当前模式"和"每个工具需要什么权限" | permissions.rs:50-53 |
| **Builder 模式** | `with_tool_requirement()` 返回 `self`，链式构建 | permissions.rs:65-73 |
| **Trait 回调 (Protocol)** | `PermissionPrompter` 接口解耦"询问用户"和"权限判断" | permissions.rs:39-41 |
| **默认从严** | 未注册的工具默认需要最高权限 `DangerFullAccess` | permissions.rs:82-86 |

## 核心授权逻辑 (authorize)

这是整个权限系统最关键的函数 (permissions.rs:89-134)：

```
authorize(tool_name, input, prompter):

  current = 当前权限模式
  required = 该工具需要的权限

  1. current == Allow → 放行（最危险模式，跳过一切）
  2. current >= required → 放行（权限足够）
  3. current == Prompt → 询问用户（Prompt 模式下一切都问）
  4. current == WorkspaceWrite 且 required == DangerFullAccess → 询问用户
     （最常见场景：普通模式下执行危险命令，弹窗确认）
  5. 其他情况 → 拒绝
```

注意第 4 点：CC **只允许 WorkspaceWrite → DangerFullAccess 的一级升级询问**。
如果是 ReadOnly 想执行 DangerFullAccess 的工具，直接拒绝，不询问。

## 你需要写的东西

```python
PermissionMode(IntEnum)         # 5 个级别: READ_ONLY=1 ... ALLOW=5
PermissionRequest(BaseModel)    # tool_name, input, current_mode, required_mode
PermissionDecision(Enum)        # ALLOW / DENY
PermissionResult(BaseModel)     # decision + reason

# Prompter 接口 — 用 Protocol 不用 ABC
# Protocol 不需要继承，只要有 decide() 方法就行（鸭子类型）
class PermissionPrompter(Protocol):
    def decide(self, request: PermissionRequest) -> PermissionResult: ...

PermissionPolicy
  __init__(active_mode: PermissionMode)
  with_tool_requirement(tool_name, required_mode) -> self  # Builder 链式
  required_mode_for(tool_name) -> PermissionMode           # 默认 DangerFullAccess
  authorize(tool_name, input, prompter=None) -> PermissionResult
```

## 关键设计细节

### 1. 为什么用 IntEnum 而不是普通 Enum？
```python
class PermissionMode(IntEnum):
    READ_ONLY = 1
    WORKSPACE_WRITE = 2
    DANGER_FULL_ACCESS = 3
    PROMPT = 4
    ALLOW = 5

# 这样可以直接比较:
PermissionMode.WORKSPACE_WRITE >= PermissionMode.READ_ONLY  # True
```
CC 源码用 Rust 的 `derive(PartialOrd, Ord)` 实现同样效果。

### 2. 为什么用 Protocol 而不是 ABC？
Protocol 是 Python 的"结构化子类型"（鸭子类型的正式版）。
不需要继承，只要你的类有 `decide()` 方法就自动匹配。
CC 源码用 Rust 的 trait 实现同样效果。
这样做的好处：CLI 的 prompter 弹终端对话框，IDE 的 prompter 弹 GUI，都能用。

### 3. 未注册工具默认 DangerFullAccess
这是**默认从严 (fail-closed)** 原则。
如果忘记给工具设权限，它默认需要最高权限 → 用户会被提示 → 不会静默执行。

### 4. Builder 模式构建 Policy
```python
policy = (PermissionPolicy(PermissionMode.WORKSPACE_WRITE)
    .with_tool_requirement("read_file", PermissionMode.READ_ONLY)
    .with_tool_requirement("write_file", PermissionMode.WORKSPACE_WRITE)
    .with_tool_requirement("bash", PermissionMode.DANGER_FULL_ACCESS))
```

## 易错点

- `PermissionMode` 用 `IntEnum` 不是 `Enum`（需要 `>=` 比较）
- `with_tool_requirement` 要返回 `self`
- `required_mode_for()` 对未注册工具默认返回 `DANGER_FULL_ACCESS`，不是 `READ_ONLY`
- `authorize` 中 Allow 模式的判断要放在 `>=` 比较之前（因为 Allow 是特殊模式）
- prompter 可以是 None（没有 prompter 时，需要询问的场景应该拒绝）
