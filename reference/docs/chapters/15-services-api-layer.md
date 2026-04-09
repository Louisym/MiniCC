# Episode 15: Services & API Layer — The Nervous System

> 🌐 **Language**: English | [中文版 →](/zh-CN/chapters/15-services-api-layer)
> 📖 **[Read Online →](https://openedclaude.github.io/claude-reviews-claude/chapters/15-services-api-layer)** — Sidebar nav, dark mode & full-text search. Better than raw GitHub.


> **Source files**: `src/services/api/claude.ts` (~126KB, 3,420 lines), `client.ts` (390 lines), `withRetry.ts` (823 lines), `errors.ts` (1,208 lines), `src/services/mcp/client.ts` (~119KB), `src/services/analytics/growthbook.ts` (~41KB), `src/services/lsp/LSPServerManager.ts` (421 lines)
>
> **One-liner**: The services layer is Claude Code's nervous system — a multi-provider API client factory, a 700-line streaming query engine, a battle-hardened retry strategy matrix, a GrowthBook-powered feature flag system, MCP protocol integration, and an LSP bridge — all wired together by AsyncGenerator pipelines and closure-factory patterns.

---

## Architecture Overview

```
src/services/
├── api/                    # Anthropic API client (~300K)
│   ├── claude.ts           # Core queryModel engine (126K, 3,420 lines)
│   ├── client.ts           # Multi-provider client factory (16K)
│   ├── withRetry.ts        # Retry strategy engine (28K, 823 lines)
│   ├── errors.ts           # Error classification system (42K)
│   ├── logging.ts          # API telemetry & diagnostics (24K)
│   ├── bootstrap.ts        # Bootstrap API requests
│   ├── filesApi.ts         # File upload API
│   ├── promptCacheBreakDetection.ts  # Cache hit analysis (26K)
│   └── sessionIngress.ts   # Session log persistence (17K)
├── mcp/                    # MCP protocol integration (~250K)
│   ├── client.ts           # MCP client lifecycle (119K)
│   ├── auth.ts             # OAuth/XAA authentication (89K)
│   ├── MCPConnectionManager.tsx  # React connection context
│   ├── types.ts            # Zod schema configuration
│   └── config.ts           # Multi-source config merge (51K)
├── analytics/              # Feature flags & telemetry
│   ├── growthbook.ts       # GrowthBook integration (41K)
│   ├── index.ts            # Event analytics pipeline
│   └── sink.ts             # Sink architecture (DD + 1P BQ)
├── lsp/                    # Language Server Protocol
│   ├── LSPServerManager.ts # Closure-factory manager (13K)
│   ├── manager.ts          # Global singleton + generation counter (10K)
│   └── passiveFeedback.ts  # Diagnostic notification handlers
├── compact/                # Context compression (→ see Episode 11)
├── oauth/                  # OAuth 2.0 client
├── plugins/                # Plugin marketplace management
├── policyLimits/           # Organization policy enforcement
├── remoteManagedSettings/  # Remote config sync
├── teamMemorySync/         # Team memory synchronization
└── extractMemories/        # Auto-memory extraction
```

### Design Principles

Five architectural invariants govern the entire services layer:

1. **Multi-Provider Abstraction** — A single `getAnthropicClient()` factory produces SDK instances for Anthropic 1P, AWS Bedrock, Azure Foundry, and Google Vertex, with dynamic `await import()` to avoid bundling unused SDKs.
2. **Fail-Open for Non-Critical Services** — Enterprise features (`policyLimits`, `remoteManagedSettings`) degrade gracefully on failure. The core query loop never blocks on them.
3. **Session-Stable Latch Mechanism** — Once a beta header is sent (e.g., `fast_mode`, `afk_mode`), it stays on for the entire session. This prevents prompt cache key changes mid-conversation — a single cache-key flip can multiply costs by 12×.
4. **AsyncGenerator Pipelines** — The core API call chain (`queryModel → withRetry → stream handler`) is wired with `AsyncGenerator<StreamEvent | AssistantMessage | SystemAPIErrorMessage>`, enabling callers to process intermediate retry/error events while waiting for the final result.
5. **Closure Factories over Classes** — State-bearing services (LSP manager, cached microcompact) use `createXxxManager()` functions with closure-scoped private state, eliminating `this` binding issues.

---

## 1. The Client Factory — Four Providers, One Interface

The entry point to the entire API subsystem is `getAnthropicClient()` in `client.ts`. It returns a single `Anthropic` SDK instance regardless of the underlying provider:

```typescript
// 源码位置: src/services/api/client.ts:88-100
export async function getAnthropicClient({
  apiKey, maxRetries, model, fetchOverride, source,
}: { ... }): Promise<Anthropic> {
  const defaultHeaders: { [key: string]: string } = {
    'x-app': 'cli',
    'User-Agent': getUserAgent(),
    'X-Claude-Code-Session-Id': getSessionId(),
    ...customHeaders,
  }
  await checkAndRefreshOAuthTokenIfNeeded()
  // ... builds ARGS with proxy, timeout (600s default), etc.
```

### Provider Dispatch Chain

The factory uses environment variable detection to select the provider, with dynamic imports to keep unused SDKs out of the bundle:

```
CLAUDE_CODE_USE_BEDROCK=1  → await import('@anthropic-ai/bedrock-sdk')
CLAUDE_CODE_USE_FOUNDRY=1  → await import('@anthropic-ai/foundry-sdk')
CLAUDE_CODE_USE_VERTEX=1   → await import('@anthropic-ai/vertex-sdk')
(default)                  → new Anthropic(...)
```

Each provider branch returns `as unknown as Anthropic` — a deliberate type lie. The Bedrock, Foundry, and Vertex SDKs have slightly different type signatures, but the `queryModel` caller treats them uniformly. The comment in source is refreshingly honest: *"we have always been lying about the return type."*

### Bedrock Specifics

```typescript
// 源码位置: src/services/api/client.ts:153-189
if (isEnvTruthy(process.env.CLAUDE_CODE_USE_BEDROCK)) {
  const { AnthropicBedrock } = await import('@anthropic-ai/bedrock-sdk')
  const awsRegion =
    model === getSmallFastModel() && process.env.ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION
      ? process.env.ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION
      : getAWSRegion()
  // Bearer token auth (AWS_BEARER_TOKEN_BEDROCK) or STS credential refresh
}
```

A small but important detail: Bedrock supports per-model region overrides. The `ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION` variable lets you route Haiku to a different region than your primary model — useful when your Opus region is overloaded.

### The `buildFetch` Wrapper

```typescript
// 源码位置: src/services/api/client.ts:358-389
function buildFetch(fetchOverride, source): ClientOptions['fetch'] {
  const inner = fetchOverride ?? globalThis.fetch
  const injectClientRequestId =
    getAPIProvider() === 'firstParty' && isFirstPartyAnthropicBaseUrl()
  return (input, init) => {
    const headers = new Headers(init?.headers)
    if (injectClientRequestId && !headers.has(CLIENT_REQUEST_ID_HEADER)) {
      headers.set(CLIENT_REQUEST_ID_HEADER, randomUUID())
    }
    return inner(input, { ...init, headers })
  }
}
```

This small wrapper solves a real-world debugging pain: when API requests time out, the server returns no request ID. By injecting a client-side `x-client-request-id` UUID into every first-party request, the API team can still correlate timeouts with server-side logs.

---

## 2. queryModel — The 700-Line Heart

`queryModel()` in `claude.ts` is the single most important function in the entire codebase. At ~700 lines, it orchestrates everything from GrowthBook kill-switch checks to streaming event accumulation. Here's the lifecycle:

```
queryModel() entry
  │
  ├─ 1. Off-switch check (GrowthBook: tengu-off-switch)
  ├─ 2. Beta headers assembly (getMergedBetas)
  ├─ 3. Tool search filtering (deferred tools only if discovered)
  ├─ 4. Tool schema building (parallel generation)
  ├─ 5. Message normalization (normalizeMessagesForAPI)
  ├─ 6. Beta header latching (fast_mode, afk_mode, cache_editing)
  ├─ 7. paramsFromContext closure (complete API request construction)
  ├─ 8. withRetry wrapper (→ see §3)
  ├─ 9. Raw SSE stream consumption (→ see §4)
  └─ 10. AssistantMessage yield
```

### The Off-Switch

```typescript
// 源码位置: src/services/api/claude.ts:1028-1049
if (
  !isClaudeAISubscriber() &&
  isNonCustomOpusModel(options.model) &&
  (await getDynamicConfig_BLOCKS_ON_INIT<{ activated: boolean }>(
    'tengu-off-switch', { activated: false }
  )).activated
) {
  logEvent('tengu_off_switch_query', {})
  yield getAssistantMessageFromError(
    new Error(CUSTOM_OFF_SWITCH_MESSAGE), options.model
  )
  return
}
```

This is Claude Code's emergency brake. When Opus is under extreme load, Anthropic can remotely disable it for PAYG users in real-time via GrowthBook. The check is gated behind `isNonCustomOpusModel()` and `!isClaudeAISubscriber()` so it doesn't affect subscribers or custom models. The message politely suggests switching to Sonnet.

Note the ordering optimization: cheap synchronous checks (`isClaudeAISubscriber`, `isNonCustomOpusModel`) execute before the `await getDynamicConfig_BLOCKS_ON_INIT` which blocks on GrowthBook initialization (~10ms).

### Tool Search Filtering

```typescript
// 源码位置: src/services/api/claude.ts:1128-1172
const deferredToolNames = new Set<string>()
if (useToolSearch) {
  for (const t of tools) {
    if (isDeferredTool(t)) deferredToolNames.add(t.name)
  }
}
// Only include deferred tools that have been discovered via tool_reference blocks
const discoveredToolNames = extractDiscoveredToolNames(messages)
filteredTools = tools.filter(tool => {
  if (!deferredToolNames.has(tool.name)) return true
  if (toolMatchesName(tool, TOOL_SEARCH_TOOL_NAME)) return true
  return discoveredToolNames.has(tool.name)
})
```

This is the dynamic tool loading system. Instead of sending all 42+ tools to the API on every request (expensive in tokens), tools marked as `deferred` are only included after they've been "discovered" via `tool_reference` blocks in the conversation history. The `ToolSearchTool` itself is always included so it can discover more tools.

### The paramsFromContext Closure

The crown jewel of `queryModel` is the `paramsFromContext` closure (~190 lines), which constructs the complete API request. It's a closure rather than a standalone function because it captures the entire request-building context (messages, system prompt, tools, betas):

```typescript
// 源码位置: src/services/api/claude.ts:1538-1729
const paramsFromContext = (retryContext: RetryContext) => {
  const betasParams = [...betas]
  // ... configure effort, task budget, thinking, context management
  // ... latch fast_mode, afk_mode, cache_editing headers
  return {
    model: normalizeModelStringForAPI(options.model),
    messages: addCacheBreakpoints(messagesForAPI, ...),
    system,
    tools: allTools,
    betas: betasParams,
    metadata: getAPIMetadata(),
    max_tokens: maxOutputTokens,
    thinking,
    // ... speed, context_management, output_config
  }
}
```

Why is it called multiple times? It's invoked for logging (fire-and-forget), for the actual API request, and potentially for non-streaming fallback retries. The `RetryContext` parameter allows the retry loop to override `maxTokensOverride` when a context overflow error shrinks the available output budget.

### Beta Header Latching

```typescript
// 源码位置: src/services/api/claude.ts:1642-1689
// Fast mode: header is latched session-stable (cache-safe), but
// speed='fast' stays dynamic so cooldown still suppresses the actual
// fast-mode request without changing the cache key.
if (fastModeHeaderLatched && !betasParams.includes(FAST_MODE_BETA_HEADER)) {
  betasParams.push(FAST_MODE_BETA_HEADER)
}
// AFK mode beta: latched once auto mode is first activated.
if (afkHeaderLatched && shouldIncludeFirstPartyOnlyBetas() && isAgenticQuery) {
  betasParams.push(AFK_MODE_BETA_HEADER)
}
```

The latch pattern is deceptively simple but solves a critical cost problem: prompt cache keys include beta headers. If `fast_mode` toggles on and off mid-session, every toggle invalidates the cache. With ~20K tokens of system prompt, a single cache miss costs significantly more than the prompt itself. The latch ensures the header, once activated, stays on for the session — the `speed='fast'` parameter still toggles dynamically to control behavior, but the cache key stays stable.

---

## 3. The Retry Strategy Engine

`withRetry.ts` (823 lines) wraps every API call with a sophisticated retry state machine:

```typescript
// 源码位置: src/services/api/withRetry.ts:170-178
export async function* withRetry<T>(
  getClient: () => Promise<Anthropic>,
  operation: (client: Anthropic, attempt: number, context: RetryContext) => Promise<T>,
  options: RetryOptions,
): AsyncGenerator<SystemAPIErrorMessage, T> {
```

The `AsyncGenerator` return type is the key design choice: callers get intermediate `SystemAPIErrorMessage` events between retries, which the UI renders as "Retrying in X seconds..." status messages.

### The Retry Decision Matrix

| Error | Strategy | Rationale |
|-------|----------|-----------|
| **429 Rate Limit** | Wait `retry-after`, or fast-mode cooldown | Honors server directive |
| **529 Overloaded** | Max 3 retries → fallback to Sonnet | Prevents cascade amplification |
| **401 Unauthorized** | Force OAuth token refresh → retry | Token may have expired |
| **403 Token Revoked** | `handleOAuth401Error()` → retry | Another process refreshed token |
| **400 Context Overflow** | Reduce `max_tokens` → retry | Shrink output to fit context |
| **ECONNRESET/EPIPE** | Disable keep-alive → retry | Stale socket detected |
| **Non-foreground 529** | Bail immediately | Reduces backend amplification |

### Foreground Source Whitelist

```typescript
// 源码位置: src/services/api/withRetry.ts:62-82
const FOREGROUND_529_RETRY_SOURCES = new Set<QuerySource>([
  'repl_main_thread', 'sdk', 'agent:custom', 'agent:default',
  'compact', 'hook_agent', 'auto_mode', ...
])
```

This is a critical anti-amplification measure. During a capacity cascade, each retry multiplies backend load by 3-10×. Background queries (summaries, titles, classifiers) that hit 529 bail immediately — the user never sees them fail. Only foreground queries where the user is actively waiting get retried.

### Persistent Retry Mode

```typescript
// 源码位置: src/services/api/withRetry.ts:96-98
const PERSISTENT_MAX_BACKOFF_MS = 5 * 60 * 1000    // 5 min max
const PERSISTENT_RESET_CAP_MS = 6 * 60 * 60 * 1000 // 6 hour cap
const HEARTBEAT_INTERVAL_MS = 30_000                // 30s keep-alive
```

For unattended CI/CD sessions (`CLAUDE_CODE_UNATTENDED_RETRY`), the retry loop runs indefinitely. Every 30 seconds during the wait, it yields a heartbeat `SystemAPIErrorMessage` so the host environment (Docker, CI runner) doesn't consider the session idle and kill it.

### Fast Mode Retry Logic

```typescript
// 源码位置: src/services/api/withRetry.ts:284-304
const retryAfterMs = getRetryAfterMs(error)
if (retryAfterMs !== null && retryAfterMs < SHORT_RETRY_THRESHOLD_MS) {
  // Short retry-after: wait and retry with fast mode still active
  await sleep(retryAfterMs, options.signal, { abortError })
  continue
}
// Long or unknown: enter cooldown (switch to standard speed)
const cooldownMs = Math.max(
  retryAfterMs ?? DEFAULT_FAST_MODE_FALLBACK_HOLD_MS, MIN_COOLDOWN_MS
)
triggerFastModeCooldown(Date.now() + cooldownMs, cooldownReason)
```

Fast mode retries split into two paths. Short `retry-after` (< 20s): keep fast mode active and cache key stable. Long or absent `retry-after`: enter cooldown, switch to standard speed. The `MIN_COOLDOWN_MS` floor prevents flip-flopping between fast and standard within seconds.

---

## 4. Streaming Architecture

### Why Raw SSE Instead of SDK Streams

```typescript
// 源码位置: src/services/api/claude.ts:1818-1836
// Use raw stream instead of BetaMessageStream to avoid O(n²) partial JSON parsing
const result = await anthropic.beta.messages
  .create({ ...params, stream: true }, { signal, ... })
  .withResponse()
stream = result.data  // Stream<BetaRawMessageStreamEvent>
```

The Anthropic SDK's `BetaMessageStream` calls `partialParse()` on every `input_json_delta` event. For long tool inputs, this grows quadratically — each delta re-parses the accumulated JSON from scratch. Claude Code bypasses this by consuming raw SSE events and accumulating content blocks manually.

### The Stream Event State Machine

```
message_start
  → Initialize usage, capture research metadata
  → Record TTFB (time to first token)

content_block_start
  → Create new block (text | thinking | tool_use | server_tool_use | connector_text)
  → Initialize accumulator: text='', thinking='', input=''

content_block_delta
  → Incremental append: text_delta | input_json_delta | thinking_delta | signature_delta
  → Type-safe accumulation (each delta type matches its block type)

content_block_stop
  → Build completed content block
  → Parse accumulated JSON for tool_use inputs: JSON.parse(accumulated)

message_delta
  → Update usage counters, capture stop_reason

message_stop
  → Finalize AssistantMessage, yield to caller
```

### The Idle Watchdog

```typescript
// 源码位置: src/services/api/claude.ts:1874-1928
const streamWatchdogEnabled = isEnvTruthy(process.env.CLAUDE_ENABLE_STREAM_WATCHDOG)
const STREAM_IDLE_TIMEOUT_MS = parseInt(...) || 90_000  // 90s default
const STREAM_IDLE_WARNING_MS = STREAM_IDLE_TIMEOUT_MS / 2  // 45s warning

streamIdleTimer = setTimeout(() => {
  streamIdleAborted = true
  releaseStreamResources()
}, STREAM_IDLE_TIMEOUT_MS)
```

Silently dropped connections are a real problem with SSE streams. The SDK's request timeout only covers the initial `fetch()`, not the streaming body. The watchdog monitors inter-chunk gaps: warning at 45 seconds, abort at 90 seconds. Without this, a hung stream blocks the session indefinitely.

---

## 5. Prompt Cache — Three-Layer Strategy

```typescript
// 源码位置: src/services/api/claude.ts:358-374
export function getCacheControl({ scope, querySource } = {}) {
  return {
    type: 'ephemeral',
    ...(should1hCacheTTL(querySource) && { ttl: '1h' }),
    ...(scope === 'global' && { scope }),
  }
}
```

| Layer | TTL | Scope | Eligibility |
|-------|-----|-------|------------|
| **Ephemeral** | 5 min (default) | Per-user | Everyone |
| **1-Hour** | 1h | Per-user | Subscribers + GrowthBook allowlist match |
| **Global** | 5 min | Cross-user | System prompt when MCP tools are stable |

### 1h TTL Eligibility & Latching

```typescript
// 源码位置: src/services/api/claude.ts:393-434
function should1hCacheTTL(querySource?: QuerySource): boolean {
  // 3P Bedrock users: opt-in via ENABLE_PROMPT_CACHING_1H_BEDROCK
  // 1P users: eligibility is latched in bootstrap state
  let userEligible = getPromptCache1hEligible()
  if (userEligible === null) {
    userEligible =
      process.env.USER_TYPE === 'ant' ||
      (isClaudeAISubscriber() && !currentLimits.isUsingOverage)
    setPromptCache1hEligible(userEligible)  // Latch!
  }
  // Allowlist is also latched to prevent mixed TTLs mid-session
  let allowlist = getPromptCache1hAllowlist()
  if (allowlist === null) {
    const config = getFeatureValue_CACHED_MAY_BE_STALE('tengu_prompt_cache_1h_config', {})
    allowlist = config.allowlist ?? []
    setPromptCache1hAllowlist(allowlist)  // Latch!
  }
  // Pattern matching with trailing * wildcard support
  return allowlist.some(pattern =>
    pattern.endsWith('*')
      ? querySource.startsWith(pattern.slice(0, -1))
      : querySource === pattern
  )
}
```

Both eligibility and the GrowthBook allowlist are latched in bootstrap state on first evaluation. This prevents a mid-session overage flip (subscriber runs out of quota) from changing cache TTL — which would bust the server-side cache with a ~20K token penalty per flip.

---

## 6. Error Classification System

`errors.ts` (1,208 lines) is the largest file in the API directory. It provides structured error classification for every API failure mode:

```typescript
// 源码位置: src/services/api/errors.ts:85-96
export function parsePromptTooLongTokenCounts(rawMessage: string) {
  const match = rawMessage.match(
    /prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)/i
  )
  return {
    actualTokens: match ? parseInt(match[1]!, 10) : undefined,
    limitTokens: match ? parseInt(match[2]!, 10) : undefined,
  }
}
```

### Error Classification Hierarchy

```
getAssistantMessageFromError(error, model)
  ├─ APIConnectionTimeoutError → "Request timed out"
  ├─ ImageSizeError / ImageResizeError → "Image too large"
  ├─ CUSTOM_OFF_SWITCH_MESSAGE → "Opus under load, switch to Sonnet"
  ├─ 429 Rate Limit
  │   ├─ Unified headers present → getRateLimitErrorMessage(limits)
  │   └─ No headers → generic "Request rejected (429)"
  ├─ prompt too long → PROMPT_TOO_LONG_ERROR_MESSAGE + errorDetails
  ├─ PDF errors → too large / password protected / invalid
  ├─ image exceeds / many-image → size error messages
  ├─ tool_use/tool_result mismatch → concurrency error + /rewind hint
  ├─ organization disabled → stale API key guidance
  ├─ credit balance too low → billing error
  └─ (default) → formatted API error string
```

The error system does double duty: it provides user-facing messages for the terminal UI, and machine-readable `errorDetails` strings for reactive compact's retry logic. `getPromptTooLongTokenGap()` parses the actual-vs-limit gap from `errorDetails`, letting compact jump past multiple message groups in one retry instead of peeling one at a time.

---

## 7. GrowthBook Feature Flags

`growthbook.ts` (1,156 lines) integrates the GrowthBook feature flag platform, serving as Claude Code's remote configuration backbone.

### Two Read Modes

```typescript
// Non-blocking: returns cached/stale value immediately
export function getFeatureValue_CACHED_MAY_BE_STALE<T>(feature, defaultValue): T {
  // Priority: env overrides → config overrides → in-memory payload → disk cache → default
}

// Blocking: waits for GrowthBook initialization to complete
export async function getDynamicConfig_BLOCKS_ON_INIT<T>(feature, defaultValue): Promise<T> {
  const growthBookClient = await initializeGrowthBook()  // blocks ~10ms
  // ...
}
```

`CACHED_MAY_BE_STALE` is the hot-path choice — used hundreds of times per session in render loops, permission checks, and model selection. It reads from an in-memory Map first, then falls back to disk cache (`~/.claude.json`). `BLOCKS_ON_INIT` is reserved for critical-path decisions like the off-switch, where a wrong answer has serious consequences.

### User Attributes for Targeting

```typescript
// 源码位置: src/services/analytics/growthbook.ts:32-47
export type GrowthBookUserAttributes = {
  id: string                    // Device ID (stable across sessions)
  sessionId: string             // Per-session unique ID
  platform: 'win32' | 'darwin' | 'linux'
  organizationUUID?: string     // Enterprise org targeting
  accountUUID?: string          // Individual account targeting
  userType?: string             // 'ant' (internal) | 'external'
  subscriptionType?: string     // Pro, Max, Enterprise, etc.
  rateLimitTier?: string        // Rate limit tier (for gradual rollouts)
  appVersion?: string           // Version-specific feature gates
}
```

### Remote Eval Workaround

GrowthBook's remote eval mode evaluates flags server-side (so targeting rules stay private), but the SDK returns `{ value: ... }` while expecting `{ defaultValue: ... }`. Claude Code works around this with `processRemoteEvalPayload()`:

```typescript
// 源码位置: src/services/analytics/growthbook.ts:327-394
async function processRemoteEvalPayload(gbClient: GrowthBook): Promise<boolean> {
  const payload = gbClient.getPayload()
  // Transform: { value: ... } → { defaultValue: ... }
  // Cache in remoteEvalFeatureValues Map (bypasses SDK's re-evaluation)
  // Store experiment data for exposure logging
  // Sync to disk for cross-process stability
}
```

The flag values are synced to disk via `syncRemoteEvalToDisk()` after every successful payload load. This serves as a cross-process cache: a new Claude Code process reads disk-cached flags immediately at startup, before GrowthBook's network init completes.

---

## 8. LSP Integration — Closure Factory Pattern

The LSP (Language Server Protocol) integration exemplifies Claude Code's preference for closure factories over classes:

```typescript
// 源码位置: src/services/lsp/LSPServerManager.ts:59-65
export function createLSPServerManager(): LSPServerManager {
  // Private state managed via closures
  const servers: Map<string, LSPServerInstance> = new Map()
  const extensionMap: Map<string, string[]> = new Map()
  const openedFiles: Map<string, string> = new Map()
  // ... 360 lines of closure methods
  return { initialize, shutdown, getServerForFile, ensureServerStarted, sendRequest, ... }
}
```

### File-Based Routing

When Claude Code reads or edits a file, the LSP manager routes the notification to the correct language server based on file extension:

```
.ts, .tsx, .js  →  TypeScript Language Server
.py             →  Python Language Server (Pyright/Pylsp)
.rs             →  rust-analyzer
(etc.)
```

### The Generation Counter

```typescript
// 源码位置: src/services/lsp/manager.ts:32-36
let initializationState: InitializationState = 'not-started'
let initializationGeneration = 0

// On reinitialize (after plugin refresh):
const currentGeneration = ++initializationGeneration
lspManagerInstance.initialize().then(() => {
  // Only update state if this is still the current initialization
  if (currentGeneration === initializationGeneration) {
    initializationState = 'success'
  }
})
```

This solves a subtle race condition: if `reinitializeLspServerManager()` is called while a previous initialization is still in-flight, the generation counter ensures the stale initialization's `.then()` handler is silently discarded. Without this, a slow init could overwrite state from a newer init.

### Deferred Tool Loading for LSP

```typescript
// 源码位置: src/services/api/claude.ts:786-793
function shouldDeferLspTool(tool: Tool): boolean {
  if (!('isLsp' in tool) || !tool.isLsp) return false
  const status = getInitializationStatus()
  return status.status === 'pending' || status.status === 'not-started'
}
```

LSP tools (code diagnostics, definition lookups) participate in the tool search system: they're deferred until the language server is ready, then lazily included once discovered.

---

## 9. MCP Integration Highlights

The MCP (Model Context Protocol) subsystem is covered in detail in [Episode 4: Plugin System](./04-plugin-system), but its service-layer aspects deserve mention here.

### Six Transport Types

```typescript
// 源码位置: src/services/mcp/types.ts
export const TransportSchema = z.enum([
  'stdio',    // Local subprocess (stdin/stdout) — most common
  'sse',      // Server-Sent Events (remote)
  'http',     // Streamable HTTP (MCP 2025 spec)
  'ws',       // WebSocket (IDE extensions)
  'sdk',      // In-process SDK-controlled transport
  'sse-ide',  // SSE via IDE bridge
])
```

### MCPConnectionManager (React Context)

```typescript
// 源码位置: src/services/mcp/MCPConnectionManager.tsx
export function MCPConnectionManager({ children, dynamicMcpConfig, isStrictMcpConfig }) {
  const { reconnectMcpServer, toggleMcpServer } = useManageMCPConnections(...)
  return (
    <MCPConnectionContext.Provider value={{ reconnectMcpServer, toggleMcpServer }}>
      {children}
    </MCPConnectionContext.Provider>
  )
}
```

The MCP connection lifecycle is managed through React context, enabling hot-reload of MCP servers without restarting the session. The React Compiler optimizes this with `_c(6)` memo slots.

---

## Transferable Design Patterns

> The following patterns can be directly applied to other Agentic systems or CLI tools.

### Pattern 1: Session-Stable Latch

**Scenario:** A feature flag or beta header controls a behavior that also serves as a cache key.
**Problem:** Toggling the flag mid-session invalidates the cache, causing severe cost amplification.
**Practice:** Latch the flag value on first evaluation. The latch stores a boolean in bootstrap state; once set to `true`, it never reverts within the session.

```typescript
let headerLatched = false
function ensureLatched(condition: boolean) {
  if (!headerLatched && condition) {
    headerLatched = true
    setHeaderLatched(true)  // Persist in bootstrap state
  }
  return headerLatched
}
```

**Claude Code applies this to:** `fast_mode`, `afk_mode`, `cache_editing` beta headers, 1h prompt cache eligibility, and the GrowthBook allowlist.

### Pattern 2: AsyncGenerator Retry Pipeline

**Scenario:** A long-running operation needs retry logic, but callers also need visibility into retry status.
**Problem:** Traditional retry wrappers block until success or final failure. The caller can't show "Retrying..." UI.
**Practice:** Make the retry wrapper an `AsyncGenerator` that `yield`s status events between attempts and `return`s the final result.

```typescript
async function* withRetry<T>(
  operation, options
): AsyncGenerator<StatusEvent, T> {
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try { return await operation(attempt) }
    catch (error) {
      yield { type: 'retry', attempt, delayMs, error }
      await sleep(delayMs)
    }
  }
}
```

### Pattern 3: Closure Factory with Generation Counter

**Scenario:** A singleton service that may need re-initialization (e.g., after plugin refresh).
**Problem:** Async init can overlap: `init_1` starts, `reinit` starts `init_2`, but `init_1` finishes second and overwrites `init_2`'s state.
**Practice:** Increment a generation counter on each init. The `.then()` callback checks if its generation is still current before updating state.

### Pattern 4: Foreground/Background Retry Classification

**Scenario:** A service experiences cascading failures (e.g., API overload).
**Problem:** Retrying all queries amplifies the overload 3-10× per retry layer.
**Practice:** Maintain a whitelist of "foreground" query sources (user is waiting). Background queries (summaries, classifiers, titles) bail immediately on overload errors. This is anti-amplification by design.

---

## Source Coordinates

| Component | Path | Lines | Key Function |
|-----------|------|-------|-------------|
| Client Factory | `services/api/client.ts` | 390 | `getAnthropicClient()` |
| Query Engine | `services/api/claude.ts` | 3,420 | `queryModel()` L1017 |
| Retry Engine | `services/api/withRetry.ts` | 823 | `withRetry()` L170 |
| Error System | `services/api/errors.ts` | 1,208 | `getAssistantMessageFromError()` L425 |
| Cache Control | `services/api/claude.ts` | — | `getCacheControl()` L358 |
| GrowthBook | `services/analytics/growthbook.ts` | 1,156 | `getFeatureValue_CACHED_MAY_BE_STALE()` L734 |
| LSP Manager | `services/lsp/LSPServerManager.ts` | 421 | `createLSPServerManager()` L59 |
| LSP Singleton | `services/lsp/manager.ts` | 290 | `initializeLspServerManager()` L145 |
| MCP Client | `services/mcp/client.ts` | ~3,000 | MCP lifecycle management |
| MCP Types | `services/mcp/types.ts` | — | `TransportSchema`, `ConfigScopeSchema` |
| Analytics | `services/analytics/index.ts` | — | `logEvent()`, PII protection |

---

*The services layer is where Claude Code's ambitions meet the real world's failure modes.*

[← Episode 14 — UI & State Management](./14-ui-state-management) · [Episode 16 — Infrastructure & Config →](./16-infrastructure-config)
