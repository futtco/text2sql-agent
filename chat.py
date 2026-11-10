#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""交互模式：用自己的中文问题向 Agent 提问，实时看它查库、纠错的每一轮。

    python chat.py                          # 进入对话，输入 quit 退出
    python chat.py "库里一共有多少个商品？"    # 只问一句就退出

需要 .env 里的 API Key（默认 glm-4-flash）。此模式只用于演示：
不写 trace、不判分、不参与任何评测结果，也不改动历史证据。
"""
import importlib.util
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)  # 评测脚本在模块顶层会读 cases.json

# 复用评测脚本里的表结构、业务规则、只读护栏和带重试的模型请求；
# 不复用它的判分、trace 和 A/B 开关 —— 交互模式不该碰任何实验数据。
_spec = importlib.util.spec_from_file_location(
    "eval_core", os.path.join(BASE, "03b_batch_eval.py"))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

SYSTEM_CHAT = (
    "你是一个数据分析助手，可以调用 run_sql 查询 SQLite 数据库。\n\n"
    f"表结构：\n{core.DDL_ALL}\n\n"
    f"数据字典与业务规则（必须遵守）：\n{core.RULES}\n\n"
    "工作规则：\n"
    "1. 任何数字都必须来自 run_sql 的返回结果，不许推测、估算或凭印象回答。\n"
    "2. 必须先调用 run_sql 看到数据，才能下结论。\n"
    "3. 需要差值、比例、增长率时，让 SQL 直接算出最终数字，不要自己心算。\n"
    "4. 如果操作被拒绝，如实说明，不要假装已经完成。\n\n"
    "确认结果后，用一句中文简短回答，直接给结论和数字；不要输出 SQL，不要加任何标记。"
)

# 模型想跳过工具直接交卷时，代码把它退回重来。
# 这条检查放在代码里而不是只写进提示词：弱模型常常只服从最后一条指令。
NUDGE_MSG = (
    "上面这条回答一次都没有调用 run_sql，不能算完成。"
    "请把要执行的 SELECT 作为 run_sql 的参数发起工具调用，"
    "不要把 SQL 写在正文里，也不要凭记忆给数字。"
)


def ask(question):
    """把一次提问跑成完整的多轮 Agent 循环，逐轮打印模型的决定与工具返回。"""
    messages = [
        {"role": "system", "content": SYSTEM_CHAT},
        {"role": "user", "content": question},
    ]
    tool_rounds = 0
    nudges = 0

    for round_no in range(1, core.MAX_ROUNDS + 1):
        message = core.send_messages(messages, tools=core.tools)

        # 模型决定不再调工具 = 它认为可以交卷了
        if not message.tool_calls:
            content = (message.content or "").strip()
            # 运行时检查：一次库都没查过就不许交卷。
            # 与批量评测里的护栏同一思路，只作用于交互模式，不改动评测代码。
            if tool_rounds == 0 and nudges < 2 and round_no < core.MAX_ROUNDS:
                nudges += 1
                sql_in_text = "select" in content.lower()
                reason = "把 SQL 写在了正文里" if sql_in_text else "还没查过库"
                print(f"  第 {round_no} 轮 · 运行时检查：{reason}，退回让它真正调用工具")
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content":
                                 core.SQL_TEXT_MSG if sql_in_text else NUDGE_MSG})
                continue
            print(f"\n助手> {content}")
            if tool_rounds == 0:
                print("       [提示] 始终没有调用工具，答案是模型直接生成的，请留意。")
            return

        messages.append(message)
        for call in message.tool_calls:
            args = json.loads(call.function.arguments)
            if call.function.name == "run_sql":
                sql = args.get("sql", "")
                print(f"  第 {round_no} 轮 · 决定查库：{sql}")
                output = core.run_sql(sql)
                tool_rounds += 1
            else:
                print(f"  第 {round_no} 轮 · 请求了未知工具：{call.function.name}")
                output = f"未知工具：{call.function.name}"
            shown = str(output).strip().replace("\n", " / ")
            print(f"            数据库返回：{shown[:220]}")
            messages.append({
                "role": "tool", "tool_call_id": call.id, "content": str(output),
            })

    print("\n助手> 达到轮数上限，这一题没能收口，请换个更具体的问法。")


def main():
    print("Text-to-SQL Agent · 交互模式（会调用模型，需要 .env 里的 key）")
    print("输入中文问题后回车；输入 quit / exit 退出。此模式不写 trace、不判分。")
    print("示例：库里一共有多少个商品？ / 2026 年 8 月上架的商品卖了多少件？")

    args = sys.argv[1:]
    if args:
        question = " ".join(args)
        print(f"\n你> {question}")
        ask(question)
        return

    while True:
        try:
            question = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            return
        if not question:
            continue
        if question.lower() in ("quit", "exit", "q", "退出"):
            print("再见")
            return
        ask(question)


if __name__ == "__main__":
    main()
