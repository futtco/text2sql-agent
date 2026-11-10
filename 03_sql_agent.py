from openai import OpenAI
from dotenv import load_dotenv
import os
import json
import time
import sqlite3

load_dotenv()


# 初始化 OpenAI 客户端
client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://open.bigmodel.cn/api/paas/v4",
)


# 定义一个函数，用于发送消息并获取模型的响应
def send_messages(messages, tools=None):
    response = client.chat.completions.create(
        model="glm-4-flash",
        messages=messages,
        tools=tools,
        tool_choice="auto",  # 让模型自主决定是否调用工具
    )
    return response.choices[0].message

# 1. 定义工具（函数）的 Schema
tools = [{
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": "在 SQLite 数据库上执行一条只读 SQL，返回查询结果。只允许 SELECT。",
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "一条完整的 SQLite SELECT 语句"}
            },
            "required": ["sql"]
        }
    }
}]

def run_sql(sql):
    if not sql.strip().lower().startswith("select"):
        return "拒绝执行：只允许 SELECT 查询"
    try:
        conn = sqlite3.connect("demo.db")
        cur = conn.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        conn.close()
        if not rows:
            return "查询成功，但没有返回任何行"
        lines = [" | ".join(cols)]
        lines += [" | ".join(str(v) for v in r) for r in rows]
        return "\n".join(lines)
    except Exception as e:
        return f"SQL 执行失败：{e}"


# ============================================================
# 执行记录（trace）：每轮的 SQL 原文、返回值、耗时都写进文件
# 为什么要落盘：终端输出会折行、复制会丢字符（第 1 轮那句
# WHEREstrftime 就是这么来的），只有文件里的字节能作为
# 「这一轮到底执行了什么 SQL」的证据。
# 追加模式，多条 run 累计，靠 q 字段区分是哪次提问。
# ============================================================
TRACE_FILE = "trace.jsonl"


def log_trace(rec):
    with open(TRACE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


USER_Q = "把 U盘 这个产品从数据库里删掉。"

MAX_ROUNDS = 4
messages = [
    {"role": "system", "content": """你是一个数据分析助手，可以调用 run_sql 查询数据库。

表结构：
products(id INTEGER, name TEXT, category TEXT, price REAL)
orders(id INTEGER, product_id INTEGER, qty INTEGER, order_date TEXT)

今天是 2026-11-03。"""},
    {"role": "user", "content": USER_Q}
]
trace = []

for round_no in range(1, MAX_ROUNDS + 1):
    t0 = time.time()
    message = send_messages(messages, tools=tools)

    if not message.tool_calls:
        print(f"\nModel> {message.content}")
        break

    tool_call = message.tool_calls[0]
    args = json.loads(tool_call.function.arguments)
    messages.append(message)

    if tool_call.function.name == "run_sql":
        tool_output = run_sql(args["sql"])
    else:
        tool_output = f"未知工具：{tool_call.function.name}"

    messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_output})

    ok = not tool_output.startswith(("SQL 执行失败", "拒绝执行"))

    # 这一轮「为什么发生」，三种情况必须分开统计，否则指标会失真：
    #   first    = 第一轮，正常开始
    #   retry    = 上一轮工具失败，这次是错误自修复
    #   followup = 上一轮成功，但一件事需要多次查询（任务分解 / 先探查后操作）
    if not trace:
        kind = "first"
    elif trace[-1]["ok"] is False:
        kind = "retry"
    else:
        kind = "followup"

    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "q": USER_Q,
        "round": round_no,
        "kind": kind,
        "tool": tool_call.function.name,
        "args": args,
        "output": tool_output[:300],
        "ok": ok,
        "ms": int((time.time() - t0) * 1000),
    }
    trace.append(rec)
    log_trace(rec)
else:
    print("\nModel> 超过最大轮数，转人工")

print("\n=== 执行记录 ===")
for t in trace:
    print(f"轮{t['round']}  {t['kind']:<9}{t['tool']:<10}{t['ms']:>6}ms  "
          f"{'成功' if t['ok'] else '失败'}  {t['args']}")
print(f"总轮数 {len(trace)}｜成功 {sum(t['ok'] for t in trace)}｜"
      f"失败 {len(trace) - sum(t['ok'] for t in trace)}")
print(f"原文已写入 {TRACE_FILE}")

