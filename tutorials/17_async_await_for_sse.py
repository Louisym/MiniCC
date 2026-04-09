"""
教程 17: Python async/await——流式 API 的基础
================================================================
你写 mini-claude-code 时需要:
  1. 调用 Anthropic API 并逐字接收 SSE 流
  2. 同时处理超时（10 秒没响应就重试）
  3. 同时监听用户输入（Ctrl+C 取消）

这些全靠 async/await。如果你只用过同步 Python，
这个教程从零教你。

目标: 学完后你能写一个异步 SSE 客户端。
================================================================
"""

import asyncio
import time
import json
from typing import AsyncIterator


# ============================================================
# 第一课: 为什么需要 async？
# ============================================================

def lesson_1_why_async():
    """
    问题: 同步代码在等待网络时白白浪费 CPU

    想象你在烧水、切菜、洗碗三件事:
    - 同步: 烧水(等3分钟) → 切菜(2分钟) → 洗碗(1分钟) = 6分钟
    - 异步: 烧水(开始) → 切菜(2分钟) → 水开了 → 洗碗 = 3分钟

    烧水的等待时间里可以做别的事——这就是异步。
    """
    print("=" * 60)
    print("第一课: 为什么需要 async")
    print("=" * 60)

    # 同步版本: 一个一个等
    print("\n  --- 同步版本 ---")
    start = time.time()

    def sync_task(name, seconds):
        time.sleep(seconds)
        return f"{name} 完成"

    # 串行执行
    r1 = sync_task("烧水", 0.3)
    r2 = sync_task("切菜", 0.2)
    r3 = sync_task("洗碗", 0.1)
    sync_time = time.time() - start
    print(f"  {r1}, {r2}, {r3}")
    print(f"  总耗时: {sync_time:.2f}s (串行: 0.3 + 0.2 + 0.1 = 0.6)")

    # 异步版本: 并发执行
    print("\n  --- 异步版本 ---")

    async def async_task(name, seconds):
        await asyncio.sleep(seconds)  # 不阻塞! 让出控制权
        return f"{name} 完成"

    async def cook_async():
        # gather = 同时开始多个任务，等它们都完成
        results = await asyncio.gather(
            async_task("烧水", 0.3),
            async_task("切菜", 0.2),
            async_task("洗碗", 0.1),
        )
        return results

    start = time.time()
    results = asyncio.run(cook_async())
    async_time = time.time() - start
    print(f"  {', '.join(results)}")
    print(f"  总耗时: {async_time:.2f}s (并发: max(0.3, 0.2, 0.1) ≈ 0.3)")
    print(f"\n  异步快了 {sync_time / async_time:.1f}x")


# ============================================================
# 第二课: async/await 的核心概念
# ============================================================

def lesson_2_core_concepts():
    """
    async/await 只有四个核心概念:

    1. async def  — 定义一个"协程函数"
    2. await      — 等待一个异步操作完成
    3. asyncio.run() — 启动事件循环
    4. asyncio.gather() — 并发执行多个协程
    """
    print("\n" + "=" * 60)
    print("第二课: async/await 的四个核心概念")
    print("=" * 60)

    # 概念 1: async def
    print("""
    概念 1: async def — 协程函数
    ─────────────────────────────
    普通函数:   def f():  return 1      → 直接返回值
    协程函数:   async def f(): return 1  → 返回一个"协程对象"

    协程对象就像一个"暂停的任务"——创建后不会立即执行，
    需要 await 或 asyncio.run() 来真正运行它。
    """)

    async def greet(name):
        return f"Hello, {name}!"

    # 直接调用不会执行!
    coro = greet("World")
    print(f"  直接调用: {coro}")
    print(f"  类型: {type(coro)}")
    # 必须 await 或 run
    result = asyncio.run(greet("World"))
    print(f"  asyncio.run(): {result}")

    # 概念 2: await
    print("""
    概念 2: await — "等待但不阻塞"
    ─────────────────────────────
    time.sleep(1)        → 阻塞整个线程，什么都不能做
    await asyncio.sleep(1) → 让出控制权，别的任务可以运行

    await 只能在 async def 里使用。
    """)

    async def demo_await():
        print("  开始...")
        await asyncio.sleep(0.1)  # 让出控制权 0.1 秒
        print("  0.1 秒后恢复!")
        return 42

    result = asyncio.run(demo_await())
    print(f"  返回值: {result}")

    # 概念 3: asyncio.run()
    print("""
    概念 3: asyncio.run() — 启动事件循环
    ─────────────────────────────────────
    事件循环 = 一个"调度器"，管理所有协程的执行。
    就像一个项目经理: 谁在等 → 先做别的 → 等完了回来继续。

    asyncio.run(coro) 做三件事:
      1. 创建事件循环
      2. 运行协程直到完成
      3. 关闭事件循环
    """)

    # 概念 4: asyncio.gather()
    print("""
    概念 4: asyncio.gather() — 并发执行
    ─────────────────────────────────────
    """)

    async def task(name, delay):
        start = time.time()
        await asyncio.sleep(delay)
        elapsed = time.time() - start
        return f"{name}: {elapsed:.2f}s"

    async def demo_gather():
        # 三个任务同时开始
        results = await asyncio.gather(
            task("A", 0.3),
            task("B", 0.2),
            task("C", 0.1),
        )
        return results

    results = asyncio.run(demo_gather())
    for r in results:
        print(f"    {r}")
    print("    ↑ 三个任务同时开始，各自独立完成")


# ============================================================
# 第三课: AsyncIterator——异步迭代器（SSE 的核心）
# ============================================================

def lesson_3_async_iterator():
    """
    SSE 流式响应 = 一个"异步迭代器"
    每次 yield 一个事件，直到流结束。

    就像:
    普通迭代器: for item in [1, 2, 3]  → 立即可用
    异步迭代器: async for item in stream → 可能要等网络
    """
    print("\n" + "=" * 60)
    print("第三课: 异步迭代器（SSE 流的基础）")
    print("=" * 60)

    # 用 async generator 模拟 SSE 流
    async def fake_sse_stream() -> AsyncIterator[dict]:
        """模拟 Anthropic API 的 SSE 流"""
        events = [
            {"type": "message_start", "message": {"id": "msg_123"}},
            {"type": "content_block_start", "index": 0},
            {"type": "content_block_delta", "delta": {"text": "Hello"}},
            {"type": "content_block_delta", "delta": {"text": " World"}},
            {"type": "content_block_delta", "delta": {"text": "!"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ]
        for event in events:
            await asyncio.sleep(0.1)  # 模拟网络延迟
            yield event  # 像 return，但不终止函数

    async def consume_stream():
        """消费 SSE 流，逐字拼接文本"""
        full_text = ""
        async for event in fake_sse_stream():
            event_type = event["type"]
            if event_type == "content_block_delta":
                chunk = event["delta"]["text"]
                full_text += chunk
                print(f"    收到: '{chunk}'  (累计: '{full_text}')")
            else:
                print(f"    事件: {event_type}")
        return full_text

    print("\n  模拟 SSE 流消费:")
    result = asyncio.run(consume_stream())
    print(f"\n  最终文本: '{result}'")

    print("""
    关键语法:
      async def stream() -> AsyncIterator:
          yield item             # async generator

      async for item in stream():
          process(item)          # async for 循环

    这就是 Claude Code 的 MessageStream.next_event() 的 Python 版。
    """)


# ============================================================
# 第四课: asyncio.wait_for()——超时控制
# ============================================================

def lesson_4_timeout():
    """
    Claude Code 的超时控制 (bash.rs:109-130) 用的就是这个模式。
    """
    print("\n" + "=" * 60)
    print("第四课: 超时控制")
    print("=" * 60)

    async def slow_operation():
        """一个很慢的操作"""
        await asyncio.sleep(10)  # 10 秒
        return "完成"

    async def demo_timeout():
        # 给慢操作设置 0.5 秒超时
        try:
            result = await asyncio.wait_for(
                slow_operation(),
                timeout=0.5,
            )
            print(f"  成功: {result}")
        except asyncio.TimeoutError:
            print(f"  超时! 操作在 0.5s 后被取消")

    print("\n  等待一个 10s 的操作，但超时设为 0.5s:")
    asyncio.run(demo_timeout())

    # Claude Code 的 send_with_retry 超时模式
    print("\n  在 Claude Code 中的应用:")
    print("""
    # bash.rs 中的超时控制 (简化的 Python 版)
    async def execute_with_timeout(command, timeout_ms):
        try:
            result = await asyncio.wait_for(
                run_command(command),
                timeout=timeout_ms / 1000,
            )
            return BashOutput(stdout=result, interrupted=False)
        except asyncio.TimeoutError:
            return BashOutput(
                stderr=f"Command exceeded timeout of {timeout_ms} ms",
                interrupted=True,
            )
    """)


# ============================================================
# 第五课: 实战——写一个异步 SSE 客户端
# ============================================================

def lesson_5_sse_client():
    """
    把前面学到的全部串起来:
    写一个可以处理超时和重试的异步 SSE 客户端。
    这就是 mini-claude-code 的 API 客户端骨架。
    """
    print("\n" + "=" * 60)
    print("第五课: 实战——异步 SSE 客户端")
    print("=" * 60)

    # SSE 事件
    class SseEvent:
        def __init__(self, event: str = "", data: str = ""):
            self.event = event
            self.data = data

        def __repr__(self):
            return f"SseEvent(event='{self.event}', data='{self.data[:40]}...')"

    # 模拟网络层: 服务器返回 SSE 字节流
    async def fake_network_stream(fail_first: bool = False):
        """模拟从网络读取 SSE 字节"""
        if fail_first:
            raise ConnectionError("模拟网络错误")

        chunks = [
            b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n",
            b"event: content_block_delta\ndata: {\"delta\":{\"text\":\"Hi\"}}\n\n",
            b"event: content_block_delta\ndata: {\"delta\":{\"text\":\" there\"}}\n\n",
            b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
        ]
        for chunk in chunks:
            await asyncio.sleep(0.05)
            yield chunk

    # SSE 解析器
    async def parse_sse_stream(byte_stream) -> AsyncIterator[SseEvent]:
        """从字节流解析 SSE 事件"""
        buffer = b""
        async for chunk in byte_stream:
            buffer += chunk
            # SSE 事件以 \n\n 分隔
            while b"\n\n" in buffer:
                raw_event, buffer = buffer.split(b"\n\n", 1)
                event_name = ""
                event_data = ""
                for line in raw_event.decode().split("\n"):
                    if line.startswith("event: "):
                        event_name = line[7:]
                    elif line.startswith("data: "):
                        event_data = line[6:]
                if event_data:
                    yield SseEvent(event=event_name, data=event_data)

    # 带重试的 SSE 客户端
    async def stream_with_retry(max_retries: int = 2):
        """带重试的流式请求——mini-claude-code 的核心"""
        for attempt in range(max_retries + 1):
            try:
                # 第一次请求故意失败，测试重试
                fail = (attempt == 0)
                if fail:
                    print(f"    [尝试 {attempt + 1}] 发起请求...")

                byte_stream = fake_network_stream(fail_first=fail)

                # 用 wait_for 设置整体超时
                async def consume():
                    events = []
                    async for event in parse_sse_stream(byte_stream):
                        events.append(event)
                    return events

                events = await asyncio.wait_for(consume(), timeout=5.0)
                print(f"    [尝试 {attempt + 1}] 成功! 收到 {len(events)} 个事件")
                return events

            except ConnectionError as e:
                print(f"    [尝试 {attempt + 1}] 连接失败: {e}")
                if attempt < max_retries:
                    wait = 0.2 * (2 ** attempt)  # 指数退避
                    print(f"    等待 {wait}s 后重试...")
                    await asyncio.sleep(wait)
                else:
                    raise

            except asyncio.TimeoutError:
                print(f"    [尝试 {attempt + 1}] 超时!")
                raise

    # 完整的使用流程
    async def full_demo():
        print("\n  完整流程: 请求 → 重试 → 解析 → 拼接")
        events = await stream_with_retry()

        # 从事件中提取文本
        text = ""
        for event in events:
            if event.event == "content_block_delta":
                data = json.loads(event.data)
                text += data["delta"]["text"]

        print(f"\n  最终文本: '{text}'")
        return text

    asyncio.run(full_demo())

    print("""
    这就是 mini-claude-code API 客户端的骨架:
    1. async def stream() → 发起 HTTP 请求
    2. async for chunk in response → 逐块读取
    3. parse_sse(chunk) → 解析 SSE 事件
    4. asyncio.wait_for(stream(), timeout) → 超时控制
    5. for attempt in range(retries) → 重试循环
    """)


# ============================================================
# 第六课: asyncio.Queue——生产者/消费者模式
# ============================================================

def lesson_6_queue():
    """
    在 agent 系统中，经常需要一个组件产生数据，另一个组件消费数据。
    比如: SSE 解析器 → 产出事件 → agentic loop 消费事件
    """
    print("\n" + "=" * 60)
    print("第六课: asyncio.Queue（生产者/消费者）")
    print("=" * 60)

    async def sse_producer(queue: asyncio.Queue):
        """生产者: 模拟 SSE 事件到达"""
        events = ["message_start", "delta:Hello", "delta: World", "message_stop"]
        for event in events:
            await asyncio.sleep(0.1)
            await queue.put(event)
            print(f"    [生产者] 放入: {event}")
        await queue.put(None)  # 结束信号

    async def ui_consumer(queue: asyncio.Queue):
        """消费者: 模拟 UI 逐字显示"""
        text = ""
        while True:
            event = await queue.get()
            if event is None:
                break
            if event.startswith("delta:"):
                chunk = event[6:]
                text += chunk
                print(f"    [消费者] 显示: '{chunk}' (累计: '{text}')")
            else:
                print(f"    [消费者] 处理: {event}")

    async def demo():
        queue = asyncio.Queue()
        # 生产者和消费者同时运行
        await asyncio.gather(
            sse_producer(queue),
            ui_consumer(queue),
        )

    print("\n  生产者(SSE)和消费者(UI)同时运行:")
    asyncio.run(demo())

    print("""
    这个模式在 Claude Code 中的应用:
    - SSE 解析器 → Queue → 消息构建器
    - Bash 命令输出 → Queue → 进度报告器
    - 多个 Worker agent → Queue → Leader agent 收集结果
    """)


# ============================================================
# 主程序
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("教程 17: Python async/await")
    print("为 mini-claude-code 的流式 API 做准备")
    print("=" * 60)

    lesson_1_why_async()
    lesson_2_core_concepts()
    lesson_3_async_iterator()
    lesson_4_timeout()
    lesson_5_sse_client()
    lesson_6_queue()

    print("\n" + "=" * 60)
    print("速查表")
    print("=" * 60)
    print("""
    async def f():        定义协程函数
    await coro            等待协程完成（不阻塞事件循环）
    asyncio.run(coro)     启动事件循环并运行
    asyncio.gather(a, b)  并发运行多个协程
    asyncio.wait_for(coro, timeout=N)  设置超时
    asyncio.sleep(N)      异步等待（不阻塞）
    async for x in stream  异步迭代
    async def gen(): yield x  异步生成器
    asyncio.Queue()       异步队列（生产者/消费者）
    """)
