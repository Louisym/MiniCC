# 14 — UI 与状态管理：在终端中构建浏览器

> **范围**: `src/ink/`（49 个文件，~600KB）、`src/state/`（3 个文件，~5KB）、`src/screens/REPL.tsx`（874KB）、`src/components/`（~200 个文件）
>
> **一句话概括**: Claude Code 搭载了一个 fork 并重写的 Ink 框架 —— React 19 并发渲染、Yoga flexbox 布局引擎、Int32Array 位打包双缓冲屏幕、W3C 事件分发模型，全部在终端中以 60fps 渲染。

---

## 1. 终端 UI 技术栈层次

```
第5层: React 组件              ← REPL.tsx (874K), PromptInput, StatusLine
第4层: React 19 Reconciler     ← reconciler.ts — ConcurrentRoot，非 LegacyRoot
第3层: 自定义 DOM (ink-box)    ← dom.ts — 带事件处理器的虚拟 DOM 节点
第2层: Yoga 布局引擎           ← Facebook 的 Flexbox-in-C，编译为 WASM
第1层: 屏幕缓冲区 (Int32)     ← screen.ts — 位打包 typed array，零 GC
第0层: ANSI 差分 → stdout      ← log-update.ts — 仅写入变化的单元格
```

**源码坐标**: `src/ink/reconciler.ts:224` — `createReconciler()` 配置了 React 19 的 fiber 架构，包含 `maySuspendCommit()`、`preloadInstance()` 等 React 19 必需方法。

---

## 2. 为什么 Fork Ink

| 原版限制 | Claude Code 需求 | 解决方案 |
|---------|-----------------|---------|
| LegacyRoot 渲染器 | 并发特性、Suspense | React 19 ConcurrentRoot |
| 无事件系统 | 快捷键、焦点管理 | W3C 捕获/冒泡事件分发 |
| 全屏重绘 | 大量输出下的 60fps | Int32Array 双缓冲 + ANSI 差分 |
| 无备用屏幕 | 叠加层对话框、搜索 | Alt-screen 管理 |
| 无虚拟滚动 | 10 万+ 行对话历史 | WeakMap 高度缓存 + 窗口化渲染 |
| 无文本选择 | 终端中的复制粘贴 | 选择系统 + NoSelect 区域 |
| 无搜索功能 | 对话内查找 | 搜索高亮叠加层（SGR 堆叠） |

---

## 3. 渲染管线

每次按键或状态变化触发以下管线：

```
stdin 字节流
  → parse-keypress.ts (23K) — 原始字节序列解析为 KeyPress 事件
  → Dispatcher.dispatch() — W3C 捕获/冒泡，穿过 DOM 树
  → React setState / useSyncExternalStore
  → React 协调（fiber 树差分）
  → Yoga 布局计算（flexbox → 绝对位置）
  → render-node-to-output.ts (63K) — DOM 树 → Screen 缓冲区
  → screen.ts diff() — 前后缓冲区对比（Int32 整数比较）
  → log-update.ts (27K) — 仅输出变化单元格的 ANSI 序列
  → stdout.write()
```

帧调度以 16ms 节流（~60fps 目标），避免每次状态变化都触发渲染。

---

## 4. 屏幕缓冲区：零 GC 的位打包数组

// 源码位置: src/ink/screen.ts（1,487 行，49KB）

这是整个 UI 系统中性能最关键的代码。每个单元格用 2 个 Int32 连续存储，而非分配 Cell 对象（200×120 屏幕将避免分配 24,000 个对象）：

```typescript
// word0: charId（32 位 — CharPool 索引）
// word1: styleId[31:17] | hyperlinkId[16:2] | width[1:0]
```

### CharPool 与 StylePool：字符串驻留

字符串通过 `CharPool` 驻留为整数 ID，ASCII 快速路径直接数组查找（无需 Map.get）。样式转换在单元格间被缓存 —— `StylePool.transition(fromId, toId)` 返回预序列化的 ANSI 转义字符串，首次调用后零分配。

### 双缓冲

`cells64` BigInt64Array 视图共享同一 ArrayBuffer，实现单次 `fill()` 调用清零整个屏幕。缓冲区只增长不缩小，避免重复分配。

---

## 5. 事件系统：终端中的 W3C

// 源码位置: src/ink/events/dispatcher.ts

Claude Code 在终端内实现了 **W3C 捕获/冒泡事件模型** —— 与浏览器相同的事件传播模型：

```
捕获阶段: root → target（自顶向下）
目标阶段: 目标节点上的事件处理器
冒泡阶段: target → root（自底向上）
```

`Dispatcher` 与 React 19 的更新优先级系统集成：离散事件（按键、点击）获得更高优先级，连续事件（滚动）获得较低优先级。

### FocusManager

焦点以**栈**方式管理 —— 每个可聚焦组件向 `FocusManager` 注册，追踪焦点链。

---

## 6. 35 行 Store（替代 Redux）

// 源码位置: src/state/store.ts — 恰好 35 行

可能是整个代码库中最优雅的代码：

```typescript
export function createStore<T>(initialState: T, onChange?: OnChange<T>): Store<T> {
  let state = initialState
  const listeners = new Set<Listener>()
  return {
    getState: () => state,
    setState: (updater) => {
      const prev = state
      const next = updater(prev)
      if (Object.is(next, prev)) return   // 引用相等跳过
      state = next
      onChange?.({ newState: next, oldState: prev })
      for (const listener of listeners) listener()
    },
    subscribe: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}
```

`Store` 接口 `{ getState, setState, subscribe }` 恰好是 React 18+ 的 `useSyncExternalStore` 所期望的格式。无中间件、无 reducer、无 action。`onChangeAppState` 集中处理所有副作用。

---

## 7. REPL 屏幕架构

// 源码位置: src/screens/REPL.tsx — 874KB

```
<FullscreenProvider>             ← 终端尺寸跟踪
  <AlternateScreen>              ← 模态叠加层的备用屏幕
    <FullscreenLayout>           ← Flexbox 根（全终端）
      <ScrollBox>                ← 可滚动容器
        <VirtualMessageList>     ← 窗口化渲染（视口 ± 1 屏）
      <PromptInput>              ← 文本输入（支持 vim 模式）
      <StatusLine>               ← 底部状态栏
      <OverlayStack>             ← 权限对话框、模型选择器、搜索
```

对话框作为叠加层渲染在 alt-screen 上，保留对话上下文。

---

## 8. 虚拟滚动与高度缓存

对于包含数千条消息的对话，只渲染可见消息加上缓冲区：

```
可见视口: messages[startIdx..endIdx]
缓冲区: 视口上下各 ±1 屏高度
其他: <Spacer height={cachedHeight} />
```

高度缓存使用 `WeakMap`，消息从对话中移除时条目自动垃圾回收。

搜索（`Ctrl+F`）时，当前匹配通过 `StylePool.withCurrentMatch()` 应用黄底 + 粗体 + 下划线 SGR 叠加层；其他匹配用 `withInverse()` —— 视觉上有区分但不那么突出。

---

## 9. Vim 模式

// 源码位置: src/hooks/useVimInput.ts

简化的两态模型：

```typescript
export type VimMode = 'INSERT' | 'NORMAL'
```

`NORMAL` 模式下按键被拦截用于导航（hjkl、w、b、0、$等）和编辑命令（dd、yy、p等）。模式状态从 `REPL.tsx` 流经 `PromptInput`、`StatusLine`、`useCancelRequest`。

---

## 10. 键绑定系统

| 上下文 | 激活时机 | 示例绑定 |
|--------|---------|---------|
| **全局** | 始终 | `Ctrl+C`（取消）、`Ctrl+D`（退出） |
| **聊天** | 对话中 | `Shift+Tab`（模式切换）、`Enter`（提交） |
| **权限** | 权限对话框打开时 | `y/n`（允许/拒绝） |
| **搜索** | 搜索激活时 | `Ctrl+G`（下一个匹配）、`Escape`（关闭） |

用户可通过 `~/.claude/keybindings.json` 自定义覆盖默认绑定。

---

## 可迁移设计模式

### 模式 1："35 行替代一个库"

当用例足够具体时，35 行定制方案胜过 50KB 依赖。关键是 `useSyncExternalStore` 已做完重活 —— 你只需匹配它期望的 API 形状。

### 模式 2：位打包 Typed Array 消除 GC

Screen 用 `Int32Array` + 位打包替代对象。`BigInt64Array` 视图实现批量清零。这种模式可迁移到任何高吞吐数据结构。

### 模式 3：非浏览器环境中的浏览器事件模型

W3C 捕获/冒泡事件分发在浏览器外同样优雅。关键适配：将终端特有事件映射到 React 组件期望的事件传播模型。

---

## 组件总结

| 组件 | 大小 | 角色 |
|------|------|------|
| `ink.tsx` | 252KB | 核心渲染引擎、React 集成、帧调度 |
| `screen.ts` | 49KB | 位打包 Int32Array 屏幕缓冲区、双缓冲、单元格差分 |
| `render-node-to-output.ts` | 63KB | DOM 树 → Screen 缓冲区转换 |
| `selection.ts` | 35KB | 文本选择 + NoSelect 区域 |
| `log-update.ts` | 27KB | ANSI 差分输出 —— 仅发送变化单元格 |
| `parse-keypress.ts` | 23KB | 原始 stdin → KeyPress 事件解析 |
| `reconciler.ts` | 15KB | React 19 ConcurrentRoot fiber 调和器 |
| `store.ts` | **836B** | 完整状态管理 —— 35 行 |
| `REPL.tsx` | 874KB | 主屏幕：虚拟滚动、叠加层、vim 模式 |

---

**上一篇**: [← 13 — 桥接系统](13-bridge-system.md)
**下一篇**: [→ 15 — 服务层与 API 架构](15-services-api-layer.md)
