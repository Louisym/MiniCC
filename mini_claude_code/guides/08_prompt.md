# 08 - 提示词构建 (prompt.py)

## 问题背景

系统提示词不是一个字符串，而是一个**分段列表** (`Vec<String>`)。这样设计是为了让 Anthropic API 对不同段做 prompt caching — 不变的段缓存命中，只有变化的段需要重新计算。

CC 在启动时自动发现 CLAUDE.md 文件、读 git 状态、按预算截断，然后拼装出完整的系统提示词。

## CC 的做法

源码: `rust/crates/runtime/src/prompt.rs` (784 行)

### 关键设计

#### 1. 静态/动态边界 (Dynamic Boundary)
```
[0] 介绍（你是什么）              ← 静态，可缓存
[1] 输出风格（可选）
[2] 系统规则
[3] 任务指南
[4] 行动准则
─── __SYSTEM_PROMPT_DYNAMIC_BOUNDARY__ ───  ← 分割线
[5] 环境信息（日期、CWD、平台）   ← 动态，每次变
[6] 项目上下文（git status/diff）
[7] CLAUDE.md 内容
[8+] 追加 section
```
边界以上的部分每次都一样 → 命中 prompt cache → 省钱。
边界以下的部分每次可能变 → 不缓存。

#### 2. 祖先链发现 (Ancestor Chain Discovery)
从根目录到 cwd，逐级搜索 4 种文件 (prompt.rs:192-212):
```
CLAUDE.md           — 项目指令（提交到仓库）
CLAUDE.local.md     — 本地指令（不提交）
.claude/CLAUDE.md   — 嵌套目录下的指令
.claude/instructions.md — 嵌套指令
```
如果 `/a/b/c` 是 cwd，搜索: `/CLAUDE.md`, `/a/CLAUDE.md`, `/a/b/CLAUDE.md`, `/a/b/c/CLAUDE.md`...

#### 3. 内容去重 (Deduplication)
如果两个目录的 CLAUDE.md 内容一样（空白归一化后），只保留第一个 (prompt.rs:326-341)。

#### 4. 双层字符预算
- 单文件上限: `MAX_INSTRUCTION_FILE_CHARS = 4000` 字符
- 总预算: `MAX_TOTAL_INSTRUCTION_CHARS = 12000` 字符
- 超出时截断并加 `[truncated]` 标记 (prompt.rs:366-376)

## 你要练习的工程模式

| 模式 | 说明 | 源码位置 |
|------|------|---------|
| **Builder 模式** | `SystemPromptBuilder` 链式构建各段 | prompt.rs:95-156 |
| **祖先链发现** | 从根到 cwd 逐级搜索 | prompt.rs:192-212 |
| **内容去重** | 空白归一化 → hash 比较 → 去重 | prompt.rs:326-341 |
| **双层预算截断** | 单文件 4000 + 总计 12000 | prompt.rs:39-40, 366-376 |
| **静态/动态边界** | prompt cache 经济优化 | prompt.rs:37, 143 |

## 你需要写的东西

```python
SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"
MAX_INSTRUCTION_FILE_CHARS = 4_000
MAX_TOTAL_INSTRUCTION_CHARS = 12_000

ContextFile(BaseModel)   — path + content
ProjectContext(BaseModel) — cwd, date, git_status, git_diff, instruction_files

discover_instruction_files(cwd: Path) -> list[ContextFile]
  祖先链逐级搜索 4 种文件 → 去重 → 返回

SystemPromptBuilder
  with_os(name, version) -> self
  with_project_context(ctx) -> self
  with_config(config) -> self
  append_section(text) -> self
  build() -> list[str]        # 返回分段列表

辅助函数:
  truncate_content(text, budget) -> str     # 截断 + [truncated]
  normalize_content(text) -> str            # 空白归一化
  dedupe_files(files) -> list[ContextFile]  # hash 去重
  collapse_blank_lines(text) -> str
```

## 易错点

- `build()` 返回 `list[str]` 不是单个字符串 — 这是为了 prompt caching
- 祖先链从根到 cwd（不是从 cwd 往上），这样根目录的指令先加载，子目录覆盖
- 去重用归一化后的 hash，不是原始内容比较
- 截断时 `[truncated]` 标记不计入预算
- 总预算是累减的：每处理一个文件就减去消耗的字符数
- git status/diff 可能为 None（不是 git 仓库时）
