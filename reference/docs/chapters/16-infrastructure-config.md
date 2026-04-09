# 16 — Infrastructure & Config: The Hidden Skeleton of Claude Code

> 🌐 **Language**: English | [中文版 →](/zh-CN/chapters/16-infrastructure-config)
> 📖 **[Read Online →](https://openedclaude.github.io/claude-reviews-claude/chapters/16-infrastructure-config)** — Sidebar nav, dark mode & full-text search. Better than raw GitHub.


> **Scope**: `bootstrap/state.ts` (56KB), `entrypoints/init.ts`, `utils/config.ts`, `utils/settings/`, `utils/secureStorage/`, `utils/tokens.ts`, `utils/claudemd.ts`, `utils/signal.ts`, `utils/git/`, `utils/thinking.ts`, `utils/cleanupRegistry.ts`, `utils/startupProfiler.ts`
>
> **One-liner**: The unsung infrastructure layer — from a 1,759-line global state singleton to a five-layer settings merge system — that keeps every other subsystem running without circular dependencies.

---

---

## 1. Bootstrap Global Singleton

**Source coordinates**: `src/bootstrap/state.ts` (1,759 lines, 56KB — the single largest file that imports almost nothing)

Every complex system has a "God object" problem. Claude Code's answer is `state.ts` — a **leaf module** that sits at the very bottom of the dependency graph, importing only external packages and type-only declarations. This is not an accident; it is _enforced by a custom ESLint rule_.

### 1.1 The Leaf Module Constraint

```typescript
// 源码位置: src/bootstrap/state.ts:17-18
// eslint-disable-next-line custom-rules/bootstrap-isolation
import { randomUUID } from 'src/utils/crypto.js'
```

The `custom-rules/bootstrap-isolation` rule ensures that `state.ts` never imports from the rest of `src/`. The only exception — `randomUUID` via `crypto.js` — requires an explicit ESLint disable comment and exists only because the browser SDK build needs a platform-agnostic `crypto` shim.

**Why this matters**: In a codebase with 100+ modules, any module that becomes a dependency hub creates circular import risks. By making `state.ts` a leaf, Claude Code guarantees that any module can import it without risk of dependency cycles. This is the **architectural immune system** against circular dependencies.

### 1.2 The State Object: ~100 Fields of Session Truth

The private `STATE` object is the single source of truth for session-wide state. A partial taxonomy:

```
┌─────────────────────────────────────────────────────────────┐
│                    STATE Object Taxonomy                     │
├──────────────────────┬──────────────────────────────────────┤
│ Identity & Paths     │ originalCwd, projectRoot, cwd,       │
│                      │ sessionId, parentSessionId            │
├──────────────────────┼──────────────────────────────────────┤
│ Cost & Metrics       │ totalCostUSD, totalAPIDuration,       │
│                      │ turnHookDurationMs, turnToolCount     │
├──────────────────────┼──────────────────────────────────────┤
│ Model Config         │ modelUsage, mainLoopModelOverride,    │
│                      │ initialMainLoopModel, modelStrings    │
├──────────────────────┼──────────────────────────────────────┤
│ Telemetry (OTel)     │ meter, sessionCounter, locCounter,    │
│                      │ loggerProvider, tracerProvider         │
├──────────────────────┼──────────────────────────────────────┤
│ Cache Latches        │ afkModeHeaderLatched,                  │
│ (sticky-on)          │ fastModeHeaderLatched,                 │
│                      │ promptCache1hEligible,                 │
│                      │ cacheEditingHeaderLatched               │
├──────────────────────┼──────────────────────────────────────┤
│ Session Flags        │ sessionBypassPermissionsMode,          │
│ (not persisted)      │ sessionTrustAccepted,                  │
│                      │ scheduledTasksEnabled,                  │
│                      │ sessionCreatedTeams                     │
├──────────────────────┼──────────────────────────────────────┤
│ Skills & Plugins     │ invokedSkills, inlinePlugins,          │
│                      │ allowedChannels, hasDevChannels         │
├──────────────────────┼──────────────────────────────────────┤
│ Prompt Cache State   │ promptCache1hAllowlist,                │
│                      │ lastMainRequestId, pendingPostCompac.  │
└──────────────────────┴──────────────────────────────────────┘
```

### 1.3 Latching: Once On, Never Off

The most elegant pattern in `state.ts` is the **sticky-on latch** — certain beta headers, once activated, remain active for the entire session:

```typescript
// 源码位置: src/bootstrap/state.ts:226-242
// Sticky-on latch for AFK_MODE_BETA_HEADER. Once auto mode is first
// activated, keep sending the header for the rest of the session so
// Shift+Tab toggles don't bust the ~50-70K token prompt cache.
afkModeHeaderLatched: boolean | null   // null = not yet triggered

// Same pattern repeats for:
fastModeHeaderLatched: boolean | null
cacheEditingHeaderLatched: boolean | null
thinkingClearLatched: boolean | null
```

**The economics**: If a `Shift+Tab` toggle flipped the prompt cache control header on and off, each flip would invalidate the server-side prompt cache (~50–70K tokens). At $3/MTok input, that's ~$0.15–$0.21 wasted per toggle. Latching turns this from a per-toggle cost into a per-session one-time cost.

### 1.4 Atomic Session Switching

```typescript
// 源码位置: src/bootstrap/state.ts:468-479
export function switchSession(
  sessionId: SessionId,
  projectDir: string | null = null,
): void {
  STATE.planSlugCache.delete(STATE.sessionId)  // Clean up old session
  STATE.sessionId = sessionId
  STATE.sessionProjectDir = projectDir
  sessionSwitched.emit(sessionId)  // Notify subscribers
}
```

`sessionId` and `sessionProjectDir` always change together — there is no separate setter for either. Comment `CC-34` references the bug that motivated this design: when they were set independently, `/resume` could leave them out of sync, leading to transcript writes to the wrong directory.

### 1.5 Interaction Time Batching

A subtle optimization for terminal rendering:

```typescript
// 源码位置: src/bootstrap/state.ts:665-689
let interactionTimeDirty = false

export function updateLastInteractionTime(immediate?: boolean): void {
  if (immediate) {
    flushInteractionTime_inner()  // Date.now() immediately
  } else {
    interactionTimeDirty = true   // Defer to next render cycle
  }
}

export function flushInteractionTime(): void {
  if (interactionTimeDirty) {
    flushInteractionTime_inner()
  }
}
```

Rather than calling `Date.now()` on every single keypress, the system marks a dirty flag and batches the actual timestamp update into the Ink render cycle. The `immediate` path exists for React `useEffect` callbacks that run _after_ the render cycle has already flushed.

---

## 2. The init.ts Initialization Orchestrator

**Source coordinates**: `src/entrypoints/init.ts` (341 lines)

The `init()` function — `memoize`-wrapped so it executes exactly once — orchestrates the startup sequence. It's a masterclass in **ordered initialization with strategic parallelism**.

### 2.1 The Initialization Sequence

```
┌─ 1. enableConfigs()              — Validate and enable config system
│
├─ 2. applySafeConfigEnvironmentVariables()
│     ↳ Only safe vars before trust dialog
│
├─ 3. applyExtraCACertsFromConfig()
│     ↳ Must happen before first TLS handshake
│     ↳ Bun caches cert store at boot (BoringSSL)
│
├─ 4. setupGracefulShutdown()       — Register cleanup handlers
│
├─ 5. void Promise.all([...])       — Fire-and-forget async init
│     ├─ firstPartyEventLogger      — Non-blocking
│     └─ growthbook                  — Feature flag refresh callback
│
├─ 6. void populateOAuthAccountInfoIfNeeded()   — Async, non-blocking
│  void initJetBrainsDetection()
│  void detectCurrentRepository()
│
├─ 7. configureGlobalMTLS()         — Mutual TLS
│  configureGlobalAgents()          — Proxy configuration
│
├─ 8. preconnectAnthropicApi()      — TCP+TLS handshake overlap
│     ↳ ~100-200ms during action-handler's ~100ms work
│
├─ 9. registerCleanup(shutdownLspServerManager)
│  registerCleanup(cleanupSessionTeams)
│
└─ 10. ensureScratchpadDir()        — If scratchpad enabled
```

### 2.2 Why the Order Matters

Step 3 (CA certs) **must** precede Step 8 (preconnect): Bun uses BoringSSL, which caches the certificate store at boot time. If extra CA certs from enterprise settings aren't applied before the first TLS handshake, they'll be ignored for the entire process lifetime.

Step 8 (preconnect) **must** follow Step 7 (proxy): the preconnect optimization opens a TCP+TLS connection to the Anthropic API, overlapping it with the ~100ms of action-handler work. But it must use the configured proxy/mTLS transport, so proxy setup goes first. The preconnect is skipped entirely when proxy/mTLS/unix socket configurations would prevent the global HTTP pool from reusing the warmed connection.

### 2.3 Telemetry: Deferred Until After Trust

```typescript
// 源码位置: src/entrypoints/init.ts:305-311
async function setMeterState(): Promise<void> {
  // Lazy-load instrumentation to defer ~400KB of OpenTelemetry + protobuf
  const { initializeTelemetry } = await import(
    '../utils/telemetry/instrumentation.js'
  )
  const meter = await initializeTelemetry()
  // ...
}
```

The telemetry stack — ~400KB of OpenTelemetry + protobuf, plus a further ~700KB of `@grpc/grpc-js` for exporters — is loaded **only after the trust dialog is accepted**. This is both a performance optimization (don't pay the import cost on `--version`) and a privacy guarantee (no telemetry before consent).

For enterprise users with remote managed settings, the sequence adds another step: wait for remote settings to load before initializing telemetry, so the telemetry configuration can include org-specific endpoints.

### 2.4 ConfigParseError: The Graceful Error Dialog

```typescript
// 源码位置: src/entrypoints/init.ts:215-237
if (error instanceof ConfigParseError) {
  if (getIsNonInteractiveSession()) {
    process.stderr.write(`Configuration error in ${error.filePath}: ...\n`)
    gracefulShutdownSync(1)
    return
  }
  return import('../components/InvalidConfigDialog.js').then(m =>
    m.showInvalidConfigDialog({ error })
  )
}
```

When `settings.json` fails Zod validation, a React-based Ink dialog appears to show the error and guide the user toward fixing it. But in non-interactive (SDK/headless) mode, the dialog would break JSON consumers, so it falls back to stderr and exits.

---

## 3. Dual-Layer Configuration System

**Source coordinates**: `src/utils/config.ts`

Claude Code separates runtime state from behavior configuration:

| Layer | File | Purpose |
|-------|------|---------|
| **GlobalConfig** | `~/.claude.json` | Runtime state: OAuth tokens, session history, usage metrics |
| **ProjectConfig** | `.claude/config.json` | Project state: allowed tools, MCP servers, trust status |
| **SettingsJson** | `settings.json` (multi-source) | Behavior: permissions, hooks, model selection, env vars |

### 3.1 ProjectConfig Structure

```typescript
export type ProjectConfig = {
  allowedTools: string[]           // Tool permissions
  mcpContextUris: string[]         // MCP context URI list

  // Last session metrics (for display)
  lastAPIDuration?: number
  lastCost?: number
  lastLinesAdded?: number
  lastModelUsage?: Record<string, { inputTokens, outputTokens, ... }>

  // Trust dialog state
  hasTrustDialogAccepted?: boolean
  hasCompletedProjectOnboarding?: boolean

  // Worktree session management
  activeWorktreeSession?: {
    originalCwd: string
    worktreePath: string
    worktreeName: string
    sessionId: string
  }
}
```

### 3.2 Re-entrancy Guard

A subtle but critical defense against infinite recursion:

```typescript
// 源码位置: src/utils/config.ts
let insideGetConfig = false

export function getGlobalConfig(): GlobalConfig {
  if (insideGetConfig) {
    return DEFAULT_GLOBAL_CONFIG  // Short-circuit with defaults
  }
  insideGetConfig = true
  try {
    // ... actual read logic (which may trigger logEvent → getGlobalConfig)
  } finally {
    insideGetConfig = false
  }
}
```

The call chain `getConfig → logEvent → getGlobalConfig → getConfig` would recurse infinitely without this guard. The fix is elegant: when re-entered, return the default config. The log event gets slightly stale data, but the system doesn't crash.

---

## 4. Five-Layer Settings Merge

**Source coordinates**: `src/utils/settings/settings.ts`, `src/utils/settings/types.ts`, `src/utils/settings/constants.ts`

### 4.1 The Five Sources

Settings load from five sources, with later sources overriding earlier ones:

```typescript
// 源码位置: src/utils/settings/constants.ts
export const SETTING_SOURCES = [
  'userSettings',      // ~/.claude/settings.json — Personal global
  'projectSettings',   // .claude/settings.json — Project shared, committed
  'localSettings',     // .claude/settings.local.json — Project local, gitignored
  'flagSettings',      // --settings CLI argument override
  'policySettings',    // managed-settings.json or remote API — Enterprise
] as const
```

### 4.2 Enterprise Managed Settings: Drop-In Directory

For enterprise deployment, Claude Code supports systemd-style drop-in configuration:

```typescript
// 源码位置: src/utils/settings/settings.ts
export function loadManagedFileSettings(): { settings, errors } {
  // 1. Load base file: managed-settings.json (lowest priority)
  const { settings } = parseSettingsFile(getManagedSettingsFilePath())

  // 2. Load drop-in directory: managed-settings.d/*.json
  //    Alphabetically sorted, later files override earlier
  //    Example: 10-otel.json, 20-security.json
  const entries = readdirSync(dropInDir)
    .filter(d => d.name.endsWith('.json') && !d.name.startsWith('.'))
    .sort()

  for (const name of entries) {
    merged = mergeWith(merged, settings, settingsMergeCustomizer)
  }
}
```

This enables IT departments to deploy configuration fragments independently: `10-otel.json` for observability settings, `20-security.json` for permission policies, `30-models.json` for approved model lists. Each team can own their fragment without merge conflicts.

### 4.3 Zod v4 Schema Validation with lazySchema

All settings pass through Zod validation:

```typescript
// 源码位置: src/utils/settings/types.ts
export const PermissionsSchema = lazySchema(() =>
  z.object({
    allow: z.array(PermissionRuleSchema()).optional(),
    deny: z.array(PermissionRuleSchema()).optional(),
    ask: z.array(PermissionRuleSchema()).optional(),
    defaultMode: z.enum(PERMISSION_MODES).optional(),
    disableBypassPermissionsMode: z.enum(['disable']).optional(),
    additionalDirectories: z.array(z.string()).optional(),
  }).passthrough()
)
```

The `lazySchema` pattern deserves attention:

```typescript
export function lazySchema<T>(factory: () => T): () => T {
  let cached: T | undefined
  return () => cached ?? (cached = factory())
}
```

This is not just a performance optimization — it **breaks circular dependencies** between schema files. When `schemas/hooks.ts` references types from `settings/types.ts` and vice versa, wrapping schemas in lazy factories ensures neither needs to be fully evaluated at import time.

---

## 5. Secure Storage

**Source coordinates**: `src/utils/secureStorage/` (index.ts, macOsKeychainStorage.ts, fallbackStorage.ts)

### 5.1 Platform Adaptation Chain

```typescript
// 源码位置: src/utils/secureStorage/index.ts
export function getSecureStorage(): SecureStorage {
  if (process.platform === 'darwin') {
    return createFallbackStorage(macOsKeychainStorage, plainTextStorage)
  }
  return plainTextStorage  // Linux/Windows: graceful degradation
}
```

On macOS, the system `security` command provides native Keychain integration. On other platforms, credentials fall back to plaintext storage in the user's config directory.

### 5.2 macOS Keychain: TTL Cache + Stale-While-Error

```typescript
// 源码位置: src/utils/secureStorage/macOsKeychainStorage.ts
export const macOsKeychainStorage = {
  read(): SecureStorageData | null {
    // TTL cache — avoid frequent spawn of security process
    if (Date.now() - prev.cachedAt < KEYCHAIN_CACHE_TTL_MS) {
      return prev.data
    }
    // execSync: security find-generic-password
    const result = execSyncWithDefaults_DEPRECATED(
      `security find-generic-password -a "${username}" -w -s "${storageServiceName}"`
    )
    // ...
  },

  update(data: SecureStorageData): { success: boolean; warning?: string } {
    // CRITICAL: security find-generic-password's stdin has a 4096 byte limit
    // Exceeding it causes silent truncation → data corruption
    const SECURITY_STDIN_LINE_LIMIT = 4096 - 64  // 64B safety margin
    // ...
  }
}
```

The **stale-while-error** strategy is the key insight: when the `security` subprocess fails (transient macOS Keychain Service restart, user switching, etc.), cached data continues to be served rather than returning null. Without this, a single `security` process failure would manifest as a global "Not logged in" error, forcing the user to re-authenticate.

### 5.3 Async De-duplication

```typescript
async readAsync(): Promise<SecureStorageData | null> {
  if (keychainCacheState.readInFlight) {
    return keychainCacheState.readInFlight  // Merge concurrent requests
  }
  keychainCacheState.readInFlight = doRead()
  try {
    return await keychainCacheState.readInFlight
  } finally {
    keychainCacheState.readInFlight = null
  }
}
```

Multiple concurrent calls to `readAsync()` share a single in-flight promise, preventing thundering herd on the Keychain subprocess.

---

## 6. Signal Event Primitive & AbortController

**Source coordinates**: `src/utils/signal.ts`, `src/utils/abortController.ts`

### 6.1 The Signal Primitive

Claude Code replaces ~15 instances of hand-written listener sets with a single reusable primitive:

```typescript
// 源码位置: src/utils/signal.ts
export type Signal<Args extends unknown[] = []> = {
  subscribe: (listener: (...args: Args) => void) => () => void
  emit: (...args: Args) => void
  clear: () => void
}

export function createSignal<Args extends unknown[] = []>(): Signal<Args> {
  const listeners = new Set<(...args: Args) => void>()
  return {
    subscribe(listener) {
      listeners.add(listener)
      return () => { listeners.delete(listener) }  // Returns unsubscribe fn
    },
    emit(...args) {
      for (const listener of listeners) listener(...args)
    },
    clear() { listeners.clear() },
  }
}
```

Usage in `state.ts`:

```typescript
// 源码位置: src/bootstrap/state.ts:481-489
const sessionSwitched = createSignal<[id: SessionId]>()
export const onSessionSwitch = sessionSwitched.subscribe
// Internal: sessionSwitched.emit(sessionId) in switchSession()
```

**Signal vs Store**: A Signal has no snapshot or `getState()` — it only says "something happened." Stores (Episode 14) hold state and notify on change. The distinction keeps the API surface minimal.

### 6.2 Parent-Child AbortController with WeakRef

```typescript
// 源码位置: src/utils/abortController.ts
export function createChildAbortController(
  parent: AbortController,
  maxListeners?: number,
): AbortController {
  const child = createAbortController(maxListeners)

  // Fast path: parent already aborted
  if (parent.signal.aborted) {
    child.abort(parent.signal.reason)
    return child
  }

  // WeakRef prevents memory leak
  const weakChild = new WeakRef(child)
  const weakParent = new WeakRef(parent)
  const handler = propagateAbort.bind(weakParent, weakChild)

  parent.signal.addEventListener('abort', handler, { once: true })

  // Auto-cleanup: child abort removes parent listener
  child.signal.addEventListener(
    'abort',
    removeAbortHandler.bind(weakParent, new WeakRef(handler)),
    { once: true },
  )

  return child
}

// Module-level function — avoids per-call closure allocation
function propagateAbort(
  this: WeakRef<AbortController>,
  weakChild: WeakRef<AbortController>,
): void {
  const parent = this.deref()
  weakChild.deref()?.abort(parent?.signal.reason)
}
```

Three memory safety guarantees:
1. **WeakRef** prevents the parent from keeping abandoned children alive
2. **{once: true}** ensures listeners fire at most once
3. **Module-level `propagateAbort`** uses `.bind()` instead of closures, avoiding per-call function object allocation

---

## 7. Git Utility Library

**Source coordinates**: `src/utils/git/gitFilesystem.ts`, `src/utils/git/gitConfigParser.ts`

### 7.1 Filesystem-Level Git Status

Instead of spawning `git` subprocesses for every status check, Claude Code reads `.git` files directly:

```typescript
// 源码位置: src/utils/git/gitFilesystem.ts
export async function resolveGitDir(startPath?: string): Promise<string | null> {
  const gitPath = join(root, '.git')
  const st = await stat(gitPath)
  if (st.isFile()) {
    // Worktree or Submodule: .git is a file containing "gitdir: <path>"
    const content = (await readFile(gitPath, 'utf-8')).trim()
    if (content.startsWith('gitdir:')) {
      return resolve(root, content.slice('gitdir:'.length).trim())
    }
  }
  return gitPath  // Normal repository: .git is a directory
}
```

This handles three cases transparently:
- **Normal repos**: `.git` is a directory → return it
- **Worktrees**: `.git` is a file containing `gitdir: ../../../.git/worktrees/name` → follow the pointer
- **Submodules**: `.git` is a file containing `gitdir: ../../.git/modules/name` → follow the pointer

### 7.2 Ref Name Safety Validation

```typescript
// 源码位置: src/utils/git/gitFilesystem.ts
export function isSafeRefName(name: string): boolean {
  if (!name || name.startsWith('-') || name.startsWith('/')) return false
  // Whitelist: ASCII alphanumeric + / . _ + - @
  // Blocks: path traversal (..), argument injection (-),
  //         shell metacharacters (backtick, $, ;, |, &)
}
```

This prevents three attack vectors:
- **Path traversal**: `../../../etc/passwd` as a branch name
- **Argument injection**: `-c 'malicious code'` passed to `git checkout`
- **Shell metacharacter injection**: `` `rm -rf /` `` embedded in ref names

---

## 8. Token Management & Context Budgets

**Source coordinates**: `src/utils/tokens.ts`, `src/utils/context.ts`, `src/utils/tokenBudget.ts`

### 8.1 The Authoritative Token Counter

`tokenCountWithEstimation()` is the **single source of truth** for context window size:

```typescript
// 源码位置: src/utils/tokens.ts
export function tokenCountWithEstimation(messages: readonly Message[]): number {
  let i = messages.length - 1
  while (i >= 0) {
    const usage = getTokenUsage(messages[i])
    if (usage) {
      // Handle parallel tool call message splitting:
      // When tools run in parallel, a single API response is split into
      // multiple Message objects sharing the same message.id.
      // We must backtrack to the FIRST message with this ID.
      const responseId = getAssistantMessageId(messages[i])
      if (responseId) {
        let j = i - 1
        while (j >= 0) {
          if (getAssistantMessageId(messages[j]) === responseId) i = j
          else if (getAssistantMessageId(messages[j]) !== undefined) break
          j--
        }
      }
      return getTokenCountFromUsage(usage) +
        roughTokenCountEstimationForMessages(messages.slice(i + 1))
    }
    i--
  }
  return roughTokenCountEstimationForMessages(messages)
}
```

The algorithm:
1. Walk backward through messages to find the last API response with `usage` data
2. Handle the parallel tool split: if the response was split into multiple messages (same `message.id`), backtrack to the first one
3. Use the API-reported token count as the baseline, then **estimate** tokens for messages that arrived after that response

This hybrid approach (API truth + estimation) avoids expensive tokenization calls while staying accurate enough for compaction thresholds.

### 8.2 Context Window Size Detection

```typescript
// 源码位置: src/utils/context.ts
export const MODEL_CONTEXT_WINDOW_DEFAULT = 200_000

export function getContextWindowForModel(model: string, betas?: string[]): number {
  // Priority:
  // 1. CLAUDE_CODE_MAX_CONTEXT_TOKENS env var (Anthropic internal)
  // 2. [1m] model suffix → 1,000,000
  // 3. Model capability query → dynamic value
  // 4. Default: 200,000

  if (has1mContext(model)) return 1_000_000
  const cap = getModelCapability(model)
  if (cap?.max_input_tokens >= 100_000) return cap.max_input_tokens
  return MODEL_CONTEXT_WINDOW_DEFAULT
}
```

### 8.3 User-Specified Token Budget

Users can embed budget hints directly in their messages:

```typescript
// 源码位置: src/utils/tokenBudget.ts
// Supported formats:
// "+500k" (at message start or end)
// "use 2M tokens" / "spend 500k tokens"

const SHORTHAND_START_RE = /^\s*\+(\d+(?:\.\d+)?)\s*(k|m|b)\b/i
const VERBOSE_RE = /\b(?:use|spend)\s+(\d+(?:\.\d+)?)\s*(k|m|b)\s*tokens?\b/i

export function parseTokenBudget(text: string): number | null {
  const startMatch = text.match(SHORTHAND_START_RE)
  if (startMatch) return parseBudgetMatch(startMatch[1]!, startMatch[2]!)
  // ...
}
```

When a user types `+500k fix the login bug`, the system parses 500,000 as the output token budget for this turn, allowing longer responses for complex tasks.

---

## 9. CLAUDE.md & Persistent Memory System

**Source coordinates**: `src/utils/claudemd.ts`, `src/memdir/memdir.ts`, `src/memdir/paths.ts`

### 9.1 The Loading Hierarchy

CLAUDE.md files load from four levels, with later levels overriding earlier ones:

```
1. /etc/claude-code/CLAUDE.md              — Enterprise global (managed memory)
2. ~/.claude/CLAUDE.md + ~/.claude/rules/   — User global
3. Project CLAUDE.md, .claude/CLAUDE.md,    — Project-level (version-controlled)
   .claude/rules/*.md
4. CLAUDE.local.md                          — Project local (gitignored)
```

### 9.2 @include Directive System

CLAUDE.md supports cross-file inclusion:

```typescript
// 源码位置: src/utils/claudemd.ts
// Supports: @path, @./relative, @~/home, @/absolute
// Only works in leaf text nodes (not inside code blocks)
// Uses marked.Lexer for markdown structure parsing
// Has circular reference detection
// Non-existent files silently ignored

const TEXT_FILE_EXTENSIONS = new Set([
  '.md', '.txt', '.json', '.yaml', '.yml', '.toml', '.xml',
  '.js', '.ts', '.tsx', '.py', '.go', '.rs', '.java', '.kt',
  '.c', '.cpp', '.h', '.cs', // ... more
])
```

The implementation parses the CLAUDE.md through `marked.Lexer`, finds `@path` references only in leaf text nodes (preventing code blocks from being treated as includes), resolves paths relative to the file's location, and recursively includes their content — with cycle detection to prevent `A includes B includes A` loops.

### 9.3 Auto Memory (memdir): Dream-Like Consolidation

```typescript
// 源码位置: src/memdir/memdir.ts
export const ENTRYPOINT_NAME = 'MEMORY.md'
export const MAX_ENTRYPOINT_LINES = 200
export const MAX_ENTRYPOINT_BYTES = 25_000

export function truncateEntrypointContent(raw: string): EntrypointTruncation {
  const contentLines = raw.trim().split('\n')
  const wasLineTruncated = contentLines.length > MAX_ENTRYPOINT_LINES
  const wasByteTruncated = raw.trim().length > MAX_ENTRYPOINT_BYTES

  if (wasLineTruncated) {
    truncated = contentLines.slice(0, MAX_ENTRYPOINT_LINES).join('\n')
  }
  if (truncated.length > MAX_ENTRYPOINT_BYTES) {
    // Cut at last newline before byte limit — don't split lines
    const cutAt = truncated.lastIndexOf('\n', MAX_ENTRYPOINT_BYTES)
    truncated = truncated.slice(0, cutAt > 0 ? cutAt : MAX_ENTRYPOINT_BYTES)
  }
}
```

The auto-memory system uses a background `DreamTask` agent (covered in Episode 08) to periodically review session history and consolidate learnings into `MEMORY.md`. The truncation strategy is deliberately line-aware: it never splits a line mid-content, preferring to cut at newline boundaries.

### 9.4 Memory Directory Path Resolution

```typescript
// 源码位置: src/memdir/paths.ts
export function isAutoMemoryEnabled(): boolean {
  // Priority chain:
  // 1. CLAUDE_CODE_DISABLE_AUTO_MEMORY env var
  // 2. CLAUDE_CODE_SIMPLE (--bare mode) → disabled
  // 3. CCR (no persistent storage) → disabled
  // 4. settings.json: autoMemoryEnabled
  // 5. Default: enabled
}

export function getMemoryBaseDir(): string {
  return process.env.CLAUDE_CODE_REMOTE_MEMORY_DIR || getClaudeConfigHomeDir()
}
```

---

## 10. Thinking Mode API Rules

**Source coordinates**: `src/utils/thinking.ts`

### 10.1 Configuration Types

```typescript
export type ThinkingConfig =
  | { type: 'adaptive' }                    // Model decides (4.6+ only)
  | { type: 'enabled'; budgetTokens: number }  // Fixed budget
  | { type: 'disabled' }                    // No thinking blocks
```

### 10.2 Provider-Aware Capability Detection

```typescript
// 源码位置: src/utils/thinking.ts
export function modelSupportsThinking(model: string): boolean {
  // 1. Check 3P model capability overrides
  const supported3P = get3PModelCapabilityOverride(model, 'thinking')
  if (supported3P !== undefined) return supported3P

  // 2. 1P and Foundry: all Claude 4+ models
  if (provider === 'firstParty' || provider === 'foundry') {
    return !canonical.includes('claude-3-')
  }

  // 3. 3P (Bedrock/Vertex): only Opus 4+ and Sonnet 4+
  return canonical.includes('sonnet-4') || canonical.includes('opus-4')
}

export function modelSupportsAdaptiveThinking(model: string): boolean {
  // Only 4.6 version models support adaptive thinking
  if (canonical.includes('opus-4-6') || canonical.includes('sonnet-4-6')) return true
  // 1P/Foundry: default enable for unknown models (new models train adaptive)
  return provider === 'firstParty' || provider === 'foundry'
}
```

### 10.3 Ultrathink: Build-Time + Runtime Double Gate

```typescript
// 源码位置: src/utils/thinking.ts
export function isUltrathinkEnabled(): boolean {
  if (!feature('ULTRATHINK')) return false   // Build-time DCE: removed entirely
  return getFeatureValue_CACHED_MAY_BE_STALE('tengu_turtle_carbon', true)
}
```

The `feature()` call is resolved at build time by `bun:bundle`. In external builds, `ULTRATHINK` is `false`, so the entire function body including the GrowthBook call is dead-code-eliminated. In internal builds, the runtime GrowthBook flag provides dynamic control.

### 10.4 Complete Feature Flag Enumeration

Across the entire codebase, **16 compile-time feature flags** (Tier 1) and **19+ runtime GrowthBook flags** (Tier 3) have been identified:

**Compile-Time Flags** (resolved by `bun:bundle`, dead-code-eliminated when `false`):

| Flag | Controls | Status in Published Build |
|------|----------|:------------------------:|
| `VOICE_MODE` | Voice input/recording | ❌ Stripped |
| `KAIROS` | Autonomous agent mode | ❌ Stripped |
| `DAEMON` | Background process management | ❌ Stripped |
| `COORDINATOR_MODE` | Multi-agent coordination | ❌ Stripped |
| `ULTRATHINK` | Extended thinking mode | ❌ Stripped |
| `ULTRAPLAN` | Advanced multi-step planning | ❌ Stripped |
| `AUTO_DREAM` | Background memory consolidation | ❌ Stripped |
| `BUDDY` | Virtual companion system | ❌ Stripped |
| `MORERIGHT` | Context window extension | ❌ Stripped |
| `DXT` | Plugin packaging tool | ❌ Stripped |
| `COMMIT_ATTRIBUTION` | Enhanced PR attribution trailers | ❌ Stripped |
| `PROACTIVE` | Proactive agent suggestions | ❌ Stripped |
| `AGENT_SWARMS` | Multi-agent team creation | ✅ Available |
| `WEB_BROWSER_TOOL` | Browser automation | ✅ Available |
| `HISTORY_SNIP` | History snipping | ✅ Available |
| `WORKFLOW_SCRIPTS` | Workflow script execution | ✅ Available |

**Runtime GrowthBook Flags** (Tier 3, `tengu_*` naming convention):
> → See [Episode 17: Telemetry & Ops](./17-telemetry-privacy-operations) §5.1 for the complete 19-flag enumeration with decoded purposes.

The three-tier system (Compile-Time DCE → Environment Check → GrowthBook) ensures defense in depth: even if a runtime flag is accidentally enabled, the compile-time gate prevents the code from existing in the binary at all.

---

## Transferable Design Patterns

> The following patterns, distilled from Claude Code's infrastructure layer, are directly applicable to any complex CLI tool or agentic system.

### Pattern 1: Leaf Module Isolation

**Scenario**: Global state module that every other module imports.
**Problem**: Adding imports to a global module creates circular dependency risk.
**Practice**: Make the global module a dependency graph leaf — **it imports nothing from the project**. Enforce this with a custom linter rule.
**Claude Code Application**: `bootstrap/state.ts` uses `custom-rules/bootstrap-isolation` to prevent any import from `src/` (except type-only imports and explicitly approved exceptions).
**Benefit**: Any module can safely `import { getSessionId } from 'bootstrap/state'` without circular dependency risk.

### Pattern 2: Sticky-On Latch (Cache Key Stability)

**Scenario**: Boolean flags that toggle API request headers affecting server-side caches.
**Problem**: Toggling a header invalidates the prompt cache, wasting tokens worth $0.15–$0.21 per flip.
**Practice**: Once a header is first activated, keep it active for the session lifetime. Use a tri-state type (`boolean | null`) where `null` means "not yet triggered."
**Claude Code Application**: `afkModeHeaderLatched`, `fastModeHeaderLatched`, `cacheEditingHeaderLatched` — all use this pattern.

### Pattern 3: Re-entrancy Guard

**Scenario**: Configuration reader that triggers logging, which reads configuration.
**Problem**: Infinite recursion: `getConfig → logEvent → getConfig → logEvent → ...`
**Practice**: Boolean guard flag + short-circuit return of default values on re-entry.
**Claude Code Application**: `config.ts` uses `insideGetConfig` flag to break the cycle.

### Pattern 4: Stale-While-Error

**Scenario**: External service (OS Keychain, remote API) that occasionally fails.
**Problem**: A single transient failure causes the entire session to show "Not logged in."
**Practice**: On failure, serve the most recently cached successful response rather than returning null. Log the anomaly but don't disrupt the user.
**Claude Code Application**: `macOsKeychainStorage` continues serving cached credentials when the `security` subprocess fails.

### Pattern 5: Drop-In Configuration Directory

**Scenario**: Enterprise deployment where multiple teams need to configure different aspects.
**Problem**: A single `settings.json` creates merge conflicts between teams and deployment pipelines.
**Practice**: Support a `settings.d/*.json` directory where files are loaded alphabetically and merged. Use numeric prefixes (`10-otel.json`, `20-security.json`) for deterministic ordering.
**Claude Code Application**: `managed-settings.d/` enables IT departments to deploy configuration fragments independently.

### Pattern 6: lazySchema for Circular Schema Dependencies

**Scenario**: Zod schemas that reference each other (Schema A uses Schema B, Schema B uses Schema A).
**Problem**: Circular import at module evaluation time causes `undefined` references.
**Practice**: Wrap schema constructors in a `lazySchema()` factory that defers evaluation until first use, with caching to avoid re-construction.
**Claude Code Application**: `schemas/hooks.ts` uses `lazySchema` to reference types from `settings/types.ts` without circular import.

### Pattern 7: WeakRef in Parent-Child Event Propagation

**Scenario**: Parent AbortController that propagates cancellation to children.
**Problem**: Parent holds strong references to all child controllers, preventing GC of abandoned children.
**Practice**: Use `WeakRef` for the parent→child reference, module-level `.bind()` handlers instead of closures, and `{once: true}` event listeners for automatic cleanup.
**Claude Code Application**: `createChildAbortController()` in `abortController.ts`.

---

## Source File Reference

| File | Size | Role |
|------|------|------|
| `bootstrap/state.ts` | 56KB / 1,759 lines | Global state singleton, leaf module |
| `entrypoints/init.ts` | 14KB / 341 lines | Initialization orchestrator |
| `utils/config.ts` | ~12KB | Dual-layer configuration (Global + Project) |
| `utils/settings/settings.ts` | ~15KB | Five-layer settings merge + enterprise drop-in |
| `utils/settings/types.ts` | ~10KB | Zod v4 schema definitions |
| `utils/secureStorage/` | ~8KB | Platform-adaptive credential storage |
| `utils/signal.ts` | ~2KB | Lightweight event primitive |
| `utils/abortController.ts` | ~5KB | WeakRef-based parent-child cancellation |
| `utils/git/gitFilesystem.ts` | ~7KB | Filesystem-level Git operations |
| `utils/tokens.ts` | ~8KB | Token counting + context estimation |
| `utils/claudemd.ts` | ~15KB | CLAUDE.md loading + @include system |
| `memdir/*.ts` | ~10KB | Auto-memory (MEMORY.md) system |
| `utils/thinking.ts` | ~5KB | Thinking mode configuration + capability detection |
| `utils/cleanupRegistry.ts` | ~1KB | Cleanup function registry |
| `utils/startupProfiler.ts` | ~3KB | Startup performance profiling |
| `utils/tokenBudget.ts` | ~2KB | User-specified token budget parsing |

---

**Previous**: [← 15 — Services & API Layer](./15-services-api-layer) · **Next**: [17 — Telemetry, Privacy & Operations →](./17-telemetry-privacy-operations)
