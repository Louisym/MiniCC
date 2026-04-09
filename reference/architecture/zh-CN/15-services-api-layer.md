# 第 15 集：服务层与 API 架构 —— 神经系统

> **源码文件**: `src/services/api/claude.ts` (~126KB, 3,420 行), `client.ts` (390 行), `withRetry.ts` (823 行), `errors.ts` (1,208 行), `src/services/mcp/client.ts` (~119KB), `src/services/analytics/growthbook.ts` (~41KB), `src/services/lsp/LSPServerManager.ts` (421 行)
>
> **一句话总结**: 服务层是 Claude Code 的神经系统——一个多提供商 API 客户端工厂、一个 700 行的流式查询引擎、一个久经沙场的重试策略矩阵、一个 GrowthBook 驱动的特性开关系统、MCP 协议集成和 LSP 桥接——全部通过 AsyncGenerator 管道和闭包工厂模式串联起来。

---

## 架构总览

```
src/services/
├── api/                    # Anthropic API 客户端 (~300K)
│   ├── claude.ts           # 核心 queryModel 引擎 (126K, 3,420 行)
│   ├── client.ts           # 多提供商客户端工厂 (16K)
│   ├── withRetry.ts        # 重试策略引擎 (28K, 823 行)
│   ├── errors.ts           # 错误分类系统 (42K)
│   ├── logging.ts          # API 遥测与诊断 (24K)
│   ├── bootstrap.ts        # 引导 API 请求
│   ├── filesApi.ts         # 文件上传 API
│   ├── promptCacheBreakDetection.ts  # 缓存命中分析 (26K)
│   └── sessionIngress.ts   # 会话日志持久化 (17K)
├── mcp/                    # MCP 协议集成 (~250K)
│   ├── client.ts           # MCP 客户端生命周期 (119K)
│   ├── auth.ts             # OAuth/XAA 认证 (89K)
│   ├── MCPConnectionManager.tsx  # React 连接上下文
│   ├── types.ts            # Zod Schema 配置
│   └── config.ts           # 多源配置合并 (51K)
├── analytics/              # 特性开关与遥测
│   ├── growthbook.ts       # GrowthBook 集成 (41K)
│   ├── index.ts            # 事件分析管道
│   └── sink.ts             # Sink 架构 (DD + 1P BQ)
├── lsp/                    # Language Server Protocol
│   ├── LSPServerManager.ts # 闭包工厂管理器 (13K)
│   ├── manager.ts          # 全局单例 + 代际计数器 (10K)
│   └── passiveFeedback.ts  # 诊断通知处理器
├── compact/                # 上下文压缩 (→ 见第 11 集)
├── oauth/                  # OAuth 2.0 客户端
├── plugins/                # 插件市场管理
├── policyLimits/           # 组织策略限制
├── remoteManagedSettings/  # 远程配置同步
├── teamMemorySync/         # 团队记忆同步
└── extractMemories/        # 自动记忆提取
```

### 设计原则

五个架构不变量贯穿整个服务层：

1. **多提供商抽象** —— 单个 `getAnthropicClient()` 工厂函数为 Anthropic 1P、AWS Bedrock、Azure Foundry 和 Google Vertex 生成 SDK 实例，通过动态 `await import()` 避免将未使用的 SDK 打入包中。
2. **非关键服务 Fail-Open** —— 企业功能（`policyLimits`、`remoteManagedSettings`）在失败时优雅降级。核心查询循环永远不会被它们阻塞。
3. **会话稳定锁存机制** —— 一旦发送了某个 beta header（如 `fast_mode`、`afk_mode`），它将在整个会话期间保持启用。这防止了对话中途 prompt cache 键的变化——单次缓存键翻转可以使成本增加 12 倍。
4. **AsyncGenerator 管道** —— 核心 API 调用链（`queryModel → withRetry → 流处理器`）通过 `AsyncGenerator<StreamEvent | AssistantMessage | SystemAPIErrorMessage>` 串联，使调用者能在等待最终结果的同时处理中间的重试/错误事件。
5. **闭包工厂替代 Class** —— 有状态的服务（LSP 管理器、缓存微压缩）使用 `createXxxManager()` 函数配合闭包作用域的私有状态，消除了 `this` 绑定问题。

---

## 1. 客户端工厂 —— 四个提供商，一个接口

整个 API 子系统的入口是 `client.ts` 中的 `getAnthropicClient()`。无论底层是哪个提供商，它都返回单一的 `Anthropic` SDK 实例：

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
  // ... 构建 ARGS（代理、超时 600s 默认值等）
```

### 提供商分发链

工厂通过环境变量检测来选择提供商，利用动态导入将未使用的 SDK 排除在打包之外：

```
CLAUDE_CODE_USE_BEDROCK=1  → await import('@anthropic-ai/bedrock-sdk')
CLAUDE_CODE_USE_FOUNDRY=1  → await import('@anthropic-ai/foundry-sdk')
CLAUDE_CODE_USE_VERTEX=1   → await import('@anthropic-ai/vertex-sdk')
（默认）                    → new Anthropic(...)
```

每个提供商分支都返回 `as unknown as Anthropic` —— 这是一个刻意的类型谎言。Bedrock、Foundry 和 Vertex SDK 的类型签名略有不同，但 `queryModel` 调用者统一处理。源码中的注释坦率得令人耳目一新：*"we have always been lying about the return type."*

### Bedrock 细节

```typescript
// 源码位置: src/services/api/client.ts:153-189
if (isEnvTruthy(process.env.CLAUDE_CODE_USE_BEDROCK)) {
  const { AnthropicBedrock } = await import('@anthropic-ai/bedrock-sdk')
  const awsRegion =
    model === getSmallFastModel() && process.env.ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION
      ? process.env.ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION
      : getAWSRegion()
  // Bearer token 认证 (AWS_BEARER_TOKEN_BEDROCK) 或 STS 凭证刷新
}
```

一个小但重要的细节：Bedrock 支持按模型指定区域。`ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION` 变量允许你将 Haiku 路由到与主模型不同的区域——当你的 Opus 区域过载时非常有用。

### `buildFetch` 包装器

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

这个小包装器解决了现实中的调试痛点：当 API 请求超时时，服务器不返回请求 ID。通过在每个第一方请求中注入客户端侧的 `x-client-request-id` UUID，API 团队仍然可以将超时与服务器日志关联起来。

---

## 2. queryModel —— 700 行的心脏

`claude.ts` 中的 `queryModel()` 是整个代码库中最重要的单个函数。约 700 行代码编排了从 GrowthBook 熔断检查到流式事件累积的一切：

```
queryModel() 入口
  │
  ├─ 1. 熔断开关检查 (GrowthBook: tengu-off-switch)
  ├─ 2. Beta headers 组装 (getMergedBetas)
  ├─ 3. 工具搜索过滤 (仅包含已发现的延迟加载工具)
  ├─ 4. 工具 Schema 构建 (并行生成)
  ├─ 5. 消息规范化 (normalizeMessagesForAPI)
  ├─ 6. Beta header 锁存 (fast_mode, afk_mode, cache_editing)
  ├─ 7. paramsFromContext 闭包 (完整 API 请求构建)
  ├─ 8. withRetry 包装器 (→ 见 §3)
  ├─ 9. 原始 SSE 流消费 (→ 见 §4)
  └─ 10. AssistantMessage 输出
```

### 熔断开关

```typescript
// 源码位置: src/services/api/claude.ts:1028-1049
if (
  !isClaudeAISubscriber() &&
  isNonCustomOpusModel(options.model) &&
  (await getDynamicConfig_BLOCKS_ON_INIT<{ activated: boolean }>(
    'tengu-off-switch', { activated: false }
  )).activated
) {
  yield getAssistantMessageFromError(
    new Error(CUSTOM_OFF_SWITCH_MESSAGE), options.model
  )
  return
}
```

这是 Claude Code 的紧急制动器。当 Opus 处于极端负载时，Anthropic 可以通过 GrowthBook 实时禁用它。检查被 `isNonCustomOpusModel()` 和 `!isClaudeAISubscriber()` 门控，不影响订阅用户或自定义模型。

注意顺序优化：廉价的同步检查在 `await getDynamicConfig_BLOCKS_ON_INIT`（阻塞等待 GrowthBook 初始化 ~10ms）之前执行。

### 工具搜索过滤

```typescript
// 源码位置: src/services/api/claude.ts:1128-1172
const deferredToolNames = new Set<string>()
if (useToolSearch) {
  for (const t of tools) {
    if (isDeferredTool(t)) deferredToolNames.add(t.name)
  }
}
// 仅包含通过 tool_reference 块发现的延迟加载工具
const discoveredToolNames = extractDiscoveredToolNames(messages)
filteredTools = tools.filter(tool => {
  if (!deferredToolNames.has(tool.name)) return true
  if (toolMatchesName(tool, TOOL_SEARCH_TOOL_NAME)) return true
  return discoveredToolNames.has(tool.name)
})
```

这是动态工具加载系统。不必在每次请求时发送全部 42+ 工具（消耗大量 token），标记为 `deferred` 的工具只在通过对话历史中的 `tool_reference` 块被「发现」后才被包含。`ToolSearchTool` 本身始终包含，以便发现更多工具。

### paramsFromContext 闭包

`queryModel` 的核心是 `paramsFromContext` 闭包（约 190 行），它构建完整的 API 请求。它是闭包而非独立函数，因为它捕获了整个请求构建上下文（消息、系统提示、工具、betas）：

```typescript
// 源码位置: src/services/api/claude.ts:1538-1729
const paramsFromContext = (retryContext: RetryContext) => {
  const betasParams = [...betas]
  // ... 配置 effort、task budget、thinking、context management
  // ... 锁存 fast_mode、afk_mode、cache_editing headers
  return {
    model: normalizeModelStringForAPI(options.model),
    messages: addCacheBreakpoints(messagesForAPI, ...),
    system, tools: allTools, betas: betasParams,
    metadata: getAPIMetadata(),
    max_tokens: maxOutputTokens, thinking,
    // ... speed, context_management, output_config
  }
}
```

为什么要多次调用？它分别用于日志记录（fire-and-forget）、实际 API 请求和非流式降级重试。`RetryContext` 参数允许重试循环在上下文溢出错误缩小可用输出预算时覆盖 `maxTokensOverride`。

### Beta Header 锁存

```typescript
// 源码位置: src/services/api/claude.ts:1642-1689
// Fast mode：header 锁存保持会话稳定（缓存安全），
// 但 speed='fast' 保持动态，使冷却期仍能抑制实际的快速模式请求而不改变缓存键。
if (fastModeHeaderLatched && !betasParams.includes(FAST_MODE_BETA_HEADER)) {
  betasParams.push(FAST_MODE_BETA_HEADER)
}
```

锁存模式看似简单，实则解决了一个关键的成本问题：prompt cache 键包含 beta headers。如果 `fast_mode` 在会话中途开关切换，每次切换都会使缓存失效。系统提示约 20K token，单次缓存未命中的成本远超提示本身。锁存确保 header 一旦激活就保持整个会话——`speed='fast'` 参数仍然动态切换以控制行为，但缓存键保持稳定。

---

## 3. 重试策略引擎

`withRetry.ts`（823 行）为每个 API 调用包装了精密的重试状态机：

```typescript
// 源码位置: src/services/api/withRetry.ts:170-178
export async function* withRetry<T>(
  getClient: () => Promise<Anthropic>,
  operation: (client: Anthropic, attempt: number, context: RetryContext) => Promise<T>,
  options: RetryOptions,
): AsyncGenerator<SystemAPIErrorMessage, T> {
```

`AsyncGenerator` 返回类型是关键设计选择：调用者在重试之间获得中间 `SystemAPIErrorMessage` 事件，UI 将其渲染为「X 秒后重试...」状态消息。

### 重试决策矩阵

| 错误 | 策略 | 原因 |
|------|------|------|
| **429 速率限制** | 等待 `retry-after`，或快速模式冷却 | 遵守服务器指令 |
| **529 过载** | 最多 3 次重试 → 降级到 Sonnet | 防止级联放大 |
| **401 未授权** | 强制刷新 OAuth token → 重试 | Token 可能已过期 |
| **403 Token 已撤销** | `handleOAuth401Error()` → 重试 | 其他进程刷新了 token |
| **400 上下文溢出** | 减小 `max_tokens` → 重试 | 缩小输出以适应上下文 |
| **ECONNRESET/EPIPE** | 禁用 keep-alive → 重试 | 检测到过期的 socket |
| **非前台 529** | 立即放弃 | 减少后端放大 |

### 前台查询源白名单

```typescript
// 源码位置: src/services/api/withRetry.ts:62-82
const FOREGROUND_529_RETRY_SOURCES = new Set<QuerySource>([
  'repl_main_thread', 'sdk', 'agent:custom', 'agent:default',
  'compact', 'hook_agent', 'auto_mode', ...
])
```

这是一项关键的反放大措施。在容量级联故障期间，每次重试将后端负载放大 3-10 倍。后台查询（摘要、标题、分类器）遇到 529 时立即放弃——用户永远看不到它们失败。只有用户正在等待结果的前台查询才会重试。

### 持久重试模式

```typescript
// 源码位置: src/services/api/withRetry.ts:96-98
const PERSISTENT_MAX_BACKOFF_MS = 5 * 60 * 1000    // 最大退避 5 分钟
const PERSISTENT_RESET_CAP_MS = 6 * 60 * 60 * 1000 // 6 小时上限
const HEARTBEAT_INTERVAL_MS = 30_000                // 30 秒心跳
```

对于无人值守的 CI/CD 会话（`CLAUDE_CODE_UNATTENDED_RETRY`），重试循环无限运行。等待期间每 30 秒发出一次心跳 `SystemAPIErrorMessage`，防止宿主环境（Docker、CI 运行器）认为会话空闲并终止它。

---

## 4. 流式处理架构

### 为什么使用原始 SSE 而非 SDK 流

```typescript
// 源码位置: src/services/api/claude.ts:1818-1836
// 使用原始流代替 BetaMessageStream 以避免 O(n²) 的部分 JSON 解析
const result = await anthropic.beta.messages
  .create({ ...params, stream: true }, { signal, ... })
  .withResponse()
stream = result.data  // Stream<BetaRawMessageStreamEvent>
```

Anthropic SDK 的 `BetaMessageStream` 在每个 `input_json_delta` 事件上调用 `partialParse()`。对于长工具输入，这会产生二次增长——每个 delta 都从头重新解析累积的 JSON。Claude Code 绕过了这一点，直接消费原始 SSE 事件并手动累积内容块。

### 流事件状态机

```
message_start
  → 初始化 usage，捕获 research 元数据
  → 记录 TTFB（首字节时间）

content_block_start
  → 创建新的 block（text | thinking | tool_use | server_tool_use | connector_text）
  → 初始化累加器：text=''，thinking=''，input=''

content_block_delta
  → 增量追加：text_delta | input_json_delta | thinking_delta | signature_delta
  → 类型安全的累积（每种 delta 类型匹配其 block 类型）

content_block_stop
  → 构建完成的 content block
  → 解析 tool_use 输入的累积 JSON：JSON.parse(accumulated)

message_delta
  → 更新 usage 计数器，捕获 stop_reason

message_stop
  → 完成 AssistantMessage，yield 给调用者
```

### 空闲看门狗

```typescript
// 源码位置: src/services/api/claude.ts:1874-1928
const STREAM_IDLE_TIMEOUT_MS = parseInt(...) || 90_000  // 默认 90 秒
const STREAM_IDLE_WARNING_MS = STREAM_IDLE_TIMEOUT_MS / 2  // 45 秒警告

streamIdleTimer = setTimeout(() => {
  streamIdleAborted = true
  releaseStreamResources()
}, STREAM_IDLE_TIMEOUT_MS)
```

静默断开的连接是 SSE 流的真实问题。SDK 的请求超时只覆盖初始的 `fetch()`，不覆盖流式 body。看门狗监控 chunk 间隔：45 秒时发出警告，90 秒时中止。没有这个，挂起的流会无限期阻塞会话。

---

## 5. Prompt 缓存 —— 三层策略

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

| 层级 | TTL | 范围 | 资格 |
|------|-----|------|------|
| **临时** | 5 分钟（默认） | 单用户 | 所有人 |
| **1 小时** | 1h | 单用户 | 订阅者 + GrowthBook 白名单匹配 |
| **全局** | 5 分钟 | 跨用户 | MCP 工具稳定时的系统提示 |

### 1h TTL 资格与锁存

```typescript
// 源码位置: src/services/api/claude.ts:393-434
function should1hCacheTTL(querySource?: QuerySource): boolean {
  // 资格在首次评估时锁存到 bootstrap state
  let userEligible = getPromptCache1hEligible()
  if (userEligible === null) {
    userEligible = process.env.USER_TYPE === 'ant' ||
      (isClaudeAISubscriber() && !currentLimits.isUsingOverage)
    setPromptCache1hEligible(userEligible)  // 锁存！
  }
  // 白名单也锁存，防止会话中途混用 TTL
  let allowlist = getPromptCache1hAllowlist()
  if (allowlist === null) {
    const config = getFeatureValue_CACHED_MAY_BE_STALE('tengu_prompt_cache_1h_config', {})
    allowlist = config.allowlist ?? []
    setPromptCache1hAllowlist(allowlist)  // 锁存！
  }
  // 支持尾部 * 通配符的模式匹配
  return allowlist.some(pattern =>
    pattern.endsWith('*')
      ? querySource.startsWith(pattern.slice(0, -1))
      : querySource === pattern
  )
}
```

资格和白名单在首次评估时都锁存到 bootstrap state。这防止了会话中途的超额翻转（订阅者用完配额）改变缓存 TTL——每次翻转会造成 ~20K token 的服务端缓存失效惩罚。

---

## 6. 错误分类系统

`errors.ts`（1,208 行）是 API 目录中最大的文件，为每种 API 失败模式提供结构化的错误分类：

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

### 错误分类层级

```
getAssistantMessageFromError(error, model)
  ├─ APIConnectionTimeoutError → "请求超时"
  ├─ ImageSizeError → "图片太大"
  ├─ 熔断开关消息 → "Opus 负载高，切换到 Sonnet"
  ├─ 429 速率限制
  │   ├─ 有统一 headers → getRateLimitErrorMessage(limits)
  │   └─ 无 headers → 通用 "请求被拒绝 (429)"
  ├─ prompt too long → PROMPT_TOO_LONG_ERROR_MESSAGE + errorDetails
  ├─ PDF 错误 → 太大 / 密码保护 / 无效
  ├─ 图片超限 / 多图限制 → 尺寸错误消息
  ├─ tool_use/tool_result 不匹配 → 并发错误 + /rewind 提示
  ├─ 组织已禁用 → 过期 API 密钥指引
  └─ （默认）→ 格式化的 API 错误字符串
```

错误系统身兼双职：它为终端 UI 提供用户可读消息，同时为响应式压缩的重试逻辑提供机器可读的 `errorDetails` 字符串。`getPromptTooLongTokenGap()` 从 `errorDetails` 解析实际值与限制值的差距，让压缩系统在一次重试中跳过多个消息组，而不是逐个剥离。

---

## 7. GrowthBook 特性开关

`growthbook.ts`（1,156 行）集成了 GrowthBook 特性开关平台，作为 Claude Code 的远程配置骨干。

### 两种读取模式

```typescript
// 非阻塞：立即返回缓存/过期值
export function getFeatureValue_CACHED_MAY_BE_STALE<T>(feature, defaultValue): T {
  // 优先级：环境变量覆盖 → 配置覆盖 → 内存载荷 → 磁盘缓存 → 默认值
}

// 阻塞：等待 GrowthBook 初始化完成
export async function getDynamicConfig_BLOCKS_ON_INIT<T>(feature, defaultValue): Promise<T> {
  const growthBookClient = await initializeGrowthBook()  // 阻塞 ~10ms
}
```

`CACHED_MAY_BE_STALE` 是热路径首选——在渲染循环、权限检查和模型选择中每次会话调用数百次。它首先从内存 Map 读取，然后回退到磁盘缓存（`~/.claude.json`）。`BLOCKS_ON_INIT` 保留给关键路径决策，如熔断开关，在这些场景中错误答案会造成严重后果。

### 用户属性与定向

```typescript
// 源码位置: src/services/analytics/growthbook.ts:32-47
export type GrowthBookUserAttributes = {
  id: string                    // 设备 ID（跨会话稳定）
  sessionId: string             // 单次会话唯一 ID
  platform: 'win32' | 'darwin' | 'linux'
  organizationUUID?: string     // 企业组织定向
  userType?: string             // 'ant'（内部）| 'external'
  subscriptionType?: string     // Pro, Max, Enterprise 等
  rateLimitTier?: string        // 速率限制等级（渐进发布）
  appVersion?: string           // 按版本门控特性
}
```

### Remote Eval 变通方案

GrowthBook 的远程评估模式在服务端评估 flag（让定向规则保持私密），但 SDK 返回 `{ value: ... }` 而期望 `{ defaultValue: ... }`。Claude Code 通过 `processRemoteEvalPayload()` 变通处理，将值缓存到内存 Map 和磁盘中，确保跨进程稳定性。

---

## 8. LSP 集成 —— 闭包工厂模式

LSP（语言服务器协议）集成展示了 Claude Code 对闭包工厂优于类的偏好：

```typescript
// 源码位置: src/services/lsp/LSPServerManager.ts:59-65
export function createLSPServerManager(): LSPServerManager {
  const servers: Map<string, LSPServerInstance> = new Map()
  const extensionMap: Map<string, string[]> = new Map()
  const openedFiles: Map<string, string> = new Map()
  // ... 360 行闭包方法
  return { initialize, shutdown, getServerForFile, ensureServerStarted, sendRequest, ... }
}
```

### 基于文件扩展名的路由

当 Claude Code 读取或编辑文件时，LSP 管理器根据文件扩展名将通知路由到正确的语言服务器。

### 代际计数器

```typescript
// 源码位置: src/services/lsp/manager.ts:32-36
let initializationGeneration = 0

const currentGeneration = ++initializationGeneration
lspManagerInstance.initialize().then(() => {
  if (currentGeneration === initializationGeneration) {
    initializationState = 'success'
  }
})
```

这解决了一个微妙的竞态条件：如果在前一次初始化仍在进行时调用 `reinitializeLspServerManager()`，代际计数器确保过期初始化的 `.then()` 处理器被静默丢弃。

---

## 9. MCP 集成要点

MCP（模型上下文协议）子系统在[第 4 集：插件系统](04-plugin-system.md)中详细介绍，但其服务层方面值得在此提及。

### 六种传输类型

```typescript
// 源码位置: src/services/mcp/types.ts
export const TransportSchema = z.enum([
  'stdio',    // 本地子进程（stdin/stdout）—— 最常见
  'sse',      // Server-Sent Events（远程）
  'http',     // Streamable HTTP（MCP 2025 规范）
  'ws',       // WebSocket（IDE 扩展）
  'sdk',      // 进程内 SDK 控制传输
  'sse-ide',  // 通过 IDE 桥接的 SSE
])
```

MCP 连接生命周期通过 React Context 管理，支持在不重启会话的情况下热重载 MCP 服务器。React Compiler 使用 `_c(6)` memo 插槽优化此组件。

---

## 可迁移设计模式

> 以下模式可直接应用于其他 Agentic 系统或 CLI 工具。

### 模式 1: 会话稳定锁存

**场景:** 特性开关或 beta header 控制的行为同时也是缓存键的一部分。
**问题:** 会话中途切换标志会使缓存失效，造成严重的成本放大。
**实践:** 在首次评估时锁存标志值。锁存在 bootstrap state 中存储布尔值；一旦设为 `true`，在会话内不再恢复。

**Claude Code 将此应用于:** `fast_mode`、`afk_mode`、`cache_editing` beta headers，1h prompt cache 资格，以及 GrowthBook 白名单。

### 模式 2: AsyncGenerator 重试管道

**场景:** 长时间运行的操作需要重试逻辑，但调用者也需要重试状态的可见性。
**问题:** 传统重试包装器阻塞直到成功或最终失败。调用者无法显示「重试中...」UI。
**实践:** 将重试包装器设为 `AsyncGenerator`，在尝试之间 `yield` 状态事件，最终 `return` 结果。

### 模式 3: 带代际计数器的闭包工厂

**场景:** 可能需要重新初始化的单例服务（如插件刷新后）。
**问题:** 异步初始化可能重叠：`init_1` 启动，`reinit` 启动 `init_2`，但 `init_1` 后完成并覆盖 `init_2` 的状态。
**实践:** 每次初始化时递增代际计数器。`.then()` 回调在更新状态前检查其代际是否仍为当前值。

### 模式 4: 前台/后台重试分类

**场景:** 服务遭遇级联故障（如 API 过载）。
**问题:** 重试所有查询会按每层重试放大过载 3-10 倍。
**实践:** 维护一个「前台」查询源白名单（用户在等待）。后台查询（摘要、分类器、标题）在过载错误时立即放弃。这是设计层面的反放大。

---

## 源码坐标

| 组件 | 路径 | 行数 | 核心函数 |
|------|------|------|----------|
| 客户端工厂 | `services/api/client.ts` | 390 | `getAnthropicClient()` |
| 查询引擎 | `services/api/claude.ts` | 3,420 | `queryModel()` L1017 |
| 重试引擎 | `services/api/withRetry.ts` | 823 | `withRetry()` L170 |
| 错误系统 | `services/api/errors.ts` | 1,208 | `getAssistantMessageFromError()` L425 |
| 缓存控制 | `services/api/claude.ts` | — | `getCacheControl()` L358 |
| GrowthBook | `services/analytics/growthbook.ts` | 1,156 | `getFeatureValue_CACHED_MAY_BE_STALE()` L734 |
| LSP 管理器 | `services/lsp/LSPServerManager.ts` | 421 | `createLSPServerManager()` L59 |
| LSP 单例 | `services/lsp/manager.ts` | 290 | `initializeLspServerManager()` L145 |
| MCP 客户端 | `services/mcp/client.ts` | ~3,000 | MCP 生命周期管理 |
| 分析 | `services/analytics/index.ts` | — | `logEvent()`，PII 保护 |

---

*服务层是 Claude Code 的雄心与现实世界故障模式交汇的地方。下一集：[第 16 集 —— 基础设施与配置系统](16-infrastructure-config.md)*

[← 第 14 集 —— UI 与状态管理](14-ui-state-management.md)
