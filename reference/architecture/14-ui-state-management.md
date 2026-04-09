# Episode 14: UI & State Management — A Browser in Your Terminal

> **Source files**: `ink/` directory — 48 files, ~620KB total. Core: `ink.tsx` (252KB), `reconciler.ts` (14.6KB), `renderer.ts` (7.7KB), `dom.ts` (15.1KB), `screen.ts` (49.3KB), `events/dispatcher.ts` (6KB), `focus.ts` (5.1KB). State: `state/store.ts` (836 bytes), `state/AppStateStore.ts` (21.8KB), `state/AppState.tsx` (23.5KB), `state/onChangeAppState.ts` (6.2KB). Screens: `screens/REPL.tsx` (874KB), `screens/Doctor.tsx` (71KB)
>
> **One-liner**: Claude Code runs a fully forked Ink rendering engine — React 19 + ConcurrentRoot, W3C-style capture/bubble events, Yoga Flexbox layout, packed Int32Array double-buffered screens, and a 35-line Zustand replacement — all inside your terminal.

---

## 1. The Terminal UI Tech Stack

Most CLI tools paint text line by line. Claude Code builds an entire component-based UI framework in the terminal — and the stack is surprisingly deep:

```
┌─────────────────────────────────────────────────────────────┐
│  React 19 (ConcurrentRoot via react-reconciler)             │
│    └─ Custom Ink Reconciler (reconciler.ts, 513 lines)      │
│        └─ Virtual DOM (dom.ts — ink-root/box/text/link/...) │
│            └─ Yoga Layout Engine (Flexbox in Terminal)       │
│                └─ Screen Buffer (screen.ts — packed Int32s)  │
│                    └─ ANSI Diff (log-update.ts → stdout)    │
└─────────────────────────────────────────────────────────────┘
```

### Key File Index

| Layer | File | Size | Role |
|-------|------|------|------|
| Entry | `ink/root.ts` | 4.6KB | Create Ink instance, mount React tree |
| Reconciler | `ink/reconciler.ts` | 14.6KB | React 19 host config, commit hooks |
| Renderer | `ink/renderer.ts` | 7.7KB | Yoga layout → Screen buffer |
| DOM | `ink/dom.ts` | 15.1KB | Virtual DOM nodes, dirty marking |
| Screen | `ink/screen.ts` | 49.3KB | Packed Int32Array cell buffer |
| Core | `ink/ink.tsx` | 252KB | Frame scheduling, input, selection |
| Events | `ink/events/dispatcher.ts` | 6KB | W3C capture/bubble dispatch |
| Focus | `ink/focus.ts` | 5.1KB | FocusManager with stack restore |
| Output | `ink/log-update.ts` | 27.2KB | ANSI diff, cursor management |

### The Complete Rendering Pipeline

```
stdin raw bytes
  → parse-keypress.ts: decode to ParsedKey (xterm/VT sequences)
  → InputEvent creation
  → Dispatcher.dispatchDiscrete(): W3C capture → target → bubble
  → React state update → Reconciler commit phase
  → resetAfterCommit() → rootNode.onComputeLayout() [Yoga]
  → rootNode.onRender() → renderer.ts generates Screen buffer
  → log-update.ts: diff prev vs next Screen → ANSI escape sequences
  → process.stdout.write()
```

Every keypress walks this entire pipeline. At 16ms frame throttle, Claude Code maintains 60fps-equivalent rendering in the terminal.

---

## 2. Why Fork Ink

Claude Code doesn't use the npm `ink` package. It maintains a complete fork with at least seven major modifications. Understanding why reveals the engineering ambition behind the project.

### The Modification Manifest

| Change | Original Ink | Forked Ink | Why |
|--------|-------------|------------|-----|
| React version | LegacyRoot | **ConcurrentRoot (React 19)** | Concurrent features, `useSyncExternalStore`, transitions |
| Event system | Basic `useInput` | **W3C capture/bubble dispatcher** | Complex overlapping focus contexts |
| Screen mode | Normal scrollback | **Alt-screen + mouse tracking** | Full-screen TUI with no scrollback pollution |
| Rendering | Single buffer | **Double-buffered + packed Int32 screens** | Zero-flicker rendering, CJK/emoji support |
| Text selection | None | **Mouse drag selection + clipboard** | Copy code from terminal output |
| Scrolling | Full re-render | **Virtual scroll with height cache** | 1000+ messages without performance cliff |
| Search | None | **Full-screen search with per-cell highlight** | Find text across entire conversation |

### React 19 Reconciler: The Bridge Between React and Terminal

```typescript
// Source: ink/reconciler.ts:224-506
const reconciler = createReconciler<
  ElementNames,  // 'ink-root' | 'ink-box' | 'ink-text' | ...
  Props,
  DOMElement,    // Virtual DOM node
  DOMElement,    // Container type
  TextNode,      // Text node type
  DOMElement,    // Suspense boundary
  unknown, unknown, DOMElement,
  HostContext,
  null,          // UpdatePayload — NOT used in React 19
  NodeJS.Timeout,
  -1, null
>({
  // React 19 commitUpdate — receives old/new props directly
  // (no updatePayload like React 18)
  commitUpdate(node, _type, oldProps, newProps) {
    const props = diff(oldProps, newProps)
    const style = diff(oldProps['style'], newProps['style'])
    // Incremental update: only changed attributes + styles
    if (props) { /* apply prop changes */ }
    if (style && node.yogaNode) { applyStyles(node.yogaNode, style, ...) }
  },

  // The magic hook: after every React commit, recompute layout + render
  resetAfterCommit(rootNode) {
    rootNode.onComputeLayout?.()  // Yoga flexbox calculation
    rootNode.onRender?.()         // Paint to screen buffer → stdout
  },
})
```

The `UpdatePayload` generic is `null` — a React 19 signature. In React 18, the reconciler pre-computed a diff payload in `prepareUpdate()` and passed it to `commitUpdate()`. React 19 eliminated this intermediate step, passing old and new props directly. This is one of the clearest signals that Claude Code is built on cutting-edge React internals.

---

## 3. The Rendering Pipeline In Depth

### DOM Node Structure

Every UI element becomes a `DOMElement` node in the virtual tree:

```typescript
// Source: ink/dom.ts:31-91
type DOMElement = {
  nodeName: ElementNames           // 'ink-root' | 'ink-box' | 'ink-text' | ...
  attributes: Record<string, DOMNodeAttribute>
  childNodes: DOMNode[]
  parentNode: DOMElement | undefined
  yogaNode?: LayoutNode            // Yoga flexbox layout node
  style: Styles                    // Flexbox properties (width, flex, padding...)
  dirty: boolean                   // Needs re-rendering

  // Event handlers — stored separately from attributes so handler
  // identity changes don't mark dirty and defeat blit optimization
  _eventHandlers?: Record<string, unknown>

  // Scroll state for overflow: 'scroll' boxes
  scrollTop?: number
  pendingScrollDelta?: number      // Accumulated delta, drained per-frame
  scrollClampMin?: number          // Virtual scroll clamp bounds
  scrollClampMax?: number
  stickyScroll?: boolean           // Auto-pin to bottom
  scrollAnchor?: { el: DOMElement; offset: number }  // Deferred position read

  // Focus management (root node only)
  focusManager?: FocusManager
}
```

Seven element types map the terminal UI vocabulary:

| Type | Purpose | Has Yoga Node? |
|------|---------|---------------|
| `ink-root` | Tree root | ✅ |
| `ink-box` | Flexbox container (`<Box>`) | ✅ |
| `ink-text` | Text content (`<Text>`) | ✅ (with measure func) |
| `ink-virtual-text` | Nested text inside `<Text>` | ❌ |
| `ink-link` | Terminal hyperlink (OSC 8) | ❌ |
| `ink-progress` | Progress bar | ❌ |
| `ink-raw-ansi` | Pre-rendered ANSI passthrough | ✅ (fixed dimensions) |

### The Screen Buffer: Packed Int32Array

This is where Claude Code gets *seriously* performance-conscious. Instead of allocating Cell objects (which would mean 24,000 objects for a 200×120 screen), the screen stores cells as packed integers:

```typescript
// Source: ink/screen.ts:332-348
// Each cell = 2 consecutive Int32 elements:
//   word0 (cells[ci]):     charId (full 32 bits, index into CharPool)
//   word1 (cells[ci + 1]): styleId[31:17] | hyperlinkId[16:2] | width[1:0]

const STYLE_SHIFT = 17
const HYPERLINK_SHIFT = 2
const HYPERLINK_MASK = 0x7fff  // 15 bits
const WIDTH_MASK = 3           // 2 bits (Narrow/Wide/SpacerTail/SpacerHead)

function packWord1(styleId: number, hyperlinkId: number, width: number): number {
  return (styleId << STYLE_SHIFT) | (hyperlinkId << HYPERLINK_SHIFT) | width
}
```

The `cells64` BigInt64Array view over the same ArrayBuffer enables 8-byte bulk fills via `cells64.fill(0n)` — one operation to clear an entire screen instead of iterating every cell.

**String interning** further reduces memory pressure:

```typescript
// Source: ink/screen.ts:21-53
class CharPool {
  private ascii: Int32Array = initCharAscii()  // Fast path for ASCII

  intern(char: string): number {
    if (char.length === 1) {
      const code = char.charCodeAt(0)
      if (code < 128) {
        const cached = this.ascii[code]!
        if (cached !== -1) return cached  // Direct array lookup, no Map.get
        // ...
      }
    }
    // Fall back to Map for non-ASCII (CJK, emoji)
    return this.stringMap.get(char) ?? this.addNew(char)
  }
}
```

### Double Buffering

The renderer maintains two `Frame` objects — `frontFrame` and `backFrame`. Each frame holds a `Screen`. On every render:

1. Reset the **back buffer** (via `resetScreen()` — a single `cells64.fill(0n)` call)
2. Render the DOM tree into the back buffer
3. Diff against the **front buffer** to produce minimal ANSI output
4. Swap: back becomes front for the next frame

The `prevFrameContaminated` flag tracks when the front buffer was mutated post-render (e.g., selection overlay). When contaminated, the renderer skips the blit optimization and does a full repaint — but only for that one frame.

### Frame Scheduling

```
// Source: ink/ink.tsx — throttled at 16ms
onRender → scheduleRender()
  → setTimeout(doRender, 16)  // ~60fps throttle
  → batchRender: coalesce multiple state changes into one frame
```

---

## 4. The Event System: W3C in a Terminal

Perhaps the most surprising engineering decision: Claude Code implements a complete W3C-style event dispatch system for terminal events. This isn't academic purity — it's a practical necessity when you have overlapping dialogs, nested scroll boxes, and a Vim mode that needs to intercept keys at different tree depths.

### Event Dispatch Phases

```typescript
// Source: ink/events/dispatcher.ts:46-78
function collectListeners(target, event): DispatchListener[] {
  const listeners: DispatchListener[] = []
  let node = target

  while (node) {
    const isTarget = node === target

    // Capture handlers: unshift → root-first order
    const captureHandler = getHandler(node, event.type, true)
    if (captureHandler) {
      listeners.unshift({
        node, handler: captureHandler,
        phase: isTarget ? 'at_target' : 'capturing'
      })
    }

    // Bubble handlers: push → target-first order
    const bubbleHandler = getHandler(node, event.type, false)
    if (bubbleHandler && (event.bubbles || isTarget)) {
      listeners.push({
        node, handler: bubbleHandler,
        phase: isTarget ? 'at_target' : 'bubbling'
      })
    }

    node = node.parentNode
  }
  return listeners
  // Result: [root-cap, ..., parent-cap, target-cap, target-bub, parent-bub, ..., root-bub]
}
```

### Event Priorities: Mirroring react-dom

```typescript
// Source: ink/events/dispatcher.ts:122-138
function getEventPriority(eventType: string): number {
  switch (eventType) {
    case 'keydown': case 'keyup': case 'click':
    case 'focus': case 'blur': case 'paste':
      return DiscreteEventPriority     // Synchronous flush
    case 'resize': case 'scroll': case 'mousemove':
      return ContinuousEventPriority   // Batchable
    default:
      return DefaultEventPriority
  }
}
```

This maps directly to React's scheduler priorities. A keypress triggers a synchronous React update (discrete priority), while a scroll event gets batched (continuous priority). The `Dispatcher` class bridges this into the reconciler:

```typescript
// Source: ink/reconciler.ts:510
// Wire the reconciler's discreteUpdates into the dispatcher.
// This breaks the import cycle: dispatcher.ts doesn't import reconciler.ts.
dispatcher.discreteUpdates = reconciler.discreteUpdates.bind(reconciler)
```

### Focus Management: Stack-Based Restore

```typescript
// Source: ink/focus.ts:15-82
class FocusManager {
  activeElement: DOMElement | null = null
  private focusStack: DOMElement[] = []  // Max 32 entries

  focus(node) {
    if (node === this.activeElement) return
    const previous = this.activeElement
    if (previous) {
      // Deduplicate before pushing (prevents unbounded growth from Tab cycling)
      const idx = this.focusStack.indexOf(previous)
      if (idx !== -1) this.focusStack.splice(idx, 1)
      this.focusStack.push(previous)
      if (this.focusStack.length > MAX_FOCUS_STACK) this.focusStack.shift()
      this.dispatchFocusEvent(previous, new FocusEvent('blur', node))
    }
    this.activeElement = node
    this.dispatchFocusEvent(node, new FocusEvent('focus', previous))
  }

  // When a dialog closes, focus automatically returns to the previous element
  handleNodeRemoved(node, root) {
    this.focusStack = this.focusStack.filter(n => n !== node && isInTree(n, root))
    // ... restore focus to most recent still-mounted element
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

The focus stack has a hard cap of 32 entries (`MAX_FOCUS_STACK`). Tab cycling deduplicates before pushing, preventing the stack from growing with repeated navigation. When a dialog is removed from the tree, the reconciler calls `handleNodeRemoved()`, which walks the stack backward to find the most recent still-mounted element — giving users automatic focus restoration without explicit teardown logic.

---

## 5. The 35-Line Store (Replacing Redux/Zustand)

This is the kind of engineering decision that makes you pause. Instead of reaching for a state management library, Claude Code implements its entire application state in exactly 35 lines of code:

```typescript
// Source: state/store.ts — COMPLETE FILE (35 lines)
type Listener = () => void
type OnChange<T> = (args: { newState: T; oldState: T }) => void

export type Store<T> = {
  getState: () => T
  setState: (updater: (prev: T) => T) => void
  subscribe: (listener: Listener) => () => void
}

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
      if (Object.is(next, prev)) return   // Reference equality skip
      state = next
      onChange?.({ newState: next, oldState: prev })  // Side-effect hook
      for (const listener of listeners) listener()    // Notify subscribers
    },

    subscribe: (listener: Listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}
```

That's it. No middleware chains, no devtools integration, no action types, no reducers. Just `getState`, `setState` (with an updater function), and `subscribe`. The `Object.is` check prevents no-op re-renders. The `onChange` callback centralizes side effects.

### React Integration via useSyncExternalStore

```typescript
// Source: state/AppState.tsx:142-163
export function useAppState<T>(selector: (state: AppState) => T): T {
  const store = useAppStore()
  const get = () => selector(store.getState())
  return useSyncExternalStore(store.subscribe, get, get)
}

// Usage in components:
const verbose = useAppState(s => s.verbose)
const model = useAppState(s => s.mainLoopModel)
```

The `useSyncExternalStore` hook (React 18+) guarantees tear-free reads during concurrent rendering — the same primitive Zustand uses internally. Claude Code just doesn't need Zustand's wrapper.

### AppState: The Full Application State Type

`AppStateStore.ts` defines the `AppState` type — **570 lines** of typed state covering every aspect of the application:

```typescript
// Source: state/AppStateStore.ts:89-452 (condensed)
export type AppState = DeepImmutable<{
  // === Session Settings ===
  settings: SettingsJson
  mainLoopModel: ModelSetting
  verbose: boolean
  thinkingEnabled: boolean | undefined
  effortValue?: EffortValue

  // === UI Display State ===
  expandedView: 'none' | 'tasks' | 'teammates'
  isBriefOnly: boolean
  footerSelection: FooterItem | null
  activeOverlays: ReadonlySet<string>
  spinnerTip?: string

  // === Permission System ===
  toolPermissionContext: ToolPermissionContext

  // === Remote / Bridge ===
  remoteSessionUrl: string | undefined
  remoteConnectionStatus: 'connecting' | 'connected' | 'reconnecting' | 'disconnected'
  replBridgeEnabled: boolean
  replBridgeConnected: boolean

  // === Speculative Execution ===
  speculation: SpeculationState
  promptSuggestion: { text, promptId, shownAt, ... }
}> & {
  // === Mutable state (excluded from DeepImmutable) ===
  tasks: { [taskId: string]: TaskState }
  agentNameRegistry: Map<string, AgentId>
  mcp: { clients, tools, commands, resources }
  plugins: { enabled, disabled, commands, errors }
  teamContext?: { teamName, teammates, ... }
  inbox: { messages: Array<...> }
  fileHistory: FileHistoryState
  attribution: AttributionState
  // ... Computer Use, REPL context, Tungsten, etc.
}
```

The `DeepImmutable<>` wrapper prevents accidental mutation for most fields. Fields containing `Map`, `Set`, function types, or task state are excluded from the wrapper via the intersection (`&`) — a pragmatic compromise between type safety and expressiveness.

### Side-Effect Centralization

All state change side effects funnel through a single `onChangeAppState` callback:

```typescript
// Source: state/onChangeAppState.ts:43-171 (condensed)
export function onChangeAppState({ newState, oldState }) {
  // Permission mode → sync to CCR/SDK
  if (prevMode !== newMode) {
    notifySessionMetadataChanged({ permission_mode: newExternal })
    notifyPermissionModeChanged(newMode)
  }

  // Model change → persist to settings file
  if (newState.mainLoopModel !== oldState.mainLoopModel) {
    updateSettingsForSource('userSettings', { model: newState.mainLoopModel })
    setMainLoopModelOverride(newState.mainLoopModel)
  }

  // Expanded view → persist to globalConfig
  if (newState.expandedView !== oldState.expandedView) {
    saveGlobalConfig(current => ({
      ...current,
      showExpandedTodos: newState.expandedView === 'tasks',
    }))
  }

  // Settings change → clear auth caches + re-apply env vars
  if (newState.settings !== oldState.settings) {
    clearApiKeyHelperCache()
    clearAwsCredentialsCache()
    if (newState.settings.env !== oldState.settings.env) {
      applyConfigEnvironmentVariables()
    }
  }
}
```

This is the "single choke point" pattern — eight different code paths can change the permission mode (Shift+Tab cycling, plan mode exit, bridge commands, slash commands...), but they all flow through this one diff. Before this was centralized, each path had to manually notify CCR, and several didn't — leaving the web UI out of sync.

---

## 6. REPL Screen Architecture

`screens/REPL.tsx` (874KB) is the application's main interface — a single React function component that orchestrates every user-facing feature. At ~12,000 lines of compiled output, it's the largest single component in the codebase.

### Component Hierarchy

```
<REPL>
  <KeybindingSetup>                // Initialize keybinding system
    <AlternateScreen>              // Enter terminal alt-screen mode
      <FullscreenLayout>           // Full-screen layout (ScrollBox + bars)
        <ScrollBox stickyScroll>   // Scrollable main content area
          <VirtualMessageList>     // Virtual scroll for 1000+ messages
            <Messages>             // Message rendering (recursive)
          </VirtualMessageList>
        </ScrollBox>
        <StatusLine>               // model │ permission │ cwd │ tokens │ cost
        <PromptInput>              // User input + autocomplete + footer pills
      </FullscreenLayout>
    </AlternateScreen>

    // Overlay dialogs (rendered outside FullscreenLayout)
    <PermissionRequest>            // Tool permission confirmation
    <ModelPicker>                  // Model selection (Meta+P)
    <ThemePicker>                  // Theme selection
    <GlobalSearchDialog>           // Full-text search (Ctrl+F)
    <MessageSelector>              // Message replay selector
    <ExportDialog>                 // Session export
    <FeedbackSurvey>               // Feedback survey
    // ... 15+ more overlay dialogs
  </KeybindingSetup>
</REPL>
```

### Three Screen Components

| Screen | File | Size | Purpose |
|--------|------|------|---------|
| REPL | `screens/REPL.tsx` | 874KB | Main interactive loop |
| Doctor | `screens/Doctor.tsx` | 71KB | Environment diagnostics (`/doctor`) |
| ResumeConversation | `screens/ResumeConversation.tsx` | 58KB | Session restore (`--resume`) |

### The Query Loop Flow

```
User input → handleSubmit()
  → Create UserMessage → addToHistory()
  → query({ messages, tools, onMessage, ... })
    → Streaming callback: handleMessageFromStream()
      → setMessages(prev => [...prev, newMessage])
      → Tool call → useCanUseTool → permission check
        → allow → execute tool → append result
        → deny → append rejection message
    → Complete → record analytics → save session
```

---

## 7. Virtual Scrolling & Height Cache

When a conversation grows to hundreds of messages, rendering every message on every frame would destroy performance. Claude Code implements terminal virtual scrolling — a technique borrowed from browser virtual list libraries like `react-window`.

### Core Strategy

```
┌────────────────────────────────┐
│  Spacer (estimated height)     │  ← Not rendered, fixed-height Box
│                                │
├────────────────────────────────┤
│  Buffer zone (1 screen above)  │  ← Rendered but off-screen
├────────────────────────────────┤
│  ████████████████████████████  │
│  ████ Visible viewport ██████  │  ← Actually visible to user
│  ████████████████████████████  │
├────────────────────────────────┤
│  Buffer zone (1 screen below)  │  ← Rendered but off-screen
├────────────────────────────────┤
│  Spacer (estimated height)     │  ← Not rendered, fixed-height Box
└────────────────────────────────┘
```

### VirtualMessageList API

```typescript
// Source: components/VirtualMessageList.tsx
type JumpHandle = {
  jumpToIndex: (i: number) => void       // Jump to message by index
  setSearchQuery: (q: string) => void    // Set search filter
  nextMatch: () => void                  // Navigate to next match
  prevMatch: () => void                  // Navigate to previous match
  warmSearchIndex: () => Promise<number> // Pre-build search index
  disarmSearch: () => void               // Clear search position
}
```

### Key Design Decisions

- **WeakMap height cache**: Each message's rendered height is cached in a WeakMap keyed by the message object. When the message reference doesn't change, the height is reused without re-measurement.

- **Window = viewport + 1 screen buffer**: Only messages within the visible viewport plus one screen height above and below are actually rendered. Everything else becomes `<Box height={N}>` spacers.

- **Scroll clamp bounds**: `scrollClampMin`/`scrollClampMax` on the DOM element prevent the scroll position from entering un-rendered territory. If the user scrolls faster than React can re-render, the renderer holds at the edge of mounted content instead of showing blank space.

- **Sticky scroll to bottom**: New messages auto-scroll to bottom via `stickyScroll`. The scroll pins to bottom unless the user explicitly scrolls up.

- **Search index**: Full-text search builds a cached plain-text index of all messages. The search highlight is applied at the screen buffer level (per-cell style overlay), not via React re-rendering.

### ScrollBox: The Scroll Container

```typescript
// Source: ink/components/ScrollBox.tsx
type ScrollBoxHandle = {
  scrollTo(y: number): void
  scrollBy(dy: number): void
  scrollToElement(el, offset?): void  // Deferred to render time
  scrollToBottom(): void
  isSticky(): boolean                  // Following bottom?
  setClampBounds(min?, max?): void     // Virtual scroll limits
}
```

The `pendingScrollDelta` accumulator drains at `SCROLL_MAX_PER_FRAME` rows per frame — so fast flicks show intermediate frames instead of one jarring jump. Direction reversal naturally cancels (pure accumulator, no target tracking).

---

## 8. Vim Mode State Machine

Claude Code includes a complete Vim editing mode for the input box — not a subset, but a full implementation with operators, motions, text objects, registers, and dot-repeat.

### State Machine Architecture

```typescript
// Source: vim/ directory
type VimState =
  | { mode: 'INSERT'; insertedText: string }
  | { mode: 'NORMAL'; command: CommandState }

type CommandState =
  | { type: 'idle' }                                  // Waiting for input
  | { type: 'count'; digits: string }                 // Prefix count (3dw)
  | { type: 'operator'; op: Operator; count }         // Waiting for motion (d_)
  | { type: 'operatorCount'; op, count, digits }      // Operator + count (d3w)
  | { type: 'operatorFind'; op, count, find }         // Operator + find (df_)
  | { type: 'operatorTextObj'; op, count, scope }     // Operator + text obj (diw)
  | { type: 'find'; find: FindType; count }           // f/F/t/T waiting for char
  | { type: 'g'; count }                              // g prefix commands
  | { type: 'replace'; count }                        // r waiting for replacement
  | { type: 'indent'; dir: '>' | '<'; count }         // >> / << indentation
```

### State Transition Diagram

```
  idle ──┬─[d/c/y]──► operator ──┬─[motion]──► execute
         ├─[1-9]────► count      ├─[0-9]────► operatorCount
         ├─[fFtT]───► find       ├─[ia]─────► operatorTextObj
         ├─[g]──────► g          └─[fFtT]───► operatorFind
         ├─[r]──────► replace
         └─[><]─────► indent
```

### Pure Function Transitions

The transition function is a pure function — no side effects, deterministic output:

```typescript
function transition(state, input, ctx): TransitionResult {
  switch (state.type) {
    case 'idle':     return fromIdle(input, ctx)
    case 'count':    return fromCount(state, input, ctx)
    case 'operator': return fromOperator(state, input, ctx)
    // ... exhaustive switch guaranteed by TypeScript
  }
}
// Returns: { next?: CommandState; execute?: () => void }
```

### Persistent State (Cross-Command Memory)

```typescript
type PersistentState = {
  lastChange: RecordedChange | null  // Dot-repeat (.)
  lastFind: { type, char } | null   // Repeat find (;/,)
  register: string                   // Yank register content
  registerIsLinewise: boolean        // Was last yank line-wise?
}
```

### Supported Operations

| Category | Commands |
|----------|----------|
| **Movement** | `h/l/j/k`, `w/b/e/W/B/E`, `0/^/$`, `gg/G`, `gj/gk` |
| **Operators** | `d` (delete), `c` (change), `y` (yank), `>/<` (indent) |
| **Find** | `f/F/t/T` + char, `;/,` repeat |
| **Text objects** | `iw/aw`, `i"/a"`, `i(/a(`, `i{/a{`, `i[/a[`, `i</a<` |
| **Commands** | `x`, `~`, `r`, `J`, `p/P`, `D/C/Y`, `o/O`, `u` (undo), `.` (repeat) |
| **Dot repeat** | Records insert text, operators, replacements, toggleCase, indent |

The `VimTextInput.tsx` (16KB) component integrates this state machine with the input box: Normal mode intercepts keystrokes and routes them through `transition()`, while Insert mode passes through to normal text editing.

---

## 9. Keybinding System

Claude Code's keybinding system supports multiple contexts, Emacs-style chord sequences, user customization, and reserved shortcuts — a full keyboard layer on top of the event system.

### Architecture

```
keybindings/
├── defaultBindings.ts          # Default key bindings per context
├── loadUserBindings.ts         # Load from ~/.claude/keybindings.json
├── schema.ts                   # JSON schema for user bindings
├── parser.ts                   # "ctrl+shift+k" → structured data
├── match.ts                    # Match keystroke against bindings
├── resolver.ts                 # Resolution engine (with chord support)
├── KeybindingContext.tsx        # React Context provider
├── KeybindingProviderSetup.tsx  # Init + chord interceptor
├── useKeybinding.ts            # Consumer hook
├── validate.ts                 # User binding validation
├── reservedShortcuts.ts        # Non-rebindable shortcuts
└── shortcutFormat.ts           # Display formatting
```

### Context-Based Binding Resolution

Each binding belongs to a **context** that determines when it's active:

```typescript
// Source: keybindings/defaultBindings.ts (condensed)
const DEFAULT_BINDINGS: KeybindingBlock[] = [
  {
    context: 'Global',              // Always active
    bindings: {
      'ctrl+c': 'app:interrupt',
      'ctrl+d': 'app:exit',
      'ctrl+l': 'app:redraw',
      'ctrl+t': 'app:toggleTodos',
      'ctrl+r': 'history:search',
    }
  },
  {
    context: 'Chat',                // When input box is focused
    bindings: {
      'escape': 'chat:cancel',
      'shift+tab': 'chat:cycleMode',
      'meta+p': 'chat:modelPicker',
      'enter': 'chat:submit',
      'ctrl+x ctrl+e': 'chat:externalEditor',  // Chord!
      'ctrl+x ctrl+k': 'chat:killAgents',      // Chord!
    }
  },
  {
    context: 'Scroll',              // When scrolled from bottom
    bindings: {
      'pageup': 'scroll:pageUp',
      'wheelup': 'scroll:lineUp',
      'ctrl+shift+c': 'selection:copy',
    }
  },
  // ... Confirmation, Settings, Transcript, etc.
]
```

### Chord Support (Emacs-Style Multi-Key Sequences)

```typescript
// User presses ctrl+x → enters "chord pending" state
// Display shows "ctrl+x ..." prompt
// User presses ctrl+e → matches 'ctrl+x ctrl+e' → 'chat:externalEditor'
// User presses other key → chord cancelled, key processed normally

type ChordResolveResult =
  | { type: 'match'; action: string }         // Complete match
  | { type: 'chord_started'; pending: ... }   // Chord in progress
  | { type: 'chord_cancelled' }               // Wrong second key
  | { type: 'unbound' }                       // Explicitly unbound
  | { type: 'none' }                          // No binding found
```

### Using Keybindings in Components

```typescript
// Single binding
useKeybinding('app:toggleTodos', () => {
  setShowTodos(prev => !prev)
}, { context: 'Global' })

// Multiple bindings
useKeybindings({
  'chat:submit': () => handleSubmit(),
  'chat:cancel': () => handleCancel(),
}, { context: 'Chat' })
```

### User Customization

Users can override any non-reserved binding via `~/.claude/keybindings.json`. The file is validated against a Zod schema, and invalid entries produce warnings without breaking the application.

---

## 10. Computer Use Integration

Claude Code integrates Anthropic's Computer Use capability — the ability for the model to see the screen, move the mouse, type on the keyboard, and control applications. This is a fundamentally different kind of tool: instead of text-in/text-out, it operates on pixels and input events.

### Architecture Overview

```
utils/computerUse/
├── executor.ts       (23.8KB)  # CLI ComputerExecutor implementation
├── wrapper.tsx       (49.4KB)  # MCP server wrapper + screenshot state
├── toolRendering.tsx (17.7KB)  # UI components for CU tool results
├── common.ts         (2.6KB)   # CLI capabilities + terminal detection
├── gates.ts          (2.6KB)   # Feature gating (Chicago MCP)
├── setup.ts          (2KB)     # Initialization
├── hostAdapter.ts    (2.8KB)   # Host abstraction (CLI vs Cowork)
├── inputLoader.ts    (1.2KB)   # Lazy-load @ant/computer-use-input
├── swiftLoader.ts    (925B)    # Lazy-load @ant/computer-use-swift
├── computerUseLock.ts(7.1KB)   # Exclusive lock for CU sessions
├── escHotkey.ts      (2KB)     # CGEventTap abort via Escape
├── drainRunLoop.ts   (2.8KB)   # CFRunLoop pump for macOS
├── cleanup.ts        (3.3KB)   # Turn-end unhide
├── appNames.ts       (6.6KB)   # Application name resolution
└── mcpServer.ts      (4.1KB)   # MCP server definition
```

### How It Differs from Regular Tools

Regular tools (`BashTool`, `FileEditTool`, etc.) use `tool_use` blocks in the API response. Computer Use uses `server_tool_use` — a different protocol where the server handles tool execution feedback natively:

| Aspect | Regular Tool | Computer Use Tool |
|--------|-------------|-------------------|
| API block type | `tool_use` | `server_tool_use` |
| Execution | CLI-side | CLI-side (screenshots) + server feedback |
| Input | Structured JSON | `{ action, coordinate?, text? }` |
| Output | Text result | JPEG screenshot (base64) |
| Platform | Cross-platform | **macOS only** (requires Swift + Rust native modules) |

### The Executor Pattern

```typescript
// Source: utils/computerUse/executor.ts:259-644
export function createCliExecutor(opts): ComputerExecutor {
  // Two native modules:
  //   @ant/computer-use-swift  — screenshots, app management, TCC
  //   @ant/computer-use-input  — mouse, keyboard (Rust/enigo)

  const cu = requireComputerUseSwift()  // Loaded once at factory time

  return {
    capabilities: { ...CLI_CU_CAPABILITIES, hostBundleId: CLI_HOST_BUNDLE_ID },

    async screenshot(opts) {
      // Pre-size to targetImageSize so API transcoder's early-return fires
      // No server-side resize → scaleCoord stays coherent
      const d = cu.display.getSize(opts.displayId)
      const [targetW, targetH] = computeTargetDims(d.width, d.height, d.scaleFactor)
      return drainRunLoop(() =>
        cu.screenshot.captureExcluding(withoutTerminal(opts.allowedBundleIds), ...)
      )
    },

    async click(x, y, button, count, modifiers?) {
      const input = requireComputerUseInput()  // Lazy-loaded
      await moveAndSettle(input, x, y)         // Instant move + 50ms settle
      if (modifiers?.length) {
        await drainRunLoop(() =>
          withModifiers(input, modifiers, () =>
            input.mouseButton(button, 'click', count)
          )
        )
      } else {
        await input.mouseButton(button, 'click', count)
      }
    },

    async key(keySequence, repeat?) {
      // xdotool-style: "ctrl+shift+a" → split on '+' → keys()
      // Bare Escape: notify CGEventTap so it doesn't abort
      const parts = keySequence.split('+')
      const isEsc = isBareEscape(parts)
      await drainRunLoop(async () => {
        for (let i = 0; i < n; i++) {
          if (isEsc) notifyExpectedEscape()
          await input.keys(parts)
        }
      })
    },

    // ... drag, scroll, type, clipboard, app management
  }
}
```

### The CFRunLoop Challenge

The most distinctive engineering detail: `drainRunLoop()`. On macOS, native GUI operations dispatch to the main thread's CFRunLoop. In a terminal app (no NSRunLoop), these events queue up and never resolve. The solution is a manual pump:

```typescript
// drainRunLoop wraps async operations that dispatch to the main queue.
// Without the pump, mouse/keyboard calls from the Rust/Swift native
// modules would hang forever in a terminal context.
await drainRunLoop(async () => {
  await cu.screenshot.captureExcluding(...)
})
```

This is why Computer Use is macOS-only: the tight integration with AppKit, CGEvent, and SCContentFilter requires native Swift and Rust modules that only work within Apple's event model.

### State in AppState

Computer Use state lives in `AppState.computerUseMcpState`:

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

This state is **session-scoped** (not persisted across resume) and tracks the app allowlist, screenshot dimensions for coordinate mapping, and which apps were hidden during the current turn (unhidden at turn end via `cleanup.ts`).

---

## Transferable Design Patterns

> The following patterns can be directly applied to other agentic systems or CLI tools.

### Pattern 1: "35 Lines to Replace a Library"

**Scenario**: You need application-wide state management in a React app.

**Practice**: Before reaching for Redux/Zustand/Jotai, ask: do you actually need middleware, devtools, or computed selectors? If the answer is no, a `createStore` function with `getState`/`setState`/`subscribe` — integrated via `useSyncExternalStore` — gives you the same concurrent-safe rendering guarantees in under 40 lines.

**Claude Code's application**: 570-line `AppState` type managed by a 35-line store. The `onChange` callback replaces middleware for side effects. The `Object.is` check replaces selector memoization for simple cases.

### Pattern 2: Browser Event Model in Non-Browser Environments

**Scenario**: Your terminal/embedded UI has overlapping interactive regions (modals, nested scrollers, focus contexts).

**Practice**: Implement the W3C capture/bubble dispatch model. The three-phase model (capture → target → bubble) with `stopPropagation()` and priority levels solves event routing problems that ad-hoc approaches struggle with.

**Claude Code's application**: A `Dispatcher` class with `dispatchDiscrete()` (sync priority for keys) and `dispatchContinuous()` (batched for scroll/resize), wired into React's scheduler via the reconciler's `resolveUpdatePriority`.

### Pattern 3: Virtual Scrolling in Non-Browser Environments

**Scenario**: You need to display thousands of items in a fixed-height viewport.

**Practice**: Only render items within the viewport + a buffer zone. Use height estimation with measurement caching. Implement scroll clamping to prevent blank screens during fast scrolling.

**Claude Code's application**: `VirtualMessageList` with WeakMap height cache, `scrollClampMin`/`scrollClampMax` for race protection between scroll position and React re-render, and `stickyScroll` for auto-following new content.

### Pattern 4: Packed Typed Arrays for GC-Free Rendering

**Scenario**: You're doing per-frame grid/cell operations where object allocation causes GC pauses.

**Practice**: Pack multiple fields into typed arrays using bit shifts. Use dual views over the same `ArrayBuffer` for per-element access (Int32Array) and bulk operations (BigInt64Array). Intern strings into integer pools.

**Claude Code's application**: `Screen.cells` packs char/style/hyperlink/width into 2 Int32s per cell. `CharPool` interns characters with an ASCII fast path (direct array lookup, no Map). Bulk clear via `cells64.fill(0n)`.

### Pattern 5: Pure-Function State Machine for Editor Modes

**Scenario**: You need a multi-mode text editor with composable commands.

**Practice**: Model each mode as a discriminated union member of the state type. Transition functions are pure: `(state, input, ctx) → { next?, execute? }`. Persistent state (registers, last command) lives outside the transient command state.

**Claude Code's application**: Vim mode with 10 command states, exhaustive switch in `transition()`, separate `PersistentState` for dot-repeat and yank registers. TypeScript's exhaustive pattern matching ensures every state is handled.

---

## Component Summary

| Component | Key Files | Size | Role |
|-----------|-----------|------|------|
| Ink Fork | `ink/` (48 files) | ~620KB | Custom terminal rendering engine |
| Reconciler | `ink/reconciler.ts` | 14.6KB | React 19 ↔ Terminal bridge |
| Screen Buffer | `ink/screen.ts` | 49.3KB | Packed Int32Array double-buffered cells |
| Event System | `ink/events/` | ~15KB | W3C capture/bubble + priority dispatch |
| Store | `state/store.ts` | 836B | 35-line global state management |
| AppState | `state/AppStateStore.ts` | 21.8KB | 570-line application state type |
| REPL Screen | `screens/REPL.tsx` | 874KB | Main interactive interface |
| Virtual Scroll | `VirtualMessageList.tsx` | 148KB | Height-cached virtual scrolling |
| Vim Mode | `vim/` directory | ~50KB | Full Vim state machine |
| Keybindings | `keybindings/` | ~40KB | Multi-context chord-enabled bindings |
| Computer Use | `utils/computerUse/` | ~125KB | macOS native screen/input control |

**Total UI & State surface: ~2MB of rendering, interaction, and state infrastructure.**

---

*Next up: Episode 15 — Services & API Layer*

[← Episode 13 — Bridge System](13-bridge-system.md) · [Episode 15 — Services & API Layer →](15-services-api-layer.md)
