# Episode 11: The Compaction System — How Claude Code Manages Infinite Conversations

> 🌐 **Language**: English | [中文版 →](/zh-CN/chapters/11-compact-system)
> 📖 **[Read Online →](https://openedclaude.github.io/claude-reviews-claude/chapters/11-compact-system)** — Sidebar nav, dark mode & full-text search. Better than raw GitHub.


> **Source files**: `compact.ts` (1,706 lines), `autoCompact.ts` (352 lines), `microCompact.ts` (531 lines), `sessionMemoryCompact.ts` (631 lines), `prompt.ts` (375 lines), `grouping.ts` (64 lines), `postCompactCleanup.ts` (100 lines), `apiMicrocompact.ts` (140 lines), `compactWarningState.ts` (20 lines), `timeBasedMCConfig.ts` (49 lines)
>
> **One-liner**: Claude Code's compaction system is a multi-tier memory management architecture — from surgical cache-editing of individual tool results to full LLM-powered conversation summarization — all designed to maintain the illusion of infinite context.

## Architecture Overview

<p align="center">
  <img src="../assets/11-compact-system.svg" width="720">
</p>

---

## The Three Tiers of Compaction

Claude Code uses a tiered approach to context management, each tier trading off precision for compression ratio:

| Tier | Mechanism | Trigger | Compression | Cache Impact |
|------|-----------|---------|-------------|--------------|
| **MicroCompact** | Clear old tool results | Every turn (time-based or count-based) | ~10-50K tokens | Preserves (cache_edits) or rebuilds (content-clear) |
| **Session Memory** | Replace old messages with pre-built memory | Auto-compact threshold | ~60-80% | Invalidates, but no LLM call |
| **Full Compact** | LLM summarizes entire conversation | Auto-compact or manual `/compact` | ~80-95% | Invalidates, costs 1 API call |

---

## Tier 1: MicroCompact — Surgical Token Reclamation

MicroCompact (`microCompact.ts`, 531 lines) operates on every turn, surgically removing old tool results without touching the conversation structure.

### Which Tools Get Compacted?

```typescript
// 源码位置: src/utils/compact/microCompact.ts:35-48
const COMPACTABLE_TOOLS = new Set([
  FILE_READ_TOOL_NAME,    // FileRead
  ...SHELL_TOOL_NAMES,    // Bash, PowerShell
  GREP_TOOL_NAME,         // Grep
  GLOB_TOOL_NAME,         // Glob
  WEB_SEARCH_TOOL_NAME,   // WebSearch
  WEB_FETCH_TOOL_NAME,    // WebFetch
  FILE_EDIT_TOOL_NAME,    // FileEdit
  FILE_WRITE_TOOL_NAME,   // FileWrite
])
```

Only high-volume, reproducible tool results are targeted. Tool results from AgentTool, MCP tools, etc. are preserved.

### Two MicroCompact Paths

**Path A: Time-Based MicroCompact (Cold Cache)**

When the gap since the last assistant message exceeds a threshold (server cache has expired):

```typescript
function maybeTimeBasedMicrocompact(messages, querySource) {
  const trigger = evaluateTimeBasedTrigger(messages, querySource)
  // If gap > threshold minutes, content-clear old tool results
  // Keep last N results, replace rest with '[Old tool result content cleared]'
}
```

This is the "brute force" path — it directly mutates message content because the cache is cold anyway. No point using cache_edits when everything will be rewritten.

**Path B: Cached MicroCompact (Warm Cache, Ant-Only)**

When the server cache is warm, uses the `cache_edits` API to remove tool results without invalidating the cached prefix:

```typescript
// Does NOT modify local messages — cache_reference and cache_edits 
// are added at the API layer
const cacheEdits = mod.createCacheEditsBlock(state, toolsToDelete)
pendingCacheEdits = cacheEdits  // Consumed by API layer
```

The key insight: cached MC never touches local message content. It queues deletion instructions that the API layer injects as `cache_edits` blocks. The server removes the tool results from its cached copy, preserving the prompt cache hit.

### Token Estimation

```typescript
function estimateMessageTokens(messages: Message[]): number {
  // Walk content blocks, estimate tokens for text/images/thinking/tool_use
  // Pad estimate by 4/3 to be conservative
  return Math.ceil(totalTokens * (4 / 3))
}
```

Images and PDFs are estimated at a flat 2,000 tokens. Thinking blocks count only the text, not the JSON wrapper or signature.

---

## Tier 2: Session Memory Compact — The Shortcut

Session Memory Compact (`sessionMemoryCompact.ts`, 631 lines) is an experimental optimization that avoids the full LLM summarization call.

### How It Works

Instead of asking the LLM to summarize the conversation, it uses the **session memory** (a continuously-maintained summary updated by a background agent) as the compaction summary. This eliminates the cost and latency of the compaction API call.

```
Before: [msg1, msg2, ..., msg_summarized, ..., msg_recent1, msg_recent2]
After:  [boundary, session_memory_summary, msg_recent1, msg_recent2]
```

### Message Preservation Strategy

```typescript
const DEFAULT_SM_COMPACT_CONFIG = {
  minTokens: 10_000,            // Keep at least 10K tokens
  minTextBlockMessages: 5,       // Keep at least 5 messages with text
  maxTokens: 40_000,            // Hard cap at 40K tokens
}
```

Starting from the last summarized message, the system expands backwards until both minimums are met, capped at `maxTokens`.

### API Invariant Preservation

The most complex part is `adjustIndexToPreserveAPIInvariants()` (80+ lines), which ensures:

1. **Tool pairs aren't split**: Every `tool_result` in the kept range must have its matching `tool_use` in a preceding assistant message
2. **Thinking blocks aren't orphaned**: If assistant messages share the same `message.id` (from streaming), all related messages must be kept together

```typescript
// Step 1: Find orphaned tool_results, pull in their tool_use messages
// Step 2: Find assistant messages with same message.id as kept ones
// Both steps walk backwards from the cut point
```

---

## Tier 3: Full Compact — The Nuclear Option

`compactConversation()` (`compact.ts`, lines 387-763) performs a full LLM-powered conversation summarization.

### The Compaction Pipeline

```
1. PreCompact hooks          — Let extensions modify/inspect before compact
2. stripImagesFromMessages() — Replace images with [image] markers
3. stripReinjectedAttachments() — Remove skill_discovery/skill_listing
4. streamCompactSummary()    — Fork agent generates summary (with PTL retry)
5. formatCompactSummary()    — Strip <analysis> scratchpad, keep <summary>
6. Clear file state cache    — readFileState.clear()
7. Restore post-compact context:
   - Top 5 recently-read files (50K token budget, 5K per file)
   - Invoked skills (25K budget, 5K per skill)
   - Active plan content
   - Plan mode instructions
   - Deferred tool deltas
   - Agent listing deltas
   - MCP instruction deltas
8. SessionStart hooks        — Re-run as if starting a new session
9. PostCompact hooks         — Let extensions react to compaction
10. Re-append session metadata — Keep title in 16KB tail window
```

### The Summary Prompt

The compaction prompt (`prompt.ts`) instructs the model to produce a structured 9-section summary:

1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections (with full snippets)
4. Errors and Fixes
5. Problem Solving
6. All User Messages (critical for intent tracking)
7. Pending Tasks
8. Current Work
9. Optional Next Step (with verbatim quotes)

The prompt uses an `<analysis>` scratchpad block that gets stripped from the final summary — a "thinking out loud" space that improves summary quality without consuming post-compact tokens.

### Prompt-Too-Long Recovery (CC-1180)

When the compact request *itself* hits the API's prompt-too-long limit:

```typescript
for (;;) {
  summaryResponse = await streamCompactSummary(...)
  if (!summary?.startsWith(PROMPT_TOO_LONG_ERROR_MESSAGE)) break
  
  // Drop oldest API-round groups until the gap is covered
  const truncated = truncateHeadForPTLRetry(messagesToSummarize, response)
  // Max 3 retries, each time dropping more from the head
}
```

The system groups messages by API round (assistant message ID boundaries), then drops the oldest groups until the token gap is covered. Fallback: drop 20% of groups when the gap is unparseable.

### Post-Compact File Restoration

```typescript
const POST_COMPACT_MAX_FILES_TO_RESTORE = 5
const POST_COMPACT_TOKEN_BUDGET = 50_000
const POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
const POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
const POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000
```

After compaction, the system re-injects the most recently-read files (up to 5, budget 50K tokens) and invoked skills (budget 25K tokens) as attachments so the model doesn't immediately need to re-read them.

---

## Auto-Compact Trigger Logic

`autoCompact.ts` (352 lines) manages the automatic triggering of compaction.

### Threshold Calculation

```typescript
// 源码位置: src/utils/compact/autoCompact.ts:25-35
function getAutoCompactThreshold(model: string): number {
  const effectiveContextWindow = getEffectiveContextWindowSize(model)
  return effectiveContextWindow - AUTOCOMPACT_BUFFER_TOKENS  // - 13,000
}

function getEffectiveContextWindowSize(model: string): number {
  const contextWindow = getContextWindowForModel(model)
  const reserved = Math.min(getMaxOutputTokensForModel(model), 20_000)
  return contextWindow - reserved
}
```

For a 200K context model: effective window ≈ 180K, auto-compact threshold ≈ 167K tokens.

### Warning State Machine

```typescript
function calculateTokenWarningState(tokenUsage, model) {
  return {
    percentLeft,                    // Visual indicator
    isAboveWarningThreshold,        // effectiveWindow - 20K
    isAboveErrorThreshold,          // effectiveWindow - 20K  
    isAboveAutoCompactThreshold,    // effectiveWindow - 13K
    isAtBlockingLimit,              // effectiveWindow - 3K (manual compact required)
  }
}
```

### Circuit Breaker

```typescript
// 源码位置: src/utils/compact/autoCompact.ts:180-185
const MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
// BQ 2026-03-10: 1,279 sessions had 50+ consecutive failures
// wasting ~250K API calls/day globally
```

After 3 consecutive failures, auto-compact stops trying for the session. This prevents sessions with irrecoverably large contexts from hammering the API.

### Recursion Guards

```typescript
if (querySource === 'session_memory' || querySource === 'compact') {
  return false  // Don't compact the compactor
}
if (querySource === 'marble_origami') {
  return false  // Don't compact the context-collapse agent
}
```

---

## Message Grouping

`grouping.ts` (64 lines) provides the fundamental operation of splitting conversations at API round boundaries.

```typescript
function groupMessagesByApiRound(messages: Message[]): Message[][] {
  // Boundary fires when a NEW assistant response begins
  // (different message.id from prior assistant)
  // Streaming chunks from same response share an id → same group
}
```

This is critical for two operations:
1. **PTL retry truncation** — drop oldest groups to fit compact request
2. **Reactive compact** — the API's 413 response triggers compaction that peels from the tail

---

## Partial Compact

`partialCompactConversation()` (`compact.ts`, line 772) supports two directions:

| Direction | What's Summarized | What's Kept | Cache Impact |
|-----------|-------------------|-------------|--------------|
| `'from'` | Messages after pivot | Earlier messages | **Preserved** — kept messages are prefix |
| `'up_to'` | Messages before pivot | Later messages | **Invalidated** — summary precedes kept |

The `'from'` direction is the cache-friendly option: the prompt cache for the kept (earlier) messages survives.

---

## Transferable Design Patterns

> The following patterns can be directly applied to other LLM-based systems or context management architectures.

### Pattern 1: Multi-Tier Compression Pipeline
**Scenario:** A growing context window needs management at different urgency levels.
**Practice:** Layer surgical (per-turn), lightweight (pre-built summary), and heavy (LLM-powered) compaction tiers, each with increasing compression ratio and cost.
**Claude Code application:** MicroCompact → Session Memory Compact → Full Compact.

### Pattern 2: Analysis Scratchpad (Strip-Before-Inject)
**Scenario:** An LLM summarization task benefits from chain-of-thought, but the output must be compact.
**Practice:** Provide an `<analysis>` block for the model to think in, then strip it from the final output.
**Claude Code application:** `formatCompactSummary()` strips `<analysis>` before injecting the summary.

### Pattern 3: Circuit Breaker for Expensive Operations
**Scenario:** A repeatedly failing expensive operation (API call) wastes resources.
**Practice:** Track consecutive failures and stop retrying after N failures per session.
**Claude Code application:** `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3` prevents runaway API calls.

---

## Component Summary

| Component | Lines | Role |
|-----------|-------|------|
| `compact.ts` | 1,706 | Core compaction: full compact, partial compact, PTL retry, post-compact restoration |
| `sessionMemoryCompact.ts` | 631 | Session memory compact: message preservation, API invariant fixing |
| `microCompact.ts` | 531 | MicroCompact: cached MC, time-based MC, tool result clearing |
| `prompt.ts` | 375 | Compaction prompts: 9-section summary template, analysis scratchpad |
| `autoCompact.ts` | 352 | Auto-compact triggers: threshold calculation, circuit breaker, warning state |
| `apiMicrocompact.ts` | 140 | API-layer cache_edits integration |
| `postCompactCleanup.ts` | 100 | Post-compact cache resets and memory file re-loads |
| `grouping.ts` | 64 | Message grouping by API round boundaries |
| `timeBasedMCConfig.ts` | 49 | Time-based MC configuration (gap threshold, keep-recent) |
| `compactWarningState.ts` | 20 | Warning suppression state after successful MC |

---

*Next: [Episode 12 — Startup & Bootstrap →](./12-startup-bootstrap)*

[← Episode 10 — Context Assembly](./10-context-assembly) | [Episode 12 →](./12-startup-bootstrap)
