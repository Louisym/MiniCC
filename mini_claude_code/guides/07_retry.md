# 07 - 重试引擎 (retry.py)

## 问题背景

API 调用会失败：限流 (429)、过载 (503)、网络抖动…… 没有重试机制的 agent 在真实环境中根本不可用。但也不是所有错误都该重试——API Key 无效 (401) 重试一百次也没用。

## CC 的做法

CC 把错误分为两类：**可重试的**和**不可重试的**。可重试的用指数退避重试，全部失败后包装成 `RetriesExhausted` 错误上报。

源码:
- `rust/crates/api/src/error.rs` — 错误分类 + `is_retryable()`
- `rust/crates/api/src/client.rs:273-336` — `send_with_retry()` + `backoff_for_attempt()`

## 你要练习的工程模式

| 模式 | 说明 | 源码位置 |
|------|------|---------|
| **错误分类** | `is_retryable()` 方法判断错误是否可重试 | error.rs:34-48 |
| **指数退避 + 溢出保护** | `initial * 2^(attempt-1)`，cap 在 max，显式检测溢出 | client.rs:325-336 |
| **RetriesExhausted 包装** | 重试耗尽后不是简单 re-raise，而是包装成带 attempts 的新错误 | error.rs:21-24 |
| **重试循环** | loop + match 分支：成功/可重试/不可重试 三路分流 | client.rs:273-307 |

## 核心: 指数退避公式

```
wait = min(initial_backoff * 2^(attempt-1), max_backoff)

attempt=1: min(200ms * 1, 2s) = 200ms
attempt=2: min(200ms * 2, 2s) = 400ms
attempt=3: min(200ms * 4, 2s) = 800ms
attempt=4: min(200ms * 8, 2s) = 1600ms
attempt=5: min(200ms * 16, 2s) = 2000ms  ← 被 cap 了
```

CC 的常量 (client.rs:18-20):
- `DEFAULT_INITIAL_BACKOFF = 200ms`
- `DEFAULT_MAX_BACKOFF = 2s`
- `DEFAULT_MAX_RETRIES = 2` (最多重试 2 次，加上首次 = 共 3 次)

### 溢出保护

CC 用 `checked_shl` 检测 `2^(attempt-1)` 是否溢出 (client.rs:326-331)。
Python 的 int 无上限不会溢出，但我们仍然实现这个检查——因为目的是学习这个模式。

## 你需要写的东西

```python
# --- 错误类型 ---
ApiError(Exception)              — 基类，带 is_retryable 属性
HttpApiError(ApiError)           — HTTP 状态码错误，带 status_code, retryable
ConnectionError_(ApiError)       — 网络连接错误，retryable=True
AuthError(ApiError)              — 认证错误，retryable=False
RetriesExhausted(ApiError)       — 重试耗尽，包装 attempts + last_error

# --- 退避计算 ---
backoff_for_attempt(attempt, initial_ms, max_ms) -> float
  公式: initial * 2^(attempt-1)，cap 在 max
  溢出保护: 如果 2^(attempt-1) 溢出，返回 max

# --- 重试引擎 ---
send_with_retry(fn, max_retries, initial_backoff_ms, max_backoff_ms) -> result
  循环调用 fn()
  成功 → 返回结果
  可重试错误 + 还有次数 → sleep + 继续
  不可重试错误 → 立即抛出
  次数耗尽 → 抛 RetriesExhausted(attempts, last_error)
```

## 错误分类速查

```
可重试 (暂时故障):          不可重试 (持久故障):
  429 Too Many Requests      401 Unauthorized
  500 Internal Error         403 Forbidden
  502 Bad Gateway            400 Bad Request
  503 Service Unavailable    404 Not Found
  504 Gateway Timeout        API Key 缺失
  408 Request Timeout        JSON 解析失败
  网络连接错误
  网络超时
```

## 易错点

- `max_retries=2` 意味着最多**重试** 2 次，首次调用不算重试，所以总共 3 次
- `backoff_for_attempt` 的 attempt 从 1 开始（第一次重试）
- 溢出保护：当 `2^(attempt-1)` 超大时，直接用 max_backoff
- `RetriesExhausted` 要保留 `last_error`，方便调试
- `send_with_retry` 接受一个 callable，不是直接绑定到 API client（更通用）
