> 🌐 **Language**: [中文版 →](README.md) | English

[![📖 Read Online](https://img.shields.io/badge/📖_Read_Online-GitHub_Pages-D97757?style=for-the-badge&logo=github)](https://openedclaude.github.io/claude-reviews-claude/)

> 💡 **Recommended**: This project is deployed to [GitHub Pages](https://openedclaude.github.io/claude-reviews-claude/) with full-text search, dark mode, and chapter navigation — a much better reading experience than raw GitHub Markdown.

# 🪞 Claude Reviews Claude Code

*An AI reading its own source code. Yes, really. Anthropic probably didn't see this coming either.*

*🍿 Season 1 complete | All 17 episodes | Claude reverse-engineers itself faster than it writes code.*

*Don't miss an episode — Star ⭐ to subscribe.*

[![Stars](https://img.shields.io/github/stars/openedclaude/claude-reviews-claude?style=social)](https://github.com/openedclaude/claude-reviews-claude)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/openedclaude/claude-reviews-claude)](https://github.com/openedclaude/claude-reviews-claude)

> **This entire analysis was written by Claude — about the source code that powers Claude Code.**
>
> 1,902 files. 477,439 lines of TypeScript. One model reading the code that defines how it thinks, acts, and executes.
>
> What you're reading is Claude's own architectural decomposition of Claude Code v2.1.88: how the query engine loops, how 42 tools are orchestrated, how multi-agent workers coordinate in parallel — all analyzed by the very model these systems were built to serve.
>
> *If you think this is absurd, imagine how the AI writing this analysis feels.*

---

## 🏗️ What's Inside

This is **not** a source code dump. It's a structured engineering analysis — architecture diagrams, code walkthroughs, and design patterns — written by Claude after reading Claude Code's TypeScript source.

| # | Topic | What You'll Learn | Deep Dive |
|---|-------|-------------------|-----------|
| 0 | **Architecture Overview** | Full landscape of 17 subsystems, engineering excellence, transferable design patterns | [Read →](https://openedclaude.github.io/claude-reviews-claude/overview) |
| 1 | **QueryEngine: The Brain** | How the 1,296-line core engine manages LLM queries, tool loops, and session state | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/01-query-engine) |
| 2 | **Tool System Architecture** | How 42+ tools are registered, validated, and executed as self-contained modules | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/02-tool-system) |
| 3 | **Multi-Agent Coordinator** | How Claude Code spawns parallel workers, routes messages, and synthesizes results | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/03-coordinator) |
| 4 | **Plugin System** | How plugins are loaded, validated, and integrated (18.8K lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/04-plugin-system) |
| 5 | **Hook System** | PreToolUse / PostToolUse / SessionStart extensibility (8K lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/05-hook-system) |
| 6 | **Bash Execution Engine** | Secure command execution, sandbox, pipe management (11.5K lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/06-bash-engine) |
| 7 | **Permission Pipeline** | Defense-in-depth: config rules → tool checks → OS sandbox (9.5K lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/07-permission-pipeline) |
| 8 | **Agent Swarms** | Multi-agent team coordination: mailbox IPC, backend detection, permission delegation (6.8K lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/08-agent-swarms) |
| 9 | **Session Persistence** | Append-only JSONL storage, parent-UUID chains, 64KB lite resume (7.6K lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/09-session-persistence) |
| 10 | **Context Assembly** | Three-layer context assembly: system prompts, CLAUDE.md memory, per-turn attachments (8.3K lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/10-context-assembly) |
| 11 | **Compact System** | Three-tier compaction: microcompact, session memory compact, LLM summarization (3.9K lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/11-compact-system) |
| 12 | **Startup & Bootstrap** | Fast-path cascade, dynamic imports, API preconnection, global state singleton (7.6K+ lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/12-startup-bootstrap) |
| 13 | **Bridge System** | Remote control protocol, dual transport generations, poll-dispatch loop, crash recovery (11.7K lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/13-bridge-system) |
| 14 | **UI & State Management** | Ink rendering engine, React reconciler, Vim mode, Computer Use (140+ components) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/14-ui-state-management) |
| 15 | **Services & API Layer** | API client, stream reassembly, MCP server management, OAuth auth (12K lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/15-services-api-layer) |
| 16 | **Infrastructure & Config** | Settings merge pipeline, GrowthBook feature flags, telemetry, build system (15K lines) | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/16-infrastructure-config) |
| 17 | **Telemetry, Privacy & Ops** | Dual-channel analytics, model codenames, undercover mode, remote killswitches, future roadmap | [Read →](https://openedclaude.github.io/claude-reviews-claude/chapters/17-telemetry-privacy-operations) |

> ⭐ **Enjoy the meta? Star the repo — an AI analyzing itself deserves at least that.**

---

## 📦 Source Code Access

This project's analysis is based on the TypeScript source of Claude Code v2.1.88. If you'd like to read the source code yourself, these community repositories provide the reconstructed codebase:

| Repository | Description |
|-----------|-------------|
| [instructkr/claw-code](https://github.com/instructkr/claw-code) | Reconstructed Claude Code source |
| [ChinaSiro/claude-code-sourcemap](https://github.com/ChinaSiro/claude-code-sourcemap) | Original TypeScript extracted from Source Map |

---

## 🧠 Architecture Overview

Claude Code is a **1,902-file, 477K-line TypeScript** codebase running on **Bun**, with a terminal UI built on **React + Ink**.

### Six Pillars

```
                        ┌─────────────────────────┐
                        │     System Prompt        │
                        │  (Identity + Rules +     │
                        │   42+ Tool Descriptions) │
                        └────────────┬────────────┘
                                     │
                  ┌──────────────────┼──────────────────┐
                  │                  │                  │
         ┌───────▼────────┐ ┌──────▼───────┐ ┌───────▼────────┐
         │  🔧 Tool System │ │  ⚙️ Query    │ │  📦 Context    │
         │  (42+ tools,    │ │  Loop        │ │  Management    │
         │   30+ methods)  │ │  (12-step    │ │  (4-layer      │
         │                 │ │   state      │ │   compression) │
         │                 │ │   machine)   │ │                │
         └───────┬────────┘ └──────┬───────┘ └───────┬────────┘
                  │                  │                  │
                  └──────────────────┼──────────────────┘
                                     │
                  ┌──────────────────┼──────────────────┐
                  │                  │                  │
         ┌───────▼────────┐ ┌──────▼───────┐ ┌───────▼────────┐
         │  🔐 Permission  │ │  🤖 Multi-   │ │  🧩 Skill &    │
         │  & Security     │ │  Agent       │ │  Plugin        │
         │  (7-layer       │ │  Swarm       │ │  (6 sources,   │
         │   defense)      │ │  (3 backends,│ │   MCP proto)   │
         │                 │ │   7 tasks)   │ │                │
         └────────────────┘ └──────────────┘ └────────────────┘
```

### The Core Loop: A "Dumb Loop" That Drives Everything

```
    User Input
      │
      ▼
    QueryEngine.query()  ◄──────────────────────┐
      │                                          │
      ▼                                          │
    Claude API (streaming)                       │
      │                                          │
      ├── stop_reason = end_turn? ──► Response    │
      │                                          │
      └── stop_reason = tool_use?                │
            │                                    │
            ▼                                    │
          🔐 Permission → 🔧 Execute → Inject ──┘
```

> **Design philosophy:** Intelligence lives in the LLM; the scaffold is just a loop. 42+ tools, 7-layer security, 4-layer compaction, multi-agent coordination — all of it is a **production-grade harness** wrapped around this loop.

### Six Subsystems at a Glance

| Subsystem | Core Capability | Key Numbers | Details |
|-----------|----------------|------------|---------|
| ⚙️ **Query Engine** | while(true) tool loop + streaming + error recovery | 12-step state machine | [EP01](https://openedclaude.github.io/claude-reviews-claude/chapters/01-query-engine) |
| 🔧 **Tool System** | File/Bash/Search/Agent/MCP, Schema-driven registration | 42+ tools, 30+ method contract | [EP02](https://openedclaude.github.io/claude-reviews-claude/chapters/02-tool-system) |
| 🔐 **Permission & Security** | Rule matching → AST analysis → YOLO classifier → OS sandbox | 7-layer defense-in-depth | [EP07](https://openedclaude.github.io/claude-reviews-claude/chapters/07-permission-pipeline) |
| 📦 **Context Management** | Micro → snip → auto → reactive compression | 4-layer cascade, 200K context | [EP11](https://openedclaude.github.io/claude-reviews-claude/chapters/11-compact-system) |
| 🤖 **Multi-Agent** | iTerm2/tmux/in-process backends, divide-and-conquer | 7 task types | [EP08](https://openedclaude.github.io/claude-reviews-claude/chapters/08-agent-swarms) |
| 🖥️ **Terminal UI** | Forked Ink + React 19, Vim mode, IDE bridge | 140+ components | [EP14](https://openedclaude.github.io/claude-reviews-claude/chapters/14-ui-state-management) |

> 📐 Full architecture diagrams and reading paths → [Architecture Overview](https://openedclaude.github.io/claude-reviews-claude/overview)

---

## 📊 Codebase at a Glance

| Metric | Value |
|--------|-------|
| Total Files | ~1,900 |
| Total Lines | 512,664 |
| Language | TypeScript (strict mode) |
| Runtime | Bun |
| Largest File | `main.tsx` (808 KB — bundled entrypoint) |
| Core Engine | `QueryEngine.ts` (1,296 lines) + `query.ts` (70K) |
| Built-in Tools | 42 modules |
| Slash Commands | 50+ |
| Ink UI Components | 140+ |
| Feature Flags | `PROACTIVE`, `KAIROS`, `BRIDGE_MODE`, `DAEMON`, `VOICE_MODE`, `AGENT_TRIGGERS`, `MONITOR_TOOL`, `COORDINATOR_MODE`, `HISTORY_SNIP` |

---

## 📁 Repository Structure

```
claude-code-deep-dive/
├── README.md                          ← 中文版 README
├── README_EN.md                       ← You are here
├── DISCLAIMER.md / DISCLAIMER_CN.md   # Legal & ethical notice
│
├── architecture/                      # 🏗️ Architecture Deep Dives (17 episodes)
│   ├── 00-overview.md                 # Architecture overview
│   ├── 01-query-engine.md             # QueryEngine: the brain
│   ├── 02-tool-system.md              # 42-module tool architecture
│   ├── ...                            # 03-13 subsystems
│   ├── 14-ui-state-management.md      # UI & state management
│   ├── 15-services-api-layer.md       # Services & API layer
│   ├── 16-infrastructure-config.md    # Infrastructure & config
│   ├── 17-telemetry-privacy-operations.md  # Telemetry, privacy & ops
│   └── zh-CN/                         # 🇨🇳 Chinese translations (18 episodes)
│       ├── 00-overview.md
│       └── ...
```

---

## 🔑 Key Insights Preview

### 1. The Core Loop Is Deliberately "Dumb"

Claude Code's `query()` function is a simple AsyncGenerator loop: send messages → get response → if tool_use, execute tool → push result → repeat. All intelligence lives in the LLM. This is a deliberate architectural choice — a "dumb scaffold, smart model" philosophy.

### 2. Multi-Agent Coordination Is a First-Class Feature

The Coordinator mode transforms Claude Code from a single-agent CLI into an orchestrator that dispatches parallel worker agents. Workers are fully isolated — they can't see the coordinator's conversation history. The coordinator must synthesize worker results and craft self-contained prompts.

### 3. Dead Code Elimination via Feature Flags

`bun:bundle` feature flags are used at build time to completely strip unused code paths. Voice mode, proactive mode, coordinator mode — all are gated behind compile-time flags, producing a smaller binary.

### 4. Permission System Uses Defense-in-Depth

Permissions are checked at multiple levels: app-level config rules → tool-specific permission models → OS-level sandbox (seccomp on Linux, seatbelt on macOS). When sandbox is active, certain operations auto-approve — trading application-layer checks for kernel-level guarantees.

---

## 📌 Roadmap

**Architecture Series** (All 17 episodes — Complete ✅)
- [x] Architecture Overview — full landscape of 17 subsystems
- [x] QueryEngine deep dive — the brain of Claude Code
- [x] Tool system walkthrough — 42 modules, one interface
- [x] Multi-agent coordinator — parallel workers, fork mechanism
- [x] Plugin system — loading, validation, integration (18.8K lines)
- [x] Hook system — PreToolUse / PostToolUse / SessionStart (8K lines)
- [x] Bash execution engine — sandbox, pipe management (11.5K lines)
- [x] Permission pipeline — defense-in-depth, sandbox (9.5K lines)
- [x] Swarm agents — multi-agent group coordination (6.8K lines)
- [x] Session persistence — conversation storage (7.6K lines)
- [x] Context assembly — attachments, memory, skills (8.3K lines)
- [x] Compact system — auto-compaction, microcompact (3.9K lines)
- [x] Startup & bootstrap — fast-path cascade, dynamic imports (7.6K+ lines)
- [x] Bridge system — remote control protocol (11.7K lines)
- [x] UI & state management — Ink rendering, Vim mode (140+ components)
- [x] Services & API layer — stream reassembly, MCP servers (12K lines)
- [x] Infrastructure & config — settings merge, feature flags, telemetry (15K lines)
- [x] Telemetry, Privacy & Ops — dual-channel analytics, undercover mode, remote killswitches (825 lines)

**Localization**
- [x] Full bilingual EN/ZH for all 18 episodes

---

## ⭐ Support This Project

If this analysis was helpful:

1. **⭐ Star** this repository
2. **🔀 Fork** to add your own analysis
3. **📢 Share** on Twitter, Reddit, or Hacker News

Every star helps more developers discover this deep dive.

## ⭐ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=openedclaude/claude-reviews-claude&type=Date)](https://star-history.com/#openedclaude/claude-reviews-claude&Date)

---

## 📜 License & Disclaimer

This analysis is released under the [MIT License](LICENSE). See [DISCLAIMER.md](DISCLAIMER.md) for important legal and ethical notices.

Analysis based on `@anthropic-ai/claude-code@2.1.88`. All code snippets are brief excerpts used for educational commentary. The original source remains the property of **Anthropic, PBC**.
