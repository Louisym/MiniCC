"""
Tutorial 01: Agentic Loop 基础 — Claude Code 的心脏
=====================================================

什么是 Agentic Loop？
--------------------
想象你在和一个特别聪明的助手对话。你说"帮我算 2+2"，助手不是直接回答，
而是说"让我用计算器算一下"，然后拿起计算器按了一下，看到结果是 4，
再告诉你"答案是 4"。

这个过程就是：
  用户说话 → 助手思考 → 助手使用工具 → 看到结果 → 助手再思考 → 给出回答

在代码里，这个"循环"就叫 Agentic Loop（智能体循环）。
它是 Claude Code 最核心的部分 —— 没有它，AI 就只能"说话"，不能"做事"。

对应源码位置：rust/crates/runtime/src/conversation.rs:170-283
（不需要你看 Rust 代码，本教程用 Python 重新实现同样的逻辑）

运行方式：python tutorials/01_agentic_loop_basics.py
"""


# ============================================================
# 第一步：定义消息模型
# ============================================================
# Claude Code 里的对话是由一条条"消息"组成的，就像微信聊天记录。
# 每条消息有两个属性：
#   - role（角色）: 谁说的？用户(user)？助手(assistant)？工具(tool)？
#   - content（内容）: 说了什么？

class Message:
    """一条对话消息，就像聊天记录里的一条。"""

    def __init__(self, role: str, content: str):
        """
        参数:
            role: 谁说的。可选值：
                  "user"      = 用户（你）
                  "assistant" = AI 助手（Claude）
                  "tool"      = 工具返回的结果
            content: 说了什么（文字内容）
        """
        self.role = role
        self.content = content

    def __repr__(self):
        # 方便打印查看
        preview = self.content[:50] + "..." if len(self.content) > 50 else self.content
        return f"Message(role={self.role!r}, content={preview!r})"


# ============================================================
# 第二步：模拟一个"假的" AI（因为我们没有真的 API）
# ============================================================
# 在真正的 Claude Code 里，这里会调用 Anthropic API。
# 我们用一个简单的"剧本式"模拟来代替。
#
# 关键概念："工具调用"
# -------------------
# AI 的回复不只是文字！它可以返回两种东西：
#   1. 普通文字（text）: "答案是 4"
#   2. 工具调用请求（tool_use）: "请帮我执行 add 工具，输入是 2,2"
#
# 当 AI 返回工具调用请求时，我们需要：
#   1. 真正执行那个工具
#   2. 把工具的结果告诉 AI
#   3. 让 AI 继续思考

class FakeAI:
    """
    模拟 AI 的回复。

    第一次被调用时：返回一个工具调用请求（"我要用计算器"）
    第二次被调用时：根据工具结果，返回最终回答

    在真正的 Claude Code 里，这是通过 HTTP 请求调用 Anthropic API 实现的。
    对应源码: rust/crates/api/src/client.rs (AnthropicClient)
    """

    def __init__(self):
        self.call_count = 0  # 记录被调用了几次

    def chat(self, messages: list[Message]) -> dict:
        """
        模拟 AI 的回复。

        参数:
            messages: 整个对话历史（所有聊天记录）
        返回:
            一个字典，表示 AI 的回复。格式有两种：
            - {"type": "text", "text": "..."} 表示普通文字回复
            - {"type": "tool_use", "name": "...", "input": "..."} 表示要调用工具
        """
        self.call_count += 1

        if self.call_count == 1:
            # 第一次：AI 决定使用工具
            print("  [FakeAI] 第 1 次调用 → 我决定使用 'add' 工具")
            return {
                "type": "tool_use",
                "name": "add",         # 工具名称
                "input": "2,2",        # 传给工具的参数
            }
        else:
            # 第二次：AI 看到了工具结果，给出最终回答
            # 先找到工具返回的结果
            tool_result = ""
            for msg in messages:
                if msg.role == "tool":
                    tool_result = msg.content

            print(f"  [FakeAI] 第 2 次调用 → 我看到工具结果是 {tool_result}，给出最终回答")
            return {
                "type": "text",
                "text": f"2 + 2 的答案是 {tool_result}。",
            }


# ============================================================
# 第三步：定义工具
# ============================================================
# "工具"就是 AI 可以使用的功能。比如：
#   - 计算器（加法）
#   - 读文件
#   - 执行 shell 命令
#   - 搜索代码
#
# 在 Claude Code 里，内置工具有：bash, read_file, write_file, edit_file, glob, grep
# 对应源码: rust/crates/tools/src/lib.rs

def tool_add(input_str: str) -> str:
    """
    一个简单的加法工具。
    输入: "2,2" 这样的字符串
    输出: "4" 计算结果的字符串
    """
    numbers = [int(x.strip()) for x in input_str.split(",")]
    result = sum(numbers)
    return str(result)


# 工具注册表：名称 → 函数
# 就像一本"工具目录"，AI 说"我要用 add"，我们就去目录里找到对应的函数
TOOL_REGISTRY = {
    "add": tool_add,
}


# ============================================================
# 第四步：实现 Agentic Loop（最核心的部分！）
# ============================================================
# 这就是 Claude Code 的心脏。
# 对应源码: rust/crates/runtime/src/conversation.rs 的 run_turn() 方法

def run_turn(ai: FakeAI, session: list[Message], user_input: str) -> list[Message]:
    """
    执行一个完整的"对话轮次"。

    这个函数做的事情：
    1. 把用户的输入加到对话历史里
    2. 不断循环：调用 AI → 如果 AI 要用工具就执行 → 再调用 AI → ...
    3. 直到 AI 给出纯文字回复（不再需要工具），循环结束

    参数:
        ai: AI 模型（真实场景下是 Anthropic API）
        session: 对话历史（所有消息的列表）
        user_input: 用户这次说的话

    返回:
        更新后的对话历史
    """
    # 步骤 1: 把用户消息加入对话历史
    session.append(Message(role="user", content=user_input))
    print(f"\n[用户] {user_input}")

    # 步骤 2: 开始 Agentic Loop
    iteration = 0
    max_iterations = 10  # 安全限制，防止无限循环

    while True:
        iteration += 1
        if iteration > max_iterations:
            print("[错误] 超过最大循环次数，强制停止")
            break

        print(f"\n--- 循环第 {iteration} 轮 ---")

        # 步骤 2a: 调用 AI（传入完整的对话历史）
        ai_response = ai.chat(session)

        # 步骤 2b: 判断 AI 的回复类型
        if ai_response["type"] == "text":
            # AI 给出了纯文字回复 → 循环结束！
            assistant_msg = Message(role="assistant", content=ai_response["text"])
            session.append(assistant_msg)
            print(f"[助手] {ai_response['text']}")
            break  # <-- 这就是循环终止的条件

        elif ai_response["type"] == "tool_use":
            # AI 要使用工具 → 我们需要执行工具，把结果告诉 AI
            tool_name = ai_response["name"]
            tool_input = ai_response["input"]
            print(f"[助手] 我要使用工具: {tool_name}({tool_input})")

            # 先把 AI 的"工具请求"消息加入历史
            session.append(Message(
                role="assistant",
                content=f"[tool_use: {tool_name}({tool_input})]"
            ))

            # 执行工具
            if tool_name in TOOL_REGISTRY:
                tool_func = TOOL_REGISTRY[tool_name]
                tool_result = tool_func(tool_input)
                print(f"[工具 {tool_name}] 执行结果: {tool_result}")
            else:
                tool_result = f"错误：未知工具 '{tool_name}'"
                print(f"[错误] {tool_result}")

            # 把工具结果加入对话历史
            session.append(Message(role="tool", content=tool_result))
            # 然后回到循环顶部，再次调用 AI

    return session


# ============================================================
# 第五步：运行！
# ============================================================

def main():
    print("=" * 60)
    print("Tutorial 01: Agentic Loop 基础演示")
    print("=" * 60)
    print()
    print("场景：用户问 '2+2 等于几？'")
    print("AI 不直接回答，而是先调用 add 工具，再根据结果回答。")
    print()

    ai = FakeAI()
    session = []  # 空的对话历史

    # 执行一个对话轮次
    session = run_turn(ai, session, "2+2 等于几？")

    # 打印最终的完整对话历史
    print("\n" + "=" * 60)
    print("完整对话历史：")
    print("=" * 60)
    for i, msg in enumerate(session):
        print(f"  [{i}] {msg}")

    # 解说
    print("\n" + "=" * 60)
    print("关键理解要点：")
    print("=" * 60)
    print("""
    1. Agentic Loop 的核心是一个 while True 循环
    2. 每轮循环调用一次 AI，AI 可能回复文字，也可能要求使用工具
    3. 如果 AI 要用工具 → 执行工具 → 把结果加入对话 → 继续循环
    4. 如果 AI 回复文字 → 循环结束
    5. 对话历史（session）是 AI 的"记忆"，每次调用都传入全部历史

    对应 Claude Code 源码:
    - conversation.rs:170  →  run_turn() 函数入口
    - conversation.rs:183  →  while True 循环开始
    - conversation.rs:196  →  调用 API (api_client.stream)
    - conversation.rs:214  →  判断是否有 tool_use（break 条件）
    - conversation.rs:218  →  遍历 pending_tool_uses 并执行
    """)


if __name__ == "__main__":
    main()
