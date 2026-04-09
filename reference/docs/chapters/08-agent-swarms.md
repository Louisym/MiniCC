# 08 — Agent Swarms: Multi-Agent Team Coordination

> 🌐 **Language**: English | [中文版 →](/zh-CN/chapters/08-agent-swarms)
> 📖 **[Read Online →](https://openedclaude.github.io/claude-reviews-claude/chapters/08-agent-swarms)** — Sidebar nav, dark mode & full-text search. Better than raw GitHub.


> **Scope**: `tools/TeamCreateTool/`, `tools/SendMessageTool/`, `tools/shared/spawnMultiAgent.ts`, `utils/swarm/` (~30 files, ~6.8K lines), `utils/teammateMailbox.ts` (1,184 lines)
>
> **One-liner**: How Claude Code spawns parallel teammates across tmux panes, iTerm2 splits, or in-process — all coordinated through a file-based mailbox system with lockfile concurrency control.

---

## Architecture Overview

![08 agent swarms 1](../assets/08-agent-swarms-1.svg)

---

## 1. Team Lifecycle

### Creating a Team

`TeamCreateTool` initializes the team infrastructure:

1. Generate unique team name (word-slug if collision)
2. Create team leader entry with deterministic agent ID: `team-lead@{teamName}`
3. Write `config.json` to `~/.claude/teams/{team-name}/`
4. Register for session cleanup (auto-delete on exit if not explicitly deleted)
5. Reset task list directory for fresh task numbering

```typescript
// Team file structure
type TeamFile = {
  name: string
  description?: string
  createdAt: number
  leadAgentId: string          // "team-lead@my-project"
  leadSessionId?: string       // For team discovery
  teamAllowedPaths?: TeamAllowedPath[]  // Shared edit permissions
  members: Array<{
    agentId: string            // "researcher@my-project"
    name: string
    agentType?: string
    model?: string
    prompt?: string
    color?: string             // Unique color for UI identification
    planModeRequired?: boolean
    tmuxPaneId: string
    cwd: string
    worktreePath?: string      // Git worktree for isolation
    subscriptions: string[]
    backendType?: BackendType  // "tmux" | "iterm2" | "in-process"
    isActive?: boolean
    mode?: PermissionMode
  }>
}
```

### Spawning Teammates

`spawnMultiAgent.ts` (1,094 lines) handles the full spawn flow:

1. **Resolve model**: `inherit` → leader's model; `undefined` → hardcoded fallback
2. **Generate unique name**: Check existing members, append `-2`, `-3`, etc.
3. **Detect backend**: tmux > iTerm2 > in-process (see §3)
4. **Create pane/process**: Backend-specific spawn
5. **Build CLI args**: propagate `--agent-id`, `--team-name`, `--agent-color`, `--permission-mode`
6. **Register in team file**: Add member entry to `config.json`
7. **Send initial message**: Write prompt to teammate's mailbox
8. **Register background task**: For UI task pill display

---

## 2. The Mailbox System

The inter-agent communication backbone is a **file-based mailbox** with lockfile concurrency control.

### Directory Structure

```
~/.claude/teams/{team-name}/
├── config.json              # Team manifest
└── inboxes/
    ├── team-lead.json       # Leader's inbox
    ├── researcher.json      # Teammate inbox
    └── test-runner.json     # Teammate inbox
```

### Message Types

| Type | Direction | Purpose |
|------|----------|---------|
| Plain text DM | Any → Any | Direct messages |
| Broadcast (`to: "*"`) | Leader → All | Team-wide announcements |
| `idle_notification` | Worker → Leader | "I'm done / blocked / failed" |
| `permission_request` | Worker → Leader | Tool permission delegation |
| `permission_response` | Leader → Worker | Permission grant/deny |
| `sandbox_permission_request` | Worker → Leader | Network access approval |
| `plan_approval_request` | Worker → Leader | Plan review for `planModeRequired` |
| `shutdown_request` | Leader → Worker | Graceful shutdown |
| `shutdown_approved/rejected` | Worker → Leader | Shutdown confirmation |

### Concurrency Control

```typescript
const LOCK_OPTIONS = {
  retries: { retries: 10, minTimeout: 5, maxTimeout: 100 }
}
// Write with lock
release = await lockfile.lock(inboxPath, {
  lockfilePath: `${inboxPath}.lock`, ...LOCK_OPTIONS
})
const messages = await readMailbox(recipientName, teamName)
messages.push(newMessage)
await writeFile(inboxPath, jsonStringify(messages, null, 2))
release()
```

Multiple Claude instances in a swarm can write concurrently — the lockfile serializes access with exponential backoff retries.

---

## 3. Backend Detection and Execution

Three backends determine how teammates physically run:

<p align="center">
  <img src="../assets/08-agent-swarms-2.svg" width="340">
</p>

### Backend Comparison

| Feature | Tmux | iTerm2 | In-Process |
|---------|------|--------|------------|
| Isolation | Separate process | Separate process | Same process, separate query loop |
| UI visibility | Pane with colored border | Native iTerm2 pane | Background task pill |
| Prerequisite | tmux installed | `it2` CLI installed | None |
| Non-interactive mode (`-p`) | ❌ | ❌ | ✅ (forced) |
| Socket isolation | PID-scoped: `claude-swarm-{pid}` | N/A | N/A |

### In-Process Fallback

When no pane backend is available (no tmux, no iTerm2), `markInProcessFallback()` is called and subsequent spawns skip detection entirely, running teammates as in-process query loops sharing the same Node.js process.

---

## 4. Permission Delegation

Teammates don't have interactive terminals — they delegate permission decisions to the leader:

### Permission Flow

1. Worker needs permission → creates `permission_request` message
2. Writes to leader's mailbox via `teammateMailbox.ts`
3. Leader's `useInboxPoller` picks up the request
4. Leader shows permission prompt to user
5. Leader sends `permission_response` back to worker's mailbox
6. Worker polls inbox, gets response, continues or aborts

### Plan Mode Required

Some teammates can be spawned with `plan_mode_required: true`:

- Teammate **must** enter plan mode and create a plan
- Plan is sent to leader as `plan_approval_request`
- Leader reviews and sends `plan_approval_response` with `approved: true/false`
- On approval, leader's permission mode is inherited (unless it's `plan`, which maps to `default`)

---

## 5. Agent Identity System

```typescript
// Deterministic agent ID format
function formatAgentId(name: string, teamName: string): string {
  return `${name}@${teamName}`  // "researcher@my-project"
}

// Leader is always
const TEAM_LEAD_NAME = 'team-lead'  // → "team-lead@my-project"
```

### CLI Flag Propagation

When spawning teammates, the leader propagates:

| Flag | Condition | Purpose |
|------|-----------|---------|
| `--dangerously-skip-permissions` | bypass mode + NOT planModeRequired | Inherit permission bypass |
| `--permission-mode auto` | auto mode | Inherit classifier |
| `--permission-mode acceptEdits` | acceptEdits mode | Inherit edit auto-allow |
| `--model {model}` | Explicit model override | Use leader's model |
| `--settings {path}` | CLI settings path | Share settings |
| `--plugin-dir {dir}` | Inline plugins | Share plugins |
| `--parent-session-id {id}` | Always | Lineage tracing |

---

## 6. Team Cleanup

### Graceful Shutdown

`SendMessage(type: shutdown_request)` → teammate responds `shutdown_approved/rejected`:

- **Approved**: In-process teammates abort their query loop; pane teammates call `gracefulShutdown(0)` via `setImmediate`
- **Rejected**: Teammate provides reason, continues working

### Session Cleanup

`cleanupSessionTeams()` runs on leader exit (registered with `gracefulShutdown`):

1. Kill orphaned teammate panes (still running after SIGINT)
2. Remove team directory: `~/.claude/teams/{team-name}/`
3. Remove task directory: `~/.claude/tasks/{team-name}/`
4. Destroy git worktrees for isolated teammates

---

## Transferable Design Patterns

> The following patterns from the Swarm system can be directly applied to any multi-agent or distributed coordination architecture.

### Why File-Based Mailboxes?

The mailbox system uses plain JSON files with lockfiles instead of IPC, WebSockets, or shared memory. This works because:

- **Cross-process**: tmux panes are separate processes with no shared memory
- **Crash-safe**: Messages persist on disk even if a teammate crashes
- **Debuggable**: `cat ~/.claude/teams/my-team/inboxes/researcher.json`
- **Simple**: No daemon, no port allocation, no service discovery

### The Backend Abstraction

The `TeammateExecutor` interface and `PaneBackend` abstraction mean the swarm system doesn't care *how* teammates run — only that they can receive initial prompts and exchange messages. Adding a new backend (e.g., Docker containers, SSH sessions) would only require implementing the `PaneBackend` interface.

### One Leader, Many Workers

The architecture enforces a strict leader-worker hierarchy:
- Only one team per leader session
- Workers cannot create teams or approve their own plans
- Shutdown is always leader-initiated, worker-approved
- Permission delegation always flows worker → leader → worker

---

## 8. Coordinator Mode

**Source coordinates**: `src/coordinator/coordinatorMode.ts`

Coordinator mode transforms the leader from a task dispatcher into a **synthesis engine** — it doesn't just delegate work, it understands and integrates results.

### 8.1 Activation: Double Gate

```typescript
// 源码位置: src/coordinator/coordinatorMode.ts
export function isCoordinatorMode(): boolean {
  if (feature('COORDINATOR_MODE')) {
    return isEnvTruthy(process.env.CLAUDE_CODE_COORDINATOR_MODE)
  }
  return false
}
```

Both the build-time feature flag AND the runtime environment variable must be enabled. When resuming a session, `matchSessionMode()` automatically flips the variable to match the resumed session's mode.

### 8.2 Coordinator Workflow

```
Research (Workers, parallel)
   ↓ Results returned via <task-notification>
Synthesis (Coordinator)
   ↓ Coordinator integrates findings
Implementation (Workers, file-serial)
   ↓ Write operations serialized per file set
Verification (Workers, parallel)
```

Core principles:
- **Coordinator owns synthesis** — no "based on your findings" delegation; the coordinator must understand and restate
- **Parallel is the superpower** — independent workers run concurrently
- **Read-write isolation** — research tasks parallel, write ops serialized by file sets
- **Continue vs Spawn** — context overlap determines whether to reuse a worker or create a new one

### 8.3 Worker Context Injection

```typescript
export function getCoordinatorUserContext(
  mcpClients: ReadonlyArray<{ name: string }>,
  scratchpadDir?: string,
): { [k: string]: string } {
  // Tells coordinator what tools workers have access to
  // Injects MCP server info for tool routing decisions
  // Provides scratchpad directory for no-prompt file sharing
}
```

---

## 9. Task Type Union (7 Variants)

**Source coordinates**: `src/tasks/`

Every background task in Claude Code is represented by one of seven state variants:

```typescript
export type TaskState =
  | LocalShellTaskState         // Local shell command execution
  | LocalAgentTaskState         // Sub-agent via AgentTool
  | RemoteAgentTaskState        // Remote CCR agent
  | InProcessTeammateTaskState  // Same-process team member
  | LocalWorkflowTaskState      // Local workflow execution
  | MonitorMcpTaskState         // MCP server monitoring
  | DreamTaskState              // Auto-memory consolidation
```

### Progress Tracking

```typescript
export type AgentProgress = {
  toolUseCount: number
  tokenCount: number
  lastActivity?: ToolActivity
  recentActivities?: ToolActivity[]  // Last 5 tool uses
  summary?: string
}

// input_tokens are CUMULATIVE (take latest), output_tokens are PER-TURN (sum)
export function updateProgressFromMessage(tracker, message): void {
  tracker.latestInputTokens = usage.input_tokens + cache_creation + cache_read
  tracker.cumulativeOutputTokens += usage.output_tokens
}
```

### Agent Completion Notification

```xml
<task-notification>
  <task-id>{agentId}</task-id>
  <status>completed|failed|killed</status>
  <summary>{description}</summary>
  <result>{agent final text response}</result>
  <usage>
    <total_tokens>N</total_tokens>
    <tool_uses>N</tool_uses>
    <duration_ms>N</duration_ms>
  </usage>
</task-notification>
```

The notification is injected as a `user` type message, so the LLM processes it naturally in the conversation flow.

---

## 10. Inter-Agent Communication Protocol

**Source coordinates**: `src/tools/SendMessageTool/`

### 10.1 Structured Message Types

Beyond plain text, agents can exchange typed control messages:

```typescript
const StructuredMessage = z.discriminatedUnion('type', [
  z.object({ type: z.literal('shutdown_request'), reason: z.string().optional() }),
  z.object({ type: z.literal('shutdown_response'), request_id: z.string(), approve: semanticBoolean() }),
  z.object({ type: z.literal('plan_approval_response'), request_id: z.string(), approve: semanticBoolean(), feedback: z.string().optional() }),
])

// Input schema: `to` field supports multiple routing targets
const inputSchema = z.object({
  to: z.string(),      // "name" | "*" (broadcast) | "uds:path" | "bridge:session_id"
  summary: z.string().optional(),
  message: z.union([z.string(), StructuredMessage]),
})
```

### 10.2 Message Routing

```
SendMessage(to="researcher", message="...")
  ↓
InProcessTeammate? → findTeammateTaskByAgentId → direct pendingMessages
  ↓ No
LocalAgent? → queuePendingMessage → consumed at tool-round boundary
  ↓ No
Pane (tmux/iterm2)? → writeToMailbox → filesystem mailbox
  ↓ No
UDS/Bridge? → socket/bridge transport
  ↓ No
"*" (broadcast)? → iterate all team members, send individually
```

### 10.3 Cross-Session Communication (UDS)

When `feature('UDS_INBOX')` is enabled, Claude Code sessions on the same machine can communicate via Unix Domain Sockets:

```typescript
// "uds:/tmp/cc-socks/1234.sock" — local Claude session
// "bridge:session_01AbCd..." — Remote Control session
// Messages wrapped as <cross-session-message from="..."> XML
```

---

## 11. DreamTask & UltraPlan

### DreamTask: Automatic Memory Consolidation

**Source coordinates**: `src/tasks/` — `DreamTaskState`

```typescript
export type DreamTaskState = TaskStateBase & {
  type: 'dream'
  phase: 'starting' | 'updating'
  sessionsReviewing: number
  filesTouched: string[]    // Incomplete — only Edit/Write tool_use, misses bash writes
  turns: DreamTurn[]        // Last 30 turns
  abortController?: AbortController
  priorMtime: number        // Rollback lock on kill
}
```

DreamTask runs a background agent that reviews recent session history and consolidates learnings into `MEMORY.md`. The `priorMtime` field acts as a rollback lock — if the dream is killed mid-write, the system can restore the file to its pre-dream state.

### UltraPlan: Orchestrated Remote Execution

```typescript
export type RemoteAgentTaskState = TaskStateBase & {
  type: 'remote_agent'
  remoteTaskType: RemoteTaskType  // 'ultraplan' | 'ultrareview' | 'autofix-pr' | ...
  isUltraplan?: boolean
  ultraplanPhase?: 'needs_input' | 'plan_ready'
  // ...
}
```

UltraPlan extends the agent paradigm to remote execution via CCR (Claude Code Runner), enabling plan-then-execute workflows where the plan is generated remotely and must receive user approval before implementation begins.

> → For the full inventory of 11 unreleased agent tools (SleepTool, PushNotificationTool, SubscribePRTool, DaemonTool, CoordinatorTool, MorerightTool, DreamConsolidationTool, DxtTool, UltraplanTool, VoiceInputTool, BuddyTool) and their feature gates, see [Episode 17: Telemetry & Ops](./17-telemetry-privacy-operations) §9.5.

---

## Summary

| Component | Lines | Role |
|----------|-------|------|
| `spawnMultiAgent.ts` | 1,094 | Unified teammate spawn logic |
| `teammateMailbox.ts` | 1,184 | File-based mailbox with lockfile concurrency |
| `teamHelpers.ts` | 684 | Team file CRUD, cleanup, worktree management |
| `SendMessageTool.ts` | 918 | DM, broadcast, shutdown, plan approval |
| `TeamCreateTool.ts` | 241 | Team initialization |
| `backends/registry.ts` | 465 | Backend detection: tmux > iTerm2 > in-process |
| `constants.ts` | 34 | Team names, session names, env vars |
| `teammateLayoutManager.ts` | ~400 | Pane creation, color assignment, border status |

The swarm system is Claude Code's most operationally complex feature — it combines process management, file-based IPC, terminal multiplexing, and distributed permission delegation into a cohesive multi-agent framework. The file-based mailbox design prioritizes simplicity and debuggability over performance, which is the right trade-off when your "distributed system" is multiple AI agents sharing a single filesystem.

---

**Previous**: [← 07 — Permission Pipeline](./07-permission-pipeline)
**Next**: [→ 09 — Session Persistence](./09-session-persistence)
