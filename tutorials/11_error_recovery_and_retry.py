"""
Tutorial 11: 错误恢复与重试引擎 — Agent 活下来的关键
=====================================================

为什么这一课最重要？

    一个没有错误恢复的 Agent，在真实环境中根本无法使用。
    API 会限流（429）、会过载（503）、会超时、会断连...
    如果每次出错就直接崩溃，用户体验为零。

    Claude Code 的应对方式：
    - 可重试错误 → 自动指数退避重试
    - 重试耗尽 → 结构化错误上报
    - 流中断 → 超时看门狗自动中止
    - 输出被截断 → 三级升级恢复

生活类比：
    你去银行办业务。
    - 窗口说"系统繁忙请稍后"(429) → 你等一会儿再排队
    - 窗口说"系统崩了"(503) → 你等更久，或者去另一个窗口
    - 窗口说"你的证件过期了"(401) → 你不能重试，要先去换证件
    - 排了3次都失败 → 你放弃，去找经理投诉（报错给用户）

对应源码：
    - rust/crates/api/src/error.rs    → ApiError 错误类型 + is_retryable()
    - rust/crates/api/src/client.rs   → send_with_retry() + backoff_for_attempt()
    - rust/crates/runtime/src/conversation.rs → RuntimeError, build_assistant_message()
    - rust/crates/api/tests/client_integration.rs → 重试集成测试

运行方式：python tutorials/11_error_recovery_and_retry.py
"""

import time
import random
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Optional


# ============================================================
# 第一步：错误分类 — 哪些错误可以重试？
# ============================================================
# 不是所有错误都应该重试！
#
# 可重试的（暂时的问题，等一等可能好）：
#   - 429 Too Many Requests → 限流了，等一下
#   - 503 Service Unavailable → 服务器过载
#   - 502 Bad Gateway → 网关故障
#   - 408 Request Timeout → 请求超时
#   - 409 Conflict → 资源冲突
#   - 500 Internal Server Error → 服务器内部错误
#   - 504 Gateway Timeout → 网关超时
#   - 网络连接错误 → 可能是网络抖动
#
# 不可重试的（持久性问题，重试没用）：
#   - 401 Unauthorized → API Key 无效
#   - 403 Forbidden → 没权限
#   - 404 Not Found → 资源不存在
#   - 400 Bad Request → 请求格式错误
#   - API Key 缺失 → 配置问题
#   - JSON 解析失败 → 数据格式问题

class ApiError(Exception):
    """
    API 错误基类。

    对应源码: api/error.rs:6-30 (ApiError enum)
    """
    pass


class HttpApiError(ApiError):
    """
    HTTP 状态码错误（来自 API 服务器的响应）。

    对应源码: api/error.rs:14-20
        Api {
            status: reqwest::StatusCode,
            error_type: Option<String>,
            message: Option<String>,
            body: String,
            retryable: bool,
        }
    """
    def __init__(self, status_code: int, error_type: str = "",
                 message: str = "", retryable: bool = False):
        self.status_code = status_code
        self.error_type = error_type
        self.message = message
        self.retryable = retryable
        super().__init__(
            f"API returned {status_code} ({error_type}): {message}"
        )

    def is_retryable(self) -> bool:
        """
        判断这个错误是否可以重试。

        对应源码: api/error.rs:34-48
            pub fn is_retryable(&self) -> bool {
                match self {
                    Self::Http(error) => error.is_connect() || error.is_timeout(),
                    Self::Api { retryable, .. } => *retryable,
                    Self::RetriesExhausted { last_error, .. } => last_error.is_retryable(),
                    // 其他所有类型 → false
                    _ => false,
                }
            }
        """
        return self.retryable


class NetworkError(ApiError):
    """网络连接错误（连接超时、断连等）"""
    def __init__(self, message: str = "connection failed"):
        super().__init__(message)

    def is_retryable(self) -> bool:
        # 网络错误通常是暂时的，可以重试
        return True


class AuthError(ApiError):
    """认证错误（API Key 无效或缺失）"""
    def __init__(self, message: str = "missing API key"):
        super().__init__(message)

    def is_retryable(self) -> bool:
        # 认证错误重试没用，必须修复配置
        return False


class RetriesExhaustedError(ApiError):
    """
    重试次数耗尽。

    对应源码: api/error.rs:21-24
        RetriesExhausted {
            attempts: u32,
            last_error: Box<ApiError>,
        }
    """
    def __init__(self, attempts: int, last_error: ApiError):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"API failed after {attempts} attempts: {last_error}"
        )


# 判断 HTTP 状态码是否可重试
# 对应源码: api/client.rs:588-590
#   const fn is_retryable_status(status) -> bool {
#       matches!(status.as_u16(), 408 | 409 | 429 | 500 | 502 | 503 | 504)
#   }
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


def is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES


# ============================================================
# 第二步：指数退避 — 等多久再重试？
# ============================================================
# 不能立刻重试！如果所有人都立刻重试，服务器会更加过载。
#
# 指数退避策略：每次等待时间翻倍，但有上限。
#
#   第 1 次重试：等 200ms
#   第 2 次重试：等 400ms
#   第 3 次重试：等 800ms（但如果上限是 2s，就等 800ms）
#   第 4 次重试：等 1600ms（但上限 2s，所以等 2s）
#
# 公式：wait = min(initial_backoff * 2^(attempt-1), max_backoff)

# 默认配置（和源码一致）
# 对应源码: api/client.rs:18-20
#   const DEFAULT_INITIAL_BACKOFF: Duration = Duration::from_millis(200);
#   const DEFAULT_MAX_BACKOFF: Duration = Duration::from_secs(2);
#   const DEFAULT_MAX_RETRIES: u32 = 2;
DEFAULT_INITIAL_BACKOFF_MS = 200
DEFAULT_MAX_BACKOFF_MS = 2000
DEFAULT_MAX_RETRIES = 2


def backoff_for_attempt(attempt: int,
                        initial_backoff_ms: int = DEFAULT_INITIAL_BACKOFF_MS,
                        max_backoff_ms: int = DEFAULT_MAX_BACKOFF_MS) -> int:
    """
    计算第 N 次重试应该等多久（毫秒）。

    对应源码: api/client.rs:325-336
        fn backoff_for_attempt(&self, attempt: u32) -> Result<Duration, ApiError> {
            let multiplier = 1_u32.checked_shl(attempt.saturating_sub(1));
            Ok(self.initial_backoff
                .checked_mul(multiplier)
                .map_or(self.max_backoff, |delay| delay.min(self.max_backoff)))
        }
    """
    # 2^(attempt-1) 就是 1, 2, 4, 8, 16...
    multiplier = 1 << (attempt - 1)
    delay = initial_backoff_ms * multiplier
    # 不能超过最大退避时间
    return min(delay, max_backoff_ms)


def min(a, b):
    return a if a < b else b


# ============================================================
# 第三步：重试引擎 — send_with_retry
# ============================================================
# 这是 API 客户端的核心。每次 API 调用都通过它包装。
#
# 流程：
#   attempt = 0
#   loop:
#     attempt += 1
#     try:
#       response = send_request()
#       if response.status == 200:
#         return response
#       elif is_retryable(response.status):
#         if attempt > max_retries:
#           break
#         sleep(backoff)
#       else:
#         raise error  ← 不可重试，直接报错
#     except network_error:
#       if attempt > max_retries:
#         break
#       sleep(backoff)
#   raise RetriesExhaustedError

@dataclass
class RetryConfig:
    """
    重试策略配置。

    对应源码: api/client.rs:104-111 (AnthropicClient 字段)
    """
    max_retries: int = DEFAULT_MAX_RETRIES
    initial_backoff_ms: int = DEFAULT_INITIAL_BACKOFF_MS
    max_backoff_ms: int = DEFAULT_MAX_BACKOFF_MS


@dataclass
class ApiResponse:
    """模拟的 API 响应"""
    status_code: int
    body: str = ""
    error_type: str = ""
    error_message: str = ""


def send_with_retry(send_fn, config: RetryConfig = RetryConfig()) -> str:
    """
    带重试的请求发送函数。

    这是整个重试引擎的核心实现。

    对应源码: api/client.rs:273-307
        async fn send_with_retry(&self, request) -> Result<Response, ApiError> {
            let mut attempts = 0;
            let mut last_error: Option<ApiError>;
            loop {
                attempts += 1;
                match self.send_raw_request(request).await {
                    Ok(response) => match expect_success(response).await {
                        Ok(response) => return Ok(response),
                        Err(error) if error.is_retryable() && attempts <= self.max_retries + 1 => {
                            last_error = Some(error);
                        }
                        Err(error) => return Err(error),
                    },
                    Err(error) if error.is_retryable() && attempts <= self.max_retries + 1 => {
                        last_error = Some(error);
                    }
                    Err(error) => return Err(error),
                }
                if attempts > self.max_retries { break; }
                tokio::time::sleep(self.backoff_for_attempt(attempts)?).await;
            }
            Err(ApiError::RetriesExhausted { attempts, last_error })
        }
    """
    attempts = 0
    last_error = None

    while True:
        attempts += 1

        try:
            # 调用实际的发送函数
            response = send_fn()

            # 检查响应状态
            if response.status_code == 200:
                return response.body  # 成功！

            # 构造错误
            retryable = is_retryable_status(response.status_code)
            error = HttpApiError(
                status_code=response.status_code,
                error_type=response.error_type,
                message=response.error_message,
                retryable=retryable,
            )

            if error.is_retryable() and attempts <= config.max_retries:
                # 可重试 且 还没超过重试次数
                last_error = error
                wait_ms = backoff_for_attempt(
                    attempts, config.initial_backoff_ms, config.max_backoff_ms
                )
                print(f"    [重试引擎] 第 {attempts} 次失败 "
                      f"({response.status_code}) → "
                      f"等待 {wait_ms}ms 后重试...")
                time.sleep(wait_ms / 1000)
                continue
            else:
                # 不可重试 或 重试次数耗尽
                raise error

        except ApiError as e:
            if not hasattr(e, 'is_retryable') or not e.is_retryable():
                raise  # 不可重试的错误直接抛出

            if attempts > config.max_retries:
                break  # 重试耗尽

            last_error = e
            wait_ms = backoff_for_attempt(
                attempts, config.initial_backoff_ms, config.max_backoff_ms
            )
            print(f"    [重试引擎] 第 {attempts} 次失败 "
                  f"({e}) → 等待 {wait_ms}ms 后重试...")
            time.sleep(wait_ms / 1000)

    # 所有重试都失败了
    raise RetriesExhaustedError(attempts, last_error)


# ============================================================
# 第四步：Agentic Loop 中的错误恢复
# ============================================================
# 重试引擎保护 API 调用层。但 Agent 循环本身也需要错误恢复。
#
# 关键场景：
# 1. API 调用失败但可恢复 → 重试引擎处理
# 2. 流中断（收到一半数据就断了）→ 需要检测并处理
# 3. 工具执行失败 → 把错误告诉 AI，让它换个方式
# 4. 超过最大迭代次数 → 防止无限循环

@dataclass
class AssistantMessage:
    """AI 的回复"""
    text_blocks: list[str]
    tool_calls: list[dict]
    finished: bool = True  # 流是否正常结束


def build_assistant_message(events: list[dict]) -> AssistantMessage:
    """
    从流事件构建 AI 回复。如果流中断（没收到 message_stop），标记为未完成。

    对应源码: conversation.rs:353-390
        fn build_assistant_message(events) {
            ...
            if !finished {
                return Err("assistant stream ended without a message stop event");
            }
            if blocks.is_empty() {
                return Err("assistant stream produced no content");
            }
        }
    """
    text = ""
    text_blocks = []
    tool_calls = []
    finished = False

    for event in events:
        if event["type"] == "text_delta":
            text += event["text"]
        elif event["type"] == "tool_use":
            if text:
                text_blocks.append(text)
                text = ""
            tool_calls.append({
                "id": event["id"],
                "name": event["name"],
                "input": event["input"],
            })
        elif event["type"] == "message_stop":
            finished = True

    if text:
        text_blocks.append(text)

    return AssistantMessage(
        text_blocks=text_blocks,
        tool_calls=tool_calls,
        finished=finished,
    )


def run_turn_with_recovery(api_fn, tool_executor, max_iterations=10):
    """
    带错误恢复的 Agent 循环。

    这个函数展示了一个完整的、能在真实环境中存活的 Agent 循环。

    对应源码: conversation.rs:170-283 (run_turn)
    加上 reference 中描述的恢复策略
    """
    iterations = 0

    while True:
        iterations += 1
        if iterations > max_iterations:
            return {
                "status": "error",
                "reason": "exceeded max iterations",
                "iterations": iterations,
            }

        # 第 1 步：调用 API（通过重试引擎保护）
        try:
            events = api_fn()
        except RetriesExhaustedError as e:
            return {
                "status": "error",
                "reason": f"API retries exhausted: {e.last_error}",
                "iterations": iterations,
            }
        except AuthError as e:
            return {
                "status": "error",
                "reason": f"auth failure: {e}",
                "iterations": iterations,
            }

        # 第 2 步：构建回复（检测流中断）
        message = build_assistant_message(events)
        if not message.finished:
            print(f"    [恢复] 流中断！注入继续消息...")
            # 流中断恢复：告诉 AI "从断点继续"
            # 这对应 reference EP01 中的 max_output_tokens 恢复策略
            continue

        if not message.text_blocks and not message.tool_calls:
            return {
                "status": "error",
                "reason": "empty response from assistant",
                "iterations": iterations,
            }

        # 第 3 步：如果没有工具调用，对话结束
        if not message.tool_calls:
            return {
                "status": "success",
                "response": " ".join(message.text_blocks),
                "iterations": iterations,
            }

        # 第 4 步：执行工具调用
        for tool_call in message.tool_calls:
            try:
                result = tool_executor(tool_call["name"], tool_call["input"])
                print(f"    [工具] {tool_call['name']}() → 成功")
            except Exception as e:
                # 工具执行失败 → 不是致命错误！
                # 把错误告诉 AI，让它换个方式
                print(f"    [工具] {tool_call['name']}() → 失败: {e}")
                print(f"    [恢复] 将错误反馈给 AI，让它重新规划...")

        # 继续循环（AI 会看到工具结果，决定下一步）


# ============================================================
# 第五步：演示
# ============================================================

def demo_error_classification():
    """演示错误分类"""
    print("\n--- 演示 1: 错误分类 ---")

    errors = [
        HttpApiError(429, "rate_limit_error", "slow down", retryable=True),
        HttpApiError(503, "overloaded_error", "busy", retryable=True),
        HttpApiError(500, "internal_error", "oops", retryable=True),
        HttpApiError(401, "authentication_error", "invalid key", retryable=False),
        HttpApiError(400, "invalid_request", "bad format", retryable=False),
        NetworkError("connection reset"),
        AuthError("ANTHROPIC_API_KEY not set"),
    ]

    for error in errors:
        retryable = error.is_retryable()
        symbol = "O (可重试)" if retryable else "X (不可重试)"
        print(f"  [{symbol}] {error}")


def demo_exponential_backoff():
    """演示指数退避"""
    print("\n--- 演示 2: 指数退避计算 ---")

    print(f"\n  配置: initial={DEFAULT_INITIAL_BACKOFF_MS}ms, "
          f"max={DEFAULT_MAX_BACKOFF_MS}ms")
    for attempt in range(1, 6):
        wait = backoff_for_attempt(attempt)
        bar = "#" * (wait // 100)
        print(f"  第 {attempt} 次: 等待 {wait:>5}ms  {bar}")

    print(f"\n  自定义配置: initial=10ms, max=25ms")
    # 对应源码中的测试: api/client.rs:910-928
    #   let client = AnthropicClient::new("test-key")
    #     .with_retry_policy(3, Duration::from_millis(10), Duration::from_millis(25));
    #   assert_eq!(backoff(1), 10ms);
    #   assert_eq!(backoff(2), 20ms);
    #   assert_eq!(backoff(3), 25ms);  ← 被 max 限制
    for attempt in range(1, 4):
        wait = backoff_for_attempt(attempt, 10, 25)
        print(f"  第 {attempt} 次: 等待 {wait}ms")


def demo_retry_success():
    """演示：第一次失败，第二次成功"""
    print("\n--- 演示 3: 重试成功 ---")

    call_count = {"n": 0}

    def fake_api():
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 第一次：429 限流
            return ApiResponse(429, error_type="rate_limit_error",
                               error_message="slow down")
        else:
            # 第二次：成功
            return ApiResponse(200, body='{"text": "Hello!"}')

    # 使用很短的退避时间，演示不用等太久
    config = RetryConfig(max_retries=2, initial_backoff_ms=50, max_backoff_ms=100)
    result = send_with_retry(fake_api, config)
    print(f"  最终结果: {result}")
    print(f"  总共调用了 {call_count['n']} 次 API")


def demo_retry_exhausted():
    """演示：重试耗尽"""
    print("\n--- 演示 4: 重试耗尽 ---")

    def always_fail():
        return ApiResponse(503, error_type="overloaded_error",
                           error_message="system overloaded")

    config = RetryConfig(max_retries=2, initial_backoff_ms=50, max_backoff_ms=100)
    try:
        send_with_retry(always_fail, config)
    except RetriesExhaustedError as e:
        print(f"  重试耗尽！共尝试 {e.attempts} 次")
        print(f"  最后的错误: {e.last_error}")


def demo_non_retryable():
    """演示：不可重试的错误直接失败"""
    print("\n--- 演示 5: 不可重试错误 ---")

    def auth_fail():
        return ApiResponse(401, error_type="authentication_error",
                           error_message="invalid api key")

    config = RetryConfig(max_retries=5)
    try:
        send_with_retry(auth_fail, config)
    except HttpApiError as e:
        print(f"  直接失败（不重试）: {e}")
        print(f"  is_retryable = {e.is_retryable()}")


def demo_agentic_loop_recovery():
    """演示：Agent 循环中的错误恢复"""
    print("\n--- 演示 6: Agent 循环错误恢复 ---")

    call_count = {"n": 0}

    def fake_api_with_events():
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 第一轮：AI 想调用工具
            return [
                {"type": "text_delta", "text": "Let me check..."},
                {"type": "tool_use", "id": "t1", "name": "bash",
                 "input": '{"command": "ls"}'},
                {"type": "message_stop"},
            ]
        elif call_count["n"] == 2:
            # 第二轮：流中断！（没有 message_stop）
            return [
                {"type": "text_delta", "text": "The files are"},
            ]
        elif call_count["n"] == 3:
            # 第三轮：恢复成功
            return [
                {"type": "text_delta",
                 "text": "The directory contains 3 files."},
                {"type": "message_stop"},
            ]

    def fake_tool_executor(name, inp):
        if name == "bash":
            return "file1.py\nfile2.py\nfile3.py"
        raise Exception(f"unknown tool: {name}")

    result = run_turn_with_recovery(fake_api_with_events, fake_tool_executor)
    print(f"  最终结果: {result}")


def main():
    print("=" * 60)
    print("Tutorial 11: 错误恢复与重试引擎")
    print("=" * 60)

    demo_error_classification()
    demo_exponential_backoff()
    demo_retry_success()
    demo_retry_exhausted()
    demo_non_retryable()
    demo_agentic_loop_recovery()

    # 全景图
    print("\n" + "=" * 60)
    print("错误恢复全景图")
    print("=" * 60)
    print("""
    错误发生
        │
        ▼
    ┌────────────────────────┐
    │  is_retryable() 分类    │ ← 第一道关卡
    │  429/503/502/500/...   │    可重试 vs 不可重试
    │  → true                │
    │  401/400/AuthError     │
    │  → false               │
    └────────────────────────┘
        │                │
      可重试          不可重试
        │                │
        ▼                ▼
    ┌──────────────┐  ┌───────────┐
    │ 指数退避等待  │  │ 直接报错   │
    │ 200→400→800ms│  │ 给用户     │
    │ (有上限 2s)  │  └───────────┘
    └──────────────┘
        │
        ▼
    ┌──────────────┐
    │ 重新发送请求  │
    │              │
    │ 成功？→ 返回 │
    │ 失败？       │
    │  ├ 还能重试  │→ 回到退避等待
    │  └ 次数耗尽  │→ RetriesExhaustedError
    └──────────────┘

    在 Agent 循环层面，还有更多恢复策略：

    ┌────────────────────────────────────────┐
    │  run_turn() Agent 循环                  │
    │                                        │
    │  API 调用失败 → 重试引擎自动处理        │
    │  流中断 → 检测 message_stop 缺失        │
    │         → 注入"从断点继续"消息          │
    │  工具执行失败 → 把错误反馈给 AI          │
    │              → AI 自动换一种方式         │
    │  超过迭代上限 → 强制终止防无限循环       │
    └────────────────────────────────────────┘

    对应 Claude Code 源码：
    - api/error.rs       → 错误类型定义 + is_retryable()
    - api/client.rs:273  → send_with_retry() 重试循环
    - api/client.rs:325  → backoff_for_attempt() 指数退避
    - api/client.rs:588  → is_retryable_status() 状态码分类
    - conversation.rs:170 → run_turn() Agent 循环
    - conversation.rs:353 → build_assistant_message() 流完整性检查
    """)


if __name__ == "__main__":
    main()
