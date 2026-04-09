# 第 14 集：UI 与状态管理 —— 终端里的浏览器

> 🌐 **语言**: 中文 | [English →](/chapters/14-ui-state-management)
> 📖 **[在线阅读 →](https://openedclaude.github.io/claude-reviews-claude/zh-CN/chapters/14-ui-state-management)** — 支持侧边栏导航、暗色模式与全文搜索，体验更佳。


> **源码文件**：`ink/` 目录 — 48 个文件，约 620KB。核心：`ink.tsx` (252KB)、`reconciler.ts` (14.6KB)、`renderer.ts` (7.7KB)、`dom.ts` (15.1KB)、`screen.ts` (49.3KB)、`events/dispatcher.ts` (6KB)、`focus.ts` (5.1KB)。状态管理：`state/store.ts` (836 字节)、`state/AppStateStore.ts` (21.8KB)、`state/AppState.tsx` (23.5KB)、`state/onChangeAppState.ts` (6.2KB)。屏幕：`screens/REPL.tsx` (874KB)、`screens/Doctor.tsx` (71KB)
>
> **一句话概括**：Claude Code 运行着一个完全 Fork 的 Ink 渲染引擎 —— React 19 + ConcurrentRoot、W3C 风格的捕获/冒泡事件系统、Yoga Flexbox 布局、以打包 Int32Array 实现的双缓冲屏幕、和一个仅 35 行的 Zustand 替代品 —— 全部运行在你的终端里。

---

## 1. 终端 UI 技术栈

大多数 CLI 工具逐行打印文本。Claude Code 在终端中构建了一整套基于组件的 UI 框架 —— 其技术栈深度令人惊讶：

```
┌─────────────────────────────────────────────────────────────┐
│  React 19 (通过 react-reconciler 实现 ConcurrentRoot)       │
│    └─ 自定义 Ink Reconciler (reconciler.ts, 513 行)         │
│        └─ 虚拟 DOM (dom.ts — ink-root/box/text/link/...)    │
│            └─ Yoga 布局引擎 (终端中的 Flexbox)              │
│                └─ 屏幕缓冲 (screen.ts — 打包 Int32 数组)    │
│                    └─ ANSI Diff (log-update.ts → stdout)    │
└─────────────────────────────────────────────────────────────┘
```

### 核心文件索引

| 层级 | 文件 | 大小 | 职责 |
|------|------|------|------|
| 入口 | `ink/root.ts` | 4.6KB | 创建 Ink 实例，挂载 React 树 |
| 协调器 | `ink/reconciler.ts` | 14.6KB | React 19 宿主配置，提交钩子 |
| 渲染器 | `ink/renderer.ts` | 7.7KB | Yoga 布局 → 屏幕缓冲 |
| DOM | `ink/dom.ts` | 15.1KB | 虚拟 DOM 节点，脏标记 |
| 屏幕 | `ink/screen.ts` | 49.3KB | 打包 Int32Array 单元格缓冲 |
| 核心 | `ink/ink.tsx` | 252KB | 帧调度，输入处理，选区 |
| 事件 | `ink/events/dispatcher.ts` | 6KB | W3C 捕获/冒泡派发 |
| 焦点 | `ink/focus.ts` | 5.1KB | FocusManager + 栈式恢复 |
| 输出 | `ink/log-update.ts` | 27.2KB | ANSI 差分，光标管理 |

### 完整渲染管线

```
stdin 原始字节
  → parse-keypress.ts：解码为 ParsedKey（xterm/VT 序列）
  → 创建 InputEvent
  → Dispatcher.dispatchDiscrete()：W3C 捕获 → 目标 → 冒泡
  → React 状态更新 → Reconciler 提交阶段
  → resetAfterCommit() → rootNode.onComputeLayout() [Yoga]
  → rootNode.onRender() → renderer.ts 生成 Screen 缓冲
  → log-update.ts：对比前后 Screen → ANSI 转义序列
  → process.stdout.write()
```

每次按键都走完这条完整管线。在 16ms 帧节流下，Claude Code 在终端中维持 60fps 等效渲染。

---

## 2. 为什么要 Fork Ink

Claude Code 并不使用 npm 上的 `ink` 包，而是维护了一个完整的 Fork 版本，至少包含七项重大修改。理解其原因，就能看到这个项目的工程野心。

### 修改清单

| 变更 | 原版 Ink | Fork 版 Ink | 原因 |
|------|---------|------------|------|
| React 版本 | LegacyRoot | **ConcurrentRoot (React 19)** | 并发特性、`useSyncExternalStore`、过渡 |
| 事件系统 | 基础 `useInput` | **W3C 捕获/冒泡派发器** | 复杂的重叠焦点上下文 |
| 屏幕模式 | 普通滚动缓冲 | **备用屏幕 + 鼠标追踪** | 全屏 TUI，不污染滚动历史 |
| 渲染 | 单缓冲 | **双缓冲 + 打包 Int32 屏幕** | 零闪烁渲染，CJK/emoji 支持 |
| 文本选择 | 无 | **鼠标拖拽选择 + 剪贴板** | 从终端输出中复制代码 |
| 滚动 | 全量重渲染 | **虚拟滚动 + 高度缓存** | 1000+ 条消息无性能悬崖 |
| 搜索 | 无 | **全屏搜索 + 逐单元格高亮** | 在整个对话中查找文本 |

### React 19 Reconciler：React 与终端之间的桥梁

```typescript
// 源码：ink/reconciler.ts:224-506
const reconciler = createReconciler<
  ElementNames,  // 'ink-root' | 'ink-box' | 'ink-text' | ...
  Props,
  DOMElement,    // 虚拟 DOM 节点
  DOMElement,    // 容器类型
  TextNode,      // 文本节点类型
  DOMElement,    // Suspense 边界
  unknown, unknown, DOMElement,
  HostContext,
  null,          // UpdatePayload — React 19 中不再使用
  NodeJS.Timeout,
  -1, null
>({
  // React 19 commitUpdate — 直接接收新旧 props
  //（React 18 使用 updatePayload）
  commitUpdate(node, _type, oldProps, newProps) {
    const props = diff(oldProps, newProps)
    const style = diff(oldProps['style'], newProps['style'])
    // 增量更新：只处理变更的属性和样式
  },

  // 关键钩子：每次 React 提交后，重新计算布局 + 渲染
  resetAfterCommit(rootNode) {
    rootNode.onComputeLayout?.()  // Yoga flexbox 计算
    rootNode.onRender?.()         // 绘制到屏幕缓冲 → stdout
  },
})
```

`UpdatePayload` 泛型为 `null` —— 这是 React 19 的标志。React 18 中协调器在 `prepareUpdate()` 中预计算差分载荷，然后传给 `commitUpdate()`。React 19 消除了这个中间步骤，直接传递新旧 props。这是 Claude Code 构建在前沿 React 内部实现之上的最明确信号之一。

---

## 3. 渲染管线深度剖析

### DOM 节点结构

每个 UI 元素都会成为虚拟树中的 `DOMElement` 节点：

```typescript
// 源码：ink/dom.ts:31-91
type DOMElement = {
  nodeName: ElementNames           // 'ink-root' | 'ink-box' | 'ink-text' | ...
  attributes: Record<string, DOMNodeAttribute>
  childNodes: DOMNode[]
  parentNode: DOMElement | undefined
  yogaNode?: LayoutNode            // Yoga flexbox 布局节点
  style: Styles                    // Flexbox 属性
  dirty: boolean                   // 需要重渲染

  // 事件处理器 —— 与属性分开存储，
  // 使处理器引用变化不会标记脏，避免破坏 blit 优化
  _eventHandlers?: Record<string, unknown>

  // overflow: 'scroll' 容器的滚动状态
  scrollTop?: number
  pendingScrollDelta?: number      // 累积增量，逐帧消耗
  scrollClampMin?: number          // 虚拟滚动钳制边界
  scrollClampMax?: number
  stickyScroll?: boolean           // 自动钉底
  scrollAnchor?: { el: DOMElement; offset: number }  // 延迟位置读取
  focusManager?: FocusManager      // 焦点管理（仅根节点）
}
```

七种元素类型映射了终端 UI 词汇：

| 类型 | 用途 | 有 Yoga 节点？ |
|------|------|---------------|
| `ink-root` | 树根 | ✅ |
| `ink-box` | Flexbox 容器 (`<Box>`) | ✅ |
| `ink-text` | 文本内容 (`<Text>`) | ✅（带测量函数）|
| `ink-virtual-text` | `<Text>` 内嵌套文本 | ❌ |
| `ink-link` | 终端超链接 (OSC 8) | ❌ |
| `ink-progress` | 进度条 | ❌ |
| `ink-raw-ansi` | 预渲染 ANSI 透传 | ✅（固定尺寸）|

### 屏幕缓冲：打包 Int32Array

这里是 Claude Code 在性能方面*真正认真*的地方。与其为每个单元格分配对象（200×120 屏幕意味着 24,000 个对象），屏幕将单元格存储为打包整数：

```typescript
// 源码：ink/screen.ts:332-348
// 每个单元格 = 2 个连续 Int32 元素：
//   word0 (cells[ci]):     charId（完整 32 位，CharPool 的索引）
//   word1 (cells[ci + 1]): styleId[31:17] | hyperlinkId[16:2] | width[1:0]

const STYLE_SHIFT = 17
const HYPERLINK_SHIFT = 2
const WIDTH_MASK = 3           // 2 位（窄/宽/SpacerTail/SpacerHead）

function packWord1(styleId: number, hyperlinkId: number, width: number): number {
  return (styleId << STYLE_SHIFT) | (hyperlinkId << HYPERLINK_SHIFT) | width
}
```

同一 ArrayBuffer 上的 `cells64` BigInt64Array 视图支持 8 字节批量填充 `cells64.fill(0n)` —— 一次操作清空整个屏幕，而非逐单元格迭代。

**字符串驻留**进一步降低内存压力：

```typescript
// 源码：ink/screen.ts:21-53
class CharPool {
  private ascii: Int32Array = initCharAscii()  // ASCII 快速路径

  intern(char: string): number {
    if (char.length === 1) {
      const code = char.charCodeAt(0)
      if (code < 128) {
        const cached = this.ascii[code]!
        if (cached !== -1) return cached  // 直接数组查找，无 Map.get
      }
    }
    // 非 ASCII（CJK、emoji）回退到 Map
    return this.stringMap.get(char) ?? this.addNew(char)
  }
}
```

### 双缓冲

渲染器维护两个 `Frame` 对象 —— `frontFrame` 和 `backFrame`。每次渲染：

1. 重置**后台缓冲**（通过 `resetScreen()` —— 一次 `cells64.fill(0n)` 调用）
2. 将 DOM 树渲染到后台缓冲
3. 与**前台缓冲**对比生成最小 ANSI 输出
4. 交换：后台变前台供下帧使用

`prevFrameContaminated` 标志追踪前台缓冲在渲染后被修改的情况（如选区叠加）。被污染时，渲染器跳过 blit 优化执行全量重绘 —— 但只限那一帧。

---

## 4. 事件系统：终端中的 W3C

也许是最令人意外的工程决策：Claude Code 在终端事件上实现了完整的 W3C 风格事件派发系统。这不是学术洁癖 —— 当你有重叠对话框、嵌套滚动容器、以及需要在不同树深度拦截按键的 Vim 模式时，这是实际需求。

### 事件派发阶段

```typescript
// 源码：ink/events/dispatcher.ts:46-78
function collectListeners(target, event): DispatchListener[] {
  const listeners: DispatchListener[] = []
  let node = target
  while (node) {
    const isTarget = node === target
    // 捕获处理器：unshift → 根优先顺序
    const captureHandler = getHandler(node, event.type, true)
    if (captureHandler) {
      listeners.unshift({ node, handler: captureHandler,
        phase: isTarget ? 'at_target' : 'capturing' })
    }
    // 冒泡处理器：push → 目标优先顺序
    const bubbleHandler = getHandler(node, event.type, false)
    if (bubbleHandler && (event.bubbles || isTarget)) {
      listeners.push({ node, handler: bubbleHandler,
        phase: isTarget ? 'at_target' : 'bubbling' })
    }
    node = node.parentNode
  }
  return listeners
  // 结果：[根捕获, ..., 父捕获, 目标捕获, 目标冒泡, 父冒泡, ..., 根冒泡]
}
```

### 事件优先级：镜像 react-dom

```typescript
// 源码：ink/events/dispatcher.ts:122-138
function getEventPriority(eventType: string): number {
  switch (eventType) {
    case 'keydown': case 'keyup': case 'click':
    case 'focus': case 'blur': case 'paste':
      return DiscreteEventPriority     // 同步刷新
    case 'resize': case 'scroll': case 'mousemove':
      return ContinuousEventPriority   // 可批处理
    default:
      return DefaultEventPriority
  }
}
```

这直接映射到 React 的调度器优先级。按键触发同步 React 更新（离散优先级），而滚动事件会被批处理（连续优先级）。

### 焦点管理：栈式恢复

```typescript
// 源码：ink/focus.ts:15-82
class FocusManager {
  activeElement: DOMElement | null = null
  private focusStack: DOMElement[] = []  // 最多 32 条目

  focus(node) {
    if (node === this.activeElement) return
    const previous = this.activeElement
    if (previous) {
      // 推入前去重（防止 Tab 循环导致无界增长）
      const idx = this.focusStack.indexOf(previous)
      if (idx !== -1) this.focusStack.splice(idx, 1)
      this.focusStack.push(previous)
      if (this.focusStack.length > MAX_FOCUS_STACK) this.focusStack.shift()
    }
    this.activeElement = node
  }

  // 对话框关闭时，焦点自动返回前一个元素
  handleNodeRemoved(node, root) {
    this.focusStack = this.focusStack.filter(n => n !== node && isInTree(n, root))
    // 从栈中恢复最近一个仍挂载的元素
    while (this.focusStack.length > 0) {
      const candidate = this.focusStack.pop()!
      if (isInTree(candidate, root)) {
        this.activeElement = candidate
        return
      }
    }
  }
}
```

焦点栈硬限制 32 条目（`MAX_FOCUS_STACK`）。Tab 循环在推入前去重，防止栈随反复导航而增长。当对话框从树中移除时，协调器调用 `handleNodeRemoved()`，反向遍历栈找到最近仍挂载的元素 —— 无需显式销毁逻辑即实现焦点自动恢复。

---

## 5. 35 行 Store（替代 Redux/Zustand）

这是那种让你停下来思考的工程决策。Claude Code 并未引入状态管理库，而是仅用 35 行代码实现了整个应用状态管理：

```typescript
// 源码：state/store.ts —— 完整文件（35 行）
export function createStore<T>(
  initialState: T,
  onChange?: OnChange<T>,
): Store<T> {
  let state = initialState
  const listeners = new Set<Listener>()

  return {
    getState: () => state,
    setState: (updater: (prev: T) => T) => {
      const prev = state
      const next = updater(prev)
      if (Object.is(next, prev)) return   // 引用相等跳过
      state = next
      onChange?.({ newState: next, oldState: prev })  // 副作用钩子
      for (const listener of listeners) listener()    // 通知订阅者
    },
    subscribe: (listener: Listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}
```

没有中间件链、没有 devtools 集成、没有 action 类型、没有 reducer。就是 `getState`、`setState`（带更新函数）和 `subscribe`。`Object.is` 检查防止无效重渲染。`onChange` 回调集中管理副作用。

### 通过 useSyncExternalStore 集成 React

```typescript
// 源码：state/AppState.tsx:142-163
export function useAppState<T>(selector: (state: AppState) => T): T {
  const store = useAppStore()
  const get = () => selector(store.getState())
  return useSyncExternalStore(store.subscribe, get, get)
}

// 在组件中使用：
const verbose = useAppState(s => s.verbose)
const model = useAppState(s => s.mainLoopModel)
```

`useSyncExternalStore` 钩子（React 18+）保证在并发渲染期间的撕裂安全读取 —— 与 Zustand 内部使用的基元完全相同。Claude Code 只是不需要 Zustand 的包装层。

### AppState：完整应用状态类型

`AppStateStore.ts` 定义了 `AppState` 类型 —— **570 行**的类型定义覆盖应用各个方面：

```typescript
// 源码：state/AppStateStore.ts:89-452（精简版）
export type AppState = DeepImmutable<{
  settings: SettingsJson           // 会话设置
  mainLoopModel: ModelSetting      // 主循环模型
  expandedView: 'none' | 'tasks' | 'teammates'  // UI 显示状态
  toolPermissionContext: ToolPermissionContext    // 权限系统
  remoteConnectionStatus: '...'    // 远程/Bridge
  speculation: SpeculationState    // 推测执行
}> & {
  tasks: { [taskId: string]: TaskState }         // 可变状态
  mcp: { clients, tools, commands, resources }   // MCP
  plugins: { enabled, disabled, commands }       // 插件
  teamContext?: { teamName, teammates, ... }     // 团队
  computerUseMcpState?: { ... }                  // Computer Use
}
```

`DeepImmutable<>` 包装器防止大多数字段的意外修改。包含 `Map`、`Set`、函数类型或任务状态的字段通过交叉类型 (`&`) 排除在包装器之外 —— 类型安全与表达力之间的务实折衷。

### 副作用集中化

所有状态变更副作用通过单一 `onChangeAppState` 回调汇聚：

```typescript
// 源码：state/onChangeAppState.ts:43-171
export function onChangeAppState({ newState, oldState }) {
  // 权限模式 → 同步到 CCR/SDK
  if (prevMode !== newMode) {
    notifySessionMetadataChanged({ permission_mode: newExternal })
  }
  // 模型变更 → 持久化到设置文件
  if (newState.mainLoopModel !== oldState.mainLoopModel) {
    updateSettingsForSource('userSettings', { model: newState.mainLoopModel })
  }
  // 设置变更 → 清除认证缓存 + 重新应用环境变量
  if (newState.settings !== oldState.settings) {
    clearApiKeyHelperCache()
  }
}
```

这就是"单一咽喉"模式 —— 八条不同代码路径都可以更改权限模式，但全部流经这一个差分。在此集中化之前，每条路径都需要手动通知 CCR，有几条没有做到 —— 导致 Web UI 与 CLI 状态不同步。

---

## 6. REPL 屏幕架构

`screens/REPL.tsx` (874KB) 是应用的主界面 —— 一个编排所有用户功能的 React 函数组件。编译输出约 12,000 行，是代码库中最大的单一组件。

### 组件层次

```
<REPL>
  <KeybindingSetup>                // 初始化键绑定系统
    <AlternateScreen>              // 进入终端备用屏幕模式
      <FullscreenLayout>           // 全屏布局
        <ScrollBox stickyScroll>   // 可滚动主内容区
          <VirtualMessageList>     // 虚拟滚动
            <Messages>             // 消息渲染（递归）
          </VirtualMessageList>
        </ScrollBox>
        <StatusLine>               // 模型 │ 权限 │ 工作目录 │ token │ 费用
        <PromptInput>              // 用户输入 + 自动补全 + 底栏按钮
      </FullscreenLayout>
    </AlternateScreen>

    // 覆盖层对话框
    <PermissionRequest>            // 工具权限确认
    <ModelPicker>                  // 模型选择 (Meta+P)
    <GlobalSearchDialog>           // 全文搜索 (Ctrl+F)
    // ... 15+ 种覆盖层对话框
  </KeybindingSetup>
</REPL>
```

### 三个屏幕组件

| 屏幕 | 文件 | 大小 | 用途 |
|------|------|------|------|
| REPL | `screens/REPL.tsx` | 874KB | 主交互循环 |
| Doctor | `screens/Doctor.tsx` | 71KB | 环境诊断 (`/doctor`) |
| ResumeConversation | `screens/ResumeConversation.tsx` | 58KB | 会话恢复 (`--resume`) |

### 查询循环流程

```
用户输入 → handleSubmit()
  → 创建 UserMessage → addToHistory()
  → query({ messages, tools, onMessage, ... })
    → 流式回调：handleMessageFromStream()
      → setMessages(prev => [...prev, newMessage])
      → 工具调用 → useCanUseTool → 权限检查
        → 允许 → 执行工具 → 追加结果
        → 拒绝 → 追加拒绝消息
    → 完成 → 记录分析 → 保存会话
```

---

## 7. 虚拟滚动与高度缓存

当对话增长到数百条消息时，每帧渲染每条消息会摧毁性能。Claude Code 实现了终端虚拟滚动 —— 一种借鉴自浏览器虚拟列表库（如 `react-window`）的技术。

### 核心策略

```
┌────────────────────────────────┐
│  Spacer（估计高度）            │  ← 不渲染，固定高度 Box
├────────────────────────────────┤
│  缓冲区（上方 1 屏高度）       │  ← 已渲染但不可见
├────────────────────────────────┤
│  ████████████████████████████  │
│  ████ 可见视口 ██████████████  │  ← 用户实际可见
│  ████████████████████████████  │
├────────────────────────────────┤
│  缓冲区（下方 1 屏高度）       │  ← 已渲染但不可见
├────────────────────────────────┤
│  Spacer（估计高度）            │  ← 不渲染，固定高度 Box
└────────────────────────────────┘
```

### 关键设计决策

- **WeakMap 高度缓存**：每条消息的渲染高度通过 WeakMap 缓存，键为消息对象引用。消息引用不变时直接复用高度无需重新测量。

- **窗口 = 视口 + 1 屏缓冲**：仅渲染可见视口加上下各一屏高度内的消息。其余全部替换为 `<Box height={N}>` 占位符。

- **滚动钳制边界**：DOM 元素上的 `scrollClampMin`/`scrollClampMax` 防止滚动位置进入未渲染区域。用户滚动快于 React 重渲染时，渲染器停在已挂载内容边缘而非显示空白。

- **粘性底部滚动**：新消息通过 `stickyScroll` 自动滚动到底部。仅用户显式上滚时取消钉底。

- **搜索索引**：全文搜索构建所有消息的缓存纯文本索引。搜索高亮在屏幕缓冲层面应用（逐单元格样式叠加），而非通过 React 重渲染。

### ScrollBox：滚动容器

`pendingScrollDelta` 累积器每帧消耗 `SCROLL_MAX_PER_FRAME` 行 —— 快速滑动显示中间帧而非一次大跳。方向反转自然抵消（纯累积器，无目标跟踪）。

---

## 8. Vim 模式状态机

Claude Code 为输入框内建了完整的 Vim 编辑模式 —— 不是子集，而是包含运算符、动作、文本对象、寄存器和点重复的完整实现。

### 状态机架构

```typescript
// 源码：vim/ 目录
type VimState =
  | { mode: 'INSERT'; insertedText: string }
  | { mode: 'NORMAL'; command: CommandState }

type CommandState =
  | { type: 'idle' }                                  // 等待输入
  | { type: 'count'; digits: string }                 // 前缀计数 (3dw)
  | { type: 'operator'; op: Operator; count }         // 等待动作 (d_)
  | { type: 'operatorCount'; op, count, digits }      // 运算符 + 计数 (d3w)
  | { type: 'operatorFind'; op, count, find }         // 运算符 + 查找 (df_)
  | { type: 'operatorTextObj'; op, count, scope }     // 运算符 + 文本对象 (diw)
  | { type: 'find'; find: FindType; count }           // f/F/t/T 等待字符
  | { type: 'g'; count }                              // g 前缀命令
  | { type: 'replace'; count }                        // r 等待替换字符
  | { type: 'indent'; dir: '>' | '<'; count }         // >> / << 缩进
```

### 状态转换图

```
  idle ──┬─[d/c/y]──► operator ──┬─[motion]──► execute
         ├─[1-9]────► count      ├─[0-9]────► operatorCount
         ├─[fFtT]───► find       ├─[ia]─────► operatorTextObj
         ├─[g]──────► g          └─[fFtT]───► operatorFind
         ├─[r]──────► replace
         └─[><]─────► indent
```

### 纯函数转换

转换函数是纯函数 —— 无副作用，确定性输出：

```typescript
function transition(state, input, ctx): TransitionResult {
  switch (state.type) {
    case 'idle':     return fromIdle(input, ctx)
    case 'count':    return fromCount(state, input, ctx)
    case 'operator': return fromOperator(state, input, ctx)
    // ... TypeScript 保证穷举
  }
}
// 返回：{ next?: CommandState; execute?: () => void }
```

### 持久状态（跨命令记忆）

```typescript
type PersistentState = {
  lastChange: RecordedChange | null  // 点重复 (.)
  lastFind: { type, char } | null   // 重复查找 (;/,)
  register: string                   // yank 寄存器内容
  registerIsLinewise: boolean        // 上次 yank 是否行级？
}
```

### 支持的操作

| 类别 | 命令 |
|------|------|
| **移动** | `h/l/j/k`, `w/b/e/W/B/E`, `0/^/$`, `gg/G`, `gj/gk` |
| **运算符** | `d` (删除), `c` (修改), `y` (复制), `>/<` (缩进) |
| **查找** | `f/F/t/T` + 字符, `;/,` 重复 |
| **文本对象** | `iw/aw`, `i"/a"`, `i(/a(`, `i{/a{`, `i[/a[`, `i</a<` |
| **命令** | `x`, `~`, `r`, `J`, `p/P`, `D/C/Y`, `o/O`, `u` (撤销), `.` (重复) |
| **点重复** | 记录插入文本、运算符、替换、大小写切换、缩进 |

`VimTextInput.tsx` (16KB) 组件将该状态机与输入框集成：Normal 模式拦截按键并路由到 `transition()`，Insert 模式直接透传到正常文本编辑。

---

## 9. 键绑定系统

Claude Code 的键绑定系统支持多上下文、Emacs 风格的和弦序列、用户自定义以及保留快捷键 —— 建立在事件系统之上的完整键盘层。

### 上下文绑定解析

每个绑定属于一个**上下文**，决定其何时激活：

```typescript
// 源码：keybindings/defaultBindings.ts（精简版）
const DEFAULT_BINDINGS: KeybindingBlock[] = [
  {
    context: 'Global',              // 始终活跃
    bindings: {
      'ctrl+c': 'app:interrupt',
      'ctrl+d': 'app:exit',
      'ctrl+l': 'app:redraw',
      'ctrl+t': 'app:toggleTodos',
      'ctrl+r': 'history:search',
    }
  },
  {
    context: 'Chat',                // 输入框获得焦点时
    bindings: {
      'escape': 'chat:cancel',
      'shift+tab': 'chat:cycleMode',
      'meta+p': 'chat:modelPicker',
      'enter': 'chat:submit',
      'ctrl+x ctrl+e': 'chat:externalEditor',  // 和弦！
      'ctrl+x ctrl+k': 'chat:killAgents',      // 和弦！
    }
  },
  {
    context: 'Scroll',              // 滚动离开底部时
    bindings: {
      'pageup': 'scroll:pageUp',
      'ctrl+shift+c': 'selection:copy',
    }
  },
]
```

### 和弦支持（Emacs 风格多键序列）

```typescript
// 用户按 ctrl+x → 进入"和弦等待"状态
// 显示 "ctrl+x ..." 提示
// 用户按 ctrl+e → 匹配 'ctrl+x ctrl+e' → 'chat:externalEditor'
// 用户按其他键 → 和弦取消，按键正常处理

type ChordResolveResult =
  | { type: 'match'; action: string }         // 完整匹配
  | { type: 'chord_started'; pending: ... }   // 和弦进行中
  | { type: 'chord_cancelled' }               // 第二键不匹配
  | { type: 'unbound' }                       // 显式解绑
  | { type: 'none' }                          // 无绑定
```

### 在组件中使用键绑定

```typescript
// 单一绑定
useKeybinding('app:toggleTodos', () => {
  setShowTodos(prev => !prev)
}, { context: 'Global' })

// 多重绑定
useKeybindings({
  'chat:submit': () => handleSubmit(),
  'chat:cancel': () => handleCancel(),
}, { context: 'Chat' })
```

### 用户自定义

用户可通过 `~/.claude/keybindings.json` 重写任何非保留绑定。文件通过 Zod schema 验证，无效条目产生警告但不会破坏应用。

---

## 10. Computer Use 集成

Claude Code 集成了 Anthropic 的 Computer Use 能力 —— 让模型能看到屏幕、移动鼠标、操作键盘并控制应用。这是一种完全不同的工具：不是文本输入/输出，而是基于像素和输入事件的操作。

### 与常规工具的对比

| 方面 | 常规工具 | Computer Use 工具 |
|------|---------|-------------------|
| API 块类型 | `tool_use` | `server_tool_use` |
| 执行 | CLI 端 | CLI 端（截图）+ 服务器反馈 |
| 输入 | 结构化 JSON | `{ action, coordinate?, text? }` |
| 输出 | 文本结果 | JPEG 截图（base64） |
| 平台 | 跨平台 | **仅 macOS**（需要 Swift + Rust 原生模块） |

### Executor 模式

```typescript
// 源码：utils/computerUse/executor.ts:259-644
export function createCliExecutor(opts): ComputerExecutor {
  // 两个原生模块：
  //   @ant/computer-use-swift  — 截图、应用管理、TCC
  //   @ant/computer-use-input  — 鼠标、键盘（Rust/enigo）

  const cu = requireComputerUseSwift()  // 工厂时加载一次

  return {
    async screenshot(opts) {
      // 预调整至 targetImageSize，使 API 转码器早返回
      // 无服务器端缩放 → scaleCoord 保持一致
      const d = cu.display.getSize(opts.displayId)
      const [targetW, targetH] = computeTargetDims(d.width, d.height, d.scaleFactor)
      return drainRunLoop(() =>
        cu.screenshot.captureExcluding(withoutTerminal(opts.allowedBundleIds), ...)
      )
    },

    async click(x, y, button, count, modifiers?) {
      const input = requireComputerUseInput()  // 惰性加载
      await moveAndSettle(input, x, y)         // 瞬移 + 50ms 沉降
      // ... 修饰键包装
    },

    async key(keySequence, repeat?) {
      // xdotool 风格："ctrl+shift+a" → 按 '+' 分割 → keys()
      // 裸 Escape：通知 CGEventTap 不要中止
      const parts = keySequence.split('+')
      await drainRunLoop(async () => {
        for (let i = 0; i < n; i++) {
          if (isBareEscape(parts)) notifyExpectedEscape()
          await input.keys(parts)
        }
      })
    },
  }
}
```

### CFRunLoop 挑战

最独特的工程细节：`drainRunLoop()`。在 macOS 上，原生 GUI 操作派发到主线程的 CFRunLoop。在终端应用中（无 NSRunLoop），这些事件会排队但永远不会被处理。解决方案是手动泵送：

```typescript
// drainRunLoop 包装派发到主队列的异步操作。
// 没有泵送，来自 Rust/Swift 原生模块的鼠标/键盘调用
// 在终端上下文中会永远挂起。
await drainRunLoop(async () => {
  await cu.screenshot.captureExcluding(...)
})
```

这就是 Computer Use 仅限 macOS 的原因：与 AppKit、CGEvent 和 SCContentFilter 的紧密集成需要仅在 Apple 事件模型内工作的原生 Swift 和 Rust 模块。

### AppState 中的状态

Computer Use 状态存储在 `AppState.computerUseMcpState` 中：

```typescript
computerUseMcpState?: {
  allowedApps?: readonly { bundleId, displayName, grantedAt }[]
  grantFlags?: { clipboardRead, clipboardWrite, systemKeyCombos }
  lastScreenshotDims?: { width, height, displayWidth, displayHeight, ... }
  hiddenDuringTurn?: ReadonlySet<string>
  selectedDisplayId?: number
  displayPinnedByModel?: boolean
}
```

此状态为**会话范围**（不跨恢复持久化），追踪应用允许列表、截图尺寸（用于坐标映射）以及当前回合被隐藏的应用（回合结束时通过 `cleanup.ts` 取消隐藏）。

---

## 可迁移设计模式

> 以下模式可直接应用于其他智能体系统或 CLI 工具。

### 模式 1："35 行替代一个库"

**场景**：你需要 React 应用中的全局状态管理。

**实践**：在引入 Redux/Zustand/Jotai 之前先问：你真的需要中间件、devtools 或计算选择器吗？如果答案是否定的，一个带 `getState`/`setState`/`subscribe` 的 `createStore` 函数 —— 通过 `useSyncExternalStore` 集成 —— 能在 40 行内提供相同的并发安全渲染保证。

### 模式 2：浏览器事件模型用于非浏览器环境

**场景**：你的终端/嵌入式 UI 有重叠的交互区域（模态框、嵌套滚动器、焦点上下文）。

**实践**：实现 W3C 捕获/冒泡派发模型。三阶段模型（捕获 → 目标 → 冒泡）配合 `stopPropagation()` 和优先级层级，能解决临时方案难以应付的事件路由问题。

### 模式 3：非浏览器环境中的虚拟滚动

**场景**：你需要在固定高度视口中显示数千条项目。

**实践**：仅渲染视口 + 缓冲区内的项目。使用高度估算配合测量缓存。实现滚动钳制以防止快速滚动时出现空白屏幕。

### 模式 4：打包类型化数组实现无 GC 渲染

**场景**：你在做逐帧的网格/单元格操作，其中对象分配导致 GC 暂停。

**实践**：使用位移将多个字段打包到类型化数组中。在同一 `ArrayBuffer` 上使用双视图，用于逐元素访问（Int32Array）和批量操作（BigInt64Array）。将字符串驻留为整数池。

### 模式 5：纯函数状态机用于编辑器模式

**场景**：你需要一个带可组合命令的多模式文本编辑器。

**实践**：将每种模式建模为状态类型的可区分联合体成员。转换函数是纯函数：`(state, input, ctx) → { next?, execute? }`。持久状态（寄存器、上次命令）存在于瞬态命令状态之外。

---

## 组件总览

| 组件 | 关键文件 | 大小 | 职责 |
|------|---------|------|------|
| Ink Fork | `ink/` (48 文件) | ~620KB | 自定义终端渲染引擎 |
| 协调器 | `ink/reconciler.ts` | 14.6KB | React 19 ↔ 终端桥梁 |
| 屏幕缓冲 | `ink/screen.ts` | 49.3KB | 打包 Int32Array 双缓冲单元格 |
| 事件系统 | `ink/events/` | ~15KB | W3C 捕获/冒泡 + 优先级派发 |
| Store | `state/store.ts` | 836B | 35 行全局状态管理 |
| AppState | `state/AppStateStore.ts` | 21.8KB | 570 行应用状态类型 |
| REPL 屏幕 | `screens/REPL.tsx` | 874KB | 主交互界面 |
| 虚拟滚动 | `VirtualMessageList.tsx` | 148KB | 高度缓存虚拟滚动 |
| Vim 模式 | `vim/` 目录 | ~50KB | 完整 Vim 状态机 |
| 键绑定 | `keybindings/` | ~40KB | 多上下文和弦键绑定 |
| Computer Use | `utils/computerUse/` | ~125KB | macOS 原生屏幕/输入控制 |

**UI 与状态管理总面积：约 2MB 的渲染、交互和状态基础设施。**

---

*下一集：第 15 集 — 服务与 API 层*

[← 第 13 集 — Bridge 系统](./13-bridge-system) · [第 15 集 — 服务与 API 层 →](./15-services-api-layer)
