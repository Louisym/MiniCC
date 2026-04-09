"""
retry.py — 错误分类 + 指数退避 + 溢出保护 + 重试引擎

忠实还原 Claude Code 的 API 重试机制。
源码对照:
  - rust/crates/api/src/error.rs   — ApiError + is_retryable()
  - rust/crates/api/src/client.rs  — send_with_retry() + backoff_for_attempt()

三大核心工程要点:
1. 错误分类: is_retryable 属性区分可重试/不可重试 (error.rs:34-48)
2. 指数退避 + 溢出保护: initial * 2^(attempt-1)，cap + overflow check (client.rs:325-336)
3. RetriesExhausted 包装: 不是简单 re-raise，而是带 attempts 的结构化错误 (error.rs:21-24)
"""

import time
from typing import Callable, TypeVar

T = TypeVar("T")

# CC 的默认常量 — 源码: client.rs:18-20
DEFAULT_INITIAL_BACKOFF_MS = 200
DEFAULT_MAX_BACKOFF_MS = 2000
DEFAULT_MAX_RETRIES = 2


# ============================================================
# 错误类型层级
# 源码: error.rs:6-30
#
# CC 用 Rust enum 的变体来区分不同错误。
# Python 用异常继承层级 + is_retryable 属性。
#
# 关键设计: is_retryable 是属性而不是方法——
# 每种错误在创建时就确定了能否重试，不需要运行时判断。
# ============================================================

class ApiError(Exception):
    """API 错误基类。源码: error.rs:6"""

    is_retryable: bool = False


class HttpApiError(ApiError):
    """HTTP 状态码错误。源码: error.rs:14-20

    CC 用 retryable 字段标记：服务端根据状态码判断是否可重试。
    """

    # 可重试的 HTTP 状态码
    RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}

    def __init__(self, status_code: int, message: str = ""):
        self.status_code = status_code
        self.is_retryable = status_code in self.RETRYABLE_STATUS_CODES
        super().__init__(f"HTTP {status_code}: {message}" if message else f"HTTP {status_code}")


class ConnectionApiError(ApiError):
    """网络连接/超时错误。源码: error.rs:36

    CC: error.is_connect() || error.is_timeout() || error.is_request() → retryable
    连接错误总是可重试的——网络抖动等一等可能就好了。
    """

    is_retryable = True

    def __init__(self, message: str = "connection error"):
        super().__init__(message)


class AuthApiError(ApiError):
    """认证错误 (API Key 无效/缺失)。源码: error.rs:39-40

    永远不可重试——重试一百次 Key 也不会变有效。
    """

    is_retryable = False

    def __init__(self, message: str = "authentication failed"):
        super().__init__(message)


class RetriesExhausted(ApiError):
    """重试耗尽。源码: error.rs:21-24

    不是简单地 re-raise 最后一个错误，而是包装成新错误:
    - attempts: 总共尝试了多少次
    - last_error: 最后一次失败的具体错误

    为什么要包装？因为调用方需要知道"是重试耗尽了"这个事实，
    而不只是看到最后一个 503 错误——后者丢失了"我已经试了 3 次"的信息。
    """

    def __init__(self, attempts: int, last_error: ApiError):
        self.attempts = attempts
        self.last_error = last_error
        self.is_retryable = last_error.is_retryable
        super().__init__(f"failed after {attempts} attempts: {last_error}")


# ============================================================
# backoff_for_attempt — 指数退避 + 溢出保护
# 源码: client.rs:325-336
#
# 公式: wait = min(initial * 2^(attempt-1), max)
#
# CC 的溢出保护 (client.rs:326-328):
#   checked_shl(attempt-1) → 如果溢出，返回 BackoffOverflow 错误
#
# Python int 不会溢出，但我们仍然实现上限保护:
#   attempt 过大时 2^(attempt-1) 会是天文数字，
#   直接 cap 在 max_backoff 而不是算出来再 min()。
# ============================================================

_MAX_SAFE_EXPONENT = 31  # 2^31 = 2147483648，超过任何合理 backoff


def backoff_for_attempt(
    attempt: int,
    initial_ms: int = DEFAULT_INITIAL_BACKOFF_MS,
    max_ms: int = DEFAULT_MAX_BACKOFF_MS,
) -> float:
    """计算第 attempt 次重试的等待时间（秒）。

    attempt 从 1 开始（第一次重试）。

    源码: client.rs:325-336
    CC 用 checked_shl 检测溢出。我们用 exponent 上限保护。
    """
    exponent = attempt - 1

    # 溢出保护: exponent 过大时直接返回 max
    if exponent > _MAX_SAFE_EXPONENT:
        return max_ms / 1000.0

    multiplier = 1 << exponent  # 2^exponent
    delay_ms = initial_ms * multiplier

    return min(delay_ms, max_ms) / 1000.0


# ============================================================
# send_with_retry — 核心重试循环
# 源码: client.rs:273-307
#
# 三路分流:
#   成功 → 返回结果
#   可重试 + 有次数 → sleep + 继续
#   不可重试 或 次数用完 → 抛异常
#
# CC 接受一个 request 对象。我们接受一个 callable — 更通用:
# 既可以包装 API 调用，也可以包装任何可能失败的操作。
# ============================================================

def send_with_retry(
    fn: Callable[[], T],
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_backoff_ms: int = DEFAULT_INITIAL_BACKOFF_MS,
    max_backoff_ms: int = DEFAULT_MAX_BACKOFF_MS,
) -> T:
    """带重试的调用。

    fn: 无参 callable，成功返回结果，失败抛 ApiError。
    max_retries: 最多重试次数（不含首次调用）。
        CC 默认 2 → 首次 + 2次重试 = 共 3 次尝试。

    源码: client.rs:273-307
    """
    attempts = 0
    last_error: ApiError | None = None

    # CC 的循环逻辑 (client.rs:280-301):
    #   loop {
    #     attempts += 1
    #     match send() {
    #       Ok → return Ok
    #       Err(retryable) if attempts <= max_retries + 1 → last_error = err
    #       Err → return Err
    #     }
    #     if attempts > max_retries { break }
    #     sleep(backoff)
    #   }
    #
    # 关键: 判断 "是否记录错误继续" 用 attempts <= max_retries + 1
    # 判断 "是否 sleep 继续下轮" 用 attempts > max_retries
    # 当 attempts == max_retries + 1 时: 记录错误，但不 sleep → break → exhausted

    while True:
        attempts += 1

        try:
            return fn()
        except ApiError as error:
            if error.is_retryable and attempts <= max_retries + 1:
                last_error = error
            else:
                raise

        if attempts > max_retries:
            break

        wait = backoff_for_attempt(attempts, initial_backoff_ms, max_backoff_ms)
        time.sleep(wait)

    # 所有重试都失败了 → 包装成 RetriesExhausted
    raise RetriesExhausted(
        attempts=attempts,
        last_error=last_error,  # type: ignore[arg-type]
    )
