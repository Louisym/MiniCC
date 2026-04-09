# 14 — UI & State Management: Building a Browser in Your Terminal

> **Scope**: `src/ink/` (49 files, ~600KB), `src/state/` (3 files, ~5KB), `src/screens/REPL.tsx` (874KB), `src/components/` (~200 files)
>
> **One-liner**: Claude Code ships a forked, rewritten Ink framework — React 19 Concurrent Rendering, a Yoga-powered flexbox layout engine, packed Int32Array double-buffered screens, W3C event dispatch, and all of it rendering at 60fps inside your terminal.

---

## 1. The Terminal UI Technology Stack

Most CLI tools use blessed or Ink for terminal UIs. Claude Code uses *neither* — it forks Ink and rewrites the core to build something that's closer to a browser rendering engine than a terminal TUI framework.

```
Layer 5: React Components         ← REPL.tsx (874K), PromptInput, StatusLine
Layer 4: React 19 Reconciler      ← reconciler.ts — ConcurrentRoot, not LegacyRoot
Layer 3: Custom DOM (ink-box)     ← dom.ts — virtual DOM nodes with event handlers
Layer 2: Yoga Layout Engine       ← Facebook's Flexbox-in-C, compiled to WASM
Layer 1: Screen Buffer (Int32)    ← screen.ts — packed typed arrays, zero GC
Layer 0: ANSI Diff → stdout       ← log-update.ts — only changed cells written
```

**Source coordinates**: `src/ink/reconciler.ts:224` — the `createReconciler()` call configures React 19's fiber architecture:

```typescript
// reconciler.ts:425 — explicit React 19 comment
// React 19 commitUpdate receives old and new props directly
// instead of an updatePayload
commitUpdate(node, _type, oldProps, newProps): void { ... }

// React 19 required methods
maySuspendCommit(): boolean { return false }
preloadInstance(): boolean { return true }
```

---

## 2. Why Fork Ink

The original Ink framework had fundamental limitations for a product-grade terminal application:

| Limitation | What Claude Code Needed | Solution |
|-----------|------------------------|----------|
| LegacyRoot renderer | Concurrent features, Suspense, transitions | ConcurrentRoot via React 19 |
| No event system | Keyboard shortcuts, focus management | W3C capture/bubble event dispatch |
| Full screen redraws | 60fps on large outputs | Packed Int32Array double-buffering + ANSI diff |
| No alternate screen | Overlay dialogs, search | Alt-screen management |
| No virtual scrolling | 100K+ line conversation history | WeakMap height cache + windowed rendering |
| No text selection | Copy-paste from terminal | Selection system with NoSelect regions |
| No search | Find in conversation | Search highlight overlay (SGR stacking) |

// 源码位置: src/ink/ — 49 files, total ~600KB of custom rendering code

---

## 3. The Rendering Pipeline

Every keypress or state change triggers this pipeline:

```
stdin bytes
  → parse-keypress.ts (23K) — raw byte sequences to KeyPress events
  → Dispatcher.dispatch() — W3C capture/bubble through DOM tree
  → React setState / useSyncExternalStore
  → React reconciliation (fiber tree diff)
  → Yoga layout computation (flexbox → absolute positions)
  → render-node-to-output.ts (63K) — DOM tree → Screen buffer
  → screen.ts diff() — compare front/back buffers (Int32 compare)
  → log-update.ts (27K) — emit only changed cells as ANSI sequences
  → stdout.write()
```

### Frame Scheduling

```typescript
// ink.tsx — throttled rendering, not on every state change
private throttle = 16  // ms — targeting ~60fps
private renderTimer: NodeJS.Timeout | null = null

onRender = () => {
  if (this.renderTimer) return  // Already scheduled
  this.renderTimer = setTimeout(() => {
    this.renderTimer = null
    this.actualRender()
  }, this.throttle)
}
```

---

## 4. The Screen Buffer: Zero-GC Packed Arrays

// 源码位置: src/ink/screen.ts (1,487 lines, 49KB)

This is the most performance-critical code in the entire UI system. Instead of allocating Cell objects (which would create 24,000 objects for a 200×120 screen), cells are packed into typed arrays:

```typescript
// Each cell = 2 × Int32 in a contiguous array:
//   word0: charId (32 bits — index into CharPool)
//   word1: styleId[31:17] | hyperlinkId[16:2] | width[1:0]

const STYLE_SHIFT = 17
const HYPERLINK_SHIFT = 2
const WIDTH_MASK = 3  // 2 bits for CellWidth enum

function packWord1(styleId: number, hyperlinkId: number, width: number): number {
  return (styleId << STYLE_SHIFT) | (hyperlinkId << HYPERLINK_SHIFT) | width
}
```

### CharPool & StylePool: String Interning

Character strings are interned into integer IDs via `CharPool`, with an ASCII fast-path (direct array lookup instead of Map.get for single-byte characters). Style transitions between cells are cached — `StylePool.transition(fromId, toId)` returns pre-serialized ANSI escape strings with zero allocations after first use.

### Double Buffering

```typescript
// resetScreen — reuses existing typed arrays, only grows (never shrinks)
export function resetScreen(screen, width, height): void {
  if (screen.cells64.length < size) {
    // Only reallocate if buffer too small
    const buf = new ArrayBuffer(size << 3)  // 8 bytes per cell
    screen.cells = new Int32Array(buf)
    screen.cells64 = new BigInt64Array(buf)
  }
  screen.cells64.fill(EMPTY_CELL_VALUE, 0, size)  // Single fill, no loop
}
```

The `cells64` BigInt64Array view over the same ArrayBuffer enables bulk clearing — one `fill()` call zeroes the entire screen instead of iterating cells.

---

## 5. The Event System: W3C in Your Terminal

// 源码位置: src/ink/events/dispatcher.ts

Claude Code implements a **W3C-style capture/bubble event model** inside the terminal — the same event propagation model browsers use:

```
Capture phase: root → target (top-down)
Target phase:  event handlers on target node
Bubble phase:  target → root (bottom-up)
```

The `Dispatcher` class integrates with React 19's update priority system:

```typescript
// reconciler.ts:510
dispatcher.discreteUpdates = reconciler.discreteUpdates.bind(reconciler)

// Discrete events (keypress, click) get higher priority
// Continuous events (scroll) get lower priority
getCurrentUpdatePriority: () => dispatcher.currentUpdatePriority,
resolveUpdatePriority(): number {
  return dispatcher.resolveEventPriority()
}
```

### FocusManager

Focus is managed as a **stack** — each focusable component registers with a `FocusManager` that tracks the focus chain. `autoFocus` prop triggers focus acquisition through `commitMount`.

---

## 6. The 35-Line Store (Replacing Redux)

// 源码位置: src/state/store.ts — exactly 35 lines

This might be the most elegant piece of code in the entire codebase:

```typescript
export function createStore<T>(initialState: T, onChange?: OnChange<T>): Store<T> {
  let state = initialState
  const listeners = new Set<Listener>()

  return {
    getState: () => state,
    setState: (updater) => {
      const prev = state
      const next = updater(prev)
      if (Object.is(next, prev)) return   // Reference equality skip
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

**Why this works**: The `Store` interface — `{ getState, setState, subscribe }` — is exactly what React 18+'s `useSyncExternalStore` expects. No middleware, no reducers, no actions, no dev tools integration. Just a closure over a mutable variable with `Object.is` reference equality to skip unnecessary re-renders.

### AppState: The Global State Tree

```typescript
// src/state/AppState.tsx
const store = createStore(initialState ?? getDefaultAppState(), onChangeAppState)
```

`onChangeAppState` centralizes all side-effects — when state changes, this single callback handles all downstream reactions (permission mode transitions, UI updates, etc.).

---

## 7. REPL Screen Architecture

// 源码位置: src/screens/REPL.tsx — 874KB, the largest component file

The REPL is organized as a layered composition:

```
<FullscreenProvider>             ← Terminal resize tracking
  <AlternateScreen>              ← Alt-screen for modal overlays
    <FullscreenLayout>           ← Flexbox root (full terminal)
      <ScrollBox>                ← Scrollable container
        <VirtualMessageList>     ← Windowed rendering (viewport ± 1 screen)
          <Message>              ← Individual message rendering
      <PromptInput>              ← Text input with vim mode
      <StatusLine>               ← Bottom status bar
      <OverlayStack>             ← Permission dialog, model picker, search
```

### Overlay System

Dialogs (permission prompts, model selection, search) are rendered as overlays on the alt-screen, not as separate terminal screens. This preserves the conversation context behind the dialog.

---

## 8. Virtual Scrolling and Height Cache

For conversations with thousands of messages, rendering every message DOM node would be prohibitively expensive. The virtual scroll system renders only visible messages plus a buffer:

```
Visible viewport: messages[startIdx..endIdx]
Buffer: ±1 screen height above/below viewport
Everything else: <Spacer height={cachedHeight} />
```

Height caching uses `WeakMap` so that entries are automatically garbage-collected when messages are removed from the conversation:

```typescript
// Heights are cached per-message using a WeakMap
// WeakMap ensures entries are GC'd when messages leave the conversation
heightCache: WeakMap<Message, number>
```

### Search Index

When the user searches (`Ctrl+F`), the system:
1. Builds an index of matching positions across all messages
2. Scrolls to the current match
3. Applies SGR overlay (yellow background + bold + underline) to the current match via `StylePool.withCurrentMatch()`
4. Other matches get `StylePool.withInverse()` — visually distinct but less prominent

---

## 9. Vim Mode

// 源码位置: src/hooks/useVimInput.ts, src/types/textInputTypes.ts

The Vim mode implementation is a simplified two-state model:

```typescript
export type VimMode = 'INSERT' | 'NORMAL'
```

In `NORMAL` mode, keystrokes are intercepted for navigation (hjkl, w, b, 0, $, etc.) and editing commands (dd, yy, p, etc.). `Escape` transitions from `INSERT` → `NORMAL`; `i`, `a`, `o` transition back to `INSERT`.

The mode state flows through the component tree:
```
REPL.tsx → useState<VimMode>('INSERT')
  → PromptInput → VimTextInput (conditional render)
  → StatusLine → mode indicator display
  → useCancelRequest → Escape behavior changes
```

---

## 10. Keybinding System

### Multi-Layer Context

Keybindings operate in layered contexts with different active bindings:

| Context | Active When | Example Bindings |
|---------|------------|-----------------|
| **Global** | Always | `Ctrl+C` (cancel), `Ctrl+D` (exit) |
| **Chat** | In conversation | `Shift+Tab` (mode cycle), `Enter` (submit) |
| **Permission** | Permission dialog open | `y/n` (allow/deny) |
| **Search** | Search active | `Ctrl+G` (next match), `Escape` (close) |

### User Customization

```typescript
// Users can override defaults via ~/.claude/keybindings.json
// Parsed at startup, merged with built-in bindings
```

---

## Transferable Design Patterns

> The following patterns from the UI rendering system can be directly applied to any high-performance terminal or embedded UI framework.

### Pattern 1: "35 Lines Replace a Library"

The store proves that when your use case is specific enough, a purpose-built solution in 35 lines beats a 50KB dependency. The key insight: `useSyncExternalStore` already does the hard work — you just need to match its expected API shape.

### Pattern 2: Packed Typed Arrays Eliminate GC

The Screen buffer uses `Int32Array` with bit-packing instead of objects. For a 200×120 terminal, this avoids 24,000 object allocations per frame. The `BigInt64Array` view over the same `ArrayBuffer` enables single-call bulk clears. This pattern is transferable to any high-throughput data structure.

### Pattern 3: Browser Event Model in Non-Browser Contexts

The W3C capture/bubble event dispatch works beautifully outside browsers. The key adaptation: mapping terminal-specific events (raw byte sequences, ANSI escape codes) to the same event propagation model that React components expect.

### Pattern 4: String Interning for Render Loops

`CharPool` and `StylePool` convert strings to integers during the render-to-buffer phase. All subsequent operations (diff, transition) use integer comparisons. The ASCII fast-path (direct array indexing for single-byte characters) handles the common case without hash table overhead.

---

## Summary

| Component | Size | Role |
|----------|------|------|
| `ink.tsx` | 252KB | Core rendering engine, React integration, frame scheduling |
| `screen.ts` | 49KB | Packed Int32Array screen buffer, double-buffering, cell diffing |
| `render-node-to-output.ts` | 63KB | DOM tree → Screen buffer conversion |
| `selection.ts` | 35KB | Text selection with NoSelect regions |
| `log-update.ts` | 27KB | ANSI diff output — only changed cells emitted |
| `parse-keypress.ts` | 23KB | Raw stdin → KeyPress event parsing |
| `reconciler.ts` | 15KB | React 19 ConcurrentRoot fiber reconciler |
| `dom.ts` | 15KB | Virtual DOM: ink-box, ink-text nodes |
| `store.ts` | **836B** | Complete state management — 35 lines |
| `REPL.tsx` | 874KB | Main screen: virtual scroll, overlays, vim mode |

Claude Code's UI system is a case study in principled engineering excess. Forking Ink and rewriting the rendering pipeline was a massive undertaking — but the result is a terminal application that renders at 60fps with zero GC pressure, handles conversations with thousands of messages through virtual scrolling, and provides a browser-grade event system for keyboard shortcuts. The 35-line store sitting next to a 49KB screen buffer captures the team's philosophy perfectly: use the simplest possible solution for simple problems, and go to extreme lengths for performance-critical ones.

---

**Previous**: [← 13 — Bridge System](13-bridge-system.md)
**Next**: [→ 15 — Services & API Layer](15-services-api-layer.md)
