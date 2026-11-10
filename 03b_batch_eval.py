# -*- coding: utf-8 -*-
"""
03b_batch_eval.py —— Text-to-SQL Agent 批量评测（50 题版）

2026-11-07 接线改动（只换数据源，不加新能力，便于和 demo.db 时期的基线对照）：
1. demo.db      → shop.db（六表电商库）
2. 写死的 3 题  → 从 cases.json 读 20 题
3. SYSTEM 表结构 → 六张表 DDL + cases.json 里的口径字典
4. 判定          → float 比较（容差 0.01），不再比字符串是否一模一样
5. 改造 B（11-07）：nudge 不再要求回答含数字，只看有没有成功查库
   —— 基线里模型用纯文字「做不到」绕过旧检查，5 道题一次库都没查

改造 A（11-04）拦「编数字」，改造 B 拦「装作做不到」，同一条线的两处运行时检查。
跑改造 B 之前请先备份 trace_shop.jsonl，脚本开头会清空它。

红线：cases.json 里的 check_sql 是标准答案的完整 SQL，
      读取时**故意不复制**进 CASES，物理上杜绝误把它塞进 prompt。

跑法（必须在 code/C5 目录，shop.db 是相对路径）：
    python 03b_batch_eval.py                      # 全部 20 题，改造 B 行为
    python 03b_batch_eval.py A1 C1 E2            # 只跑指定题号，验通路用
    python 03b_batch_eval.py --baseline          # 切回改造前的旧 nudge 行为
    python 03b_batch_eval.py --derive            # 单跑时开启改造 C 的派生值
    python 03b_batch_eval.py --ab --repeat 3     # 实验一：唯一变量 = nudge 行为
    python 03b_batch_eval.py --ab-derive --repeat 3  # 实验二：唯一变量 = 派生值开关

两个 A/B 实验都是「同一条命令里先跑一遍基线、再跑一遍改造后，交替 N 轮」。
交替而不是分组跑，是为了让两组摊上同样的时间/服务状态，避免环境漂移混进差值。
每遍单独一个 trace 文件、每遍结束立刻追加汇总行，中途断了已完成的遍不丢。

实验一（--ab）量改造 B：两组都不开派生值，保持与 11-08 历史记录可比。
实验二（--ab-derive）量改造 C：两组 nudge 都固定为改造 B，**唯一变量是派生值**，
这是严格单变量；runs_summary.jsonl 里用 experiment 字段区分两次实验，别混着统计。

改造 C（11-08）：工具返回值派生。D1 六遍全是分两次查询、每次返回一个数值，
模型拿不到「差值」只能心算，而 glm-4-flash 的减法实测不稳定。现在 run_sql 在
「同一题内连续两次单值查询」时，把确定性的差值/比值附在返回文本里。
工具只做算术，不选表、不写 join、不判断哪个月是基准 —— 顺序由模型的 SQL 决定。

逐轮记录：trace_shop_after.jsonl（改造后）、trace_shop_baseline.jsonl（基线）、
trace_shop_smoke.jsonl（3 题验通路）是之前两次单跑的存档，不参与 --ab 统计。
"""

from openai import OpenAI
from dotenv import load_dotenv
import os
import re
import sys
import json
import time
import sqlite3
from collections import Counter

from schema_link import (DDL_ALL, retrieve, retrieve_fixed,     # 改造 F：动态口径路由
                        relation_block, relation_ids)          # 实验七：表关系卡
import result_audit                                            # 实验十一：工具返回值体检（纯函数）

load_dotenv()

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://open.bigmodel.cn/api/paas/v4",
)

DB = "shop.db"
CASES_FILE = "cases.json"
TRACE_FILE = "trace_shop_after.jsonl"   # 每遍运行前重新赋值
MAX_ROUNDS = 6
MAX_ROWS = 20          # run_sql 最多回显 20 行，防止模型 SELECT * 把上下文撑爆

# A/B 开关：切回改造 B 之前的旧 nudge 行为，用来量「同一配置的波动」
BASELINE = False
AB = False
AB_DERIVE = False      # True = 量改造 C（派生值），此时两组 nudge 固定为改造 B
AB_DEDUP = False       # True = 量改造 D（重复SQL早停），其余开关两组一致
AB_E = False           # True = 量改造 E（正文贴SQL 打回），其余开关两组一致
AB_SCHEMA = False      # True = 量改造 F（动态口径路由），其余开关两组一致
AB_FIXED = False       # True = 实验六：固定 8 条 vs 按题路由 8 条，单独量「路由」的净贡献
AB_REL = False         # True = 实验七：两组都开 8 条路由，唯一变量是「表关系卡」
AB_REL_GATE = False    # True = 实验八：无卡 vs「只在跨表意图命中时才发」的门控卡
AB_SELFCHECK = False   # True = 实验九：唯一变量是「交卷前自评」
SCHEMA_LINK = False    # 改造 F 开关，默认关：默认 SYSTEM 必须与历史逐字节一致
SCHEMA_FIXED = False   # 实验六对照臂：8 条规则但不按题挑（与 SCHEMA_LINK 互斥）
SCHEMA_REL = False     # 实验七：按题给 2 张「概念 → 表/JOIN 路径」关系卡
SCHEMA_REL_GATE = False  # 实验八：候选池门控；没命中意图的题整段不注入
SELF_CHECK = False     # 实验九：模型想交卷时，代码先拦下来让它自评「信息够不够」
# 实验十：收口解析层容错（模型只吐裸数字时补 FINAL: 前缀）。默认关，
# 保证实验九及之前所有数据在关掉时逐字节可复现；要量它用 --ab-finalnorm。
FINAL_NORMALIZE = False
AB_FINALNORM = False
# 实验十一：工具返回值体检。默认关，保证所有历史实验的工具返回逐字节可复现；
# 打开时只在 run_sql 返回文本末尾追加一行提示，不改 SQL / 提示词 / 判定。
RESULT_AUDIT = False
AB_RESULTAUDIT = False
REPEAT = 1
SINGLE_BASELINE = False
SINGLE_DERIVE = False
DERIVE = False         # 改造 C 开关，默认关：默认行为必须与 11-08 历史记录一致
# 改造 D（11-09）：重复 SQL 不重跑。同一题内「归一化后一字不差」的 SQL 再出现，
# 不再执行（结果不可能变），直接把「这条查过了」告诉模型；连续命中 DUP_HIT_LIMIT 次就收口。
# 默认开 —— 重跑相同 SQL 永远没有收益。要复现历史行为用 --no-dedup。
DEDUP = True
DUP_HIT_LIMIT = 2      # 连续命中重复多少次后强制收口（第 3 次撞同一句时停）
# 改造 E（11-09）：模型把 SQL 写在正文里、tool_call 却是空的 —— 它以为自己在调工具。
# 这种情况下改造 B 的提示语（「你还没有调用 run_sql」）会被理解成「再写一遍」，于是空转到轮数上限。
# 现在改成识别 + 换话术，最多提醒 SQL_NUDGE_LIMIT 次，再犯就强制收口。
SQL_GUARD = True
SQL_NUDGE_LIMIT = 2
# 实验九（收口闸门 / Agent 自评）：模型准备输出 FINAL 时，代码先拦下来，
# 用一次不带工具的请求让它对照题面核一遍「信息够不够」。
# 这不同于 nudge（查没查库）和 sqlguard（真调没调工具）—— 那些是代码查行为，
# 这一步是让 Agent 自己判断「已经查到的东西够不够回答问题」。
# 自评本身是纯函数 + 一次额外请求，最多触发 SELF_CHECK_LIMIT 次，防止无限循环。
SELF_CHECK_LIMIT = 1
# 11-10 探测结论：先试过「开放式自评」（让模型自己判断信息够不够、还缺不缺一次查询），
# 给它看明显漏了 status 的 SQL，6/6 一律答 CHECK: OK —— 和表关系卡一样没被采纳。
# 但同一个模型被问「这条 SQL 里有没有单词 status？只答 有/没有」时 4/4 全对。
# 所以不是模型读不出 SQL，是开放式判断太虚。改成：代码给一条可验证的检查项，
# 模型只做字面判定，代码拿到答案后决定是否打回。决策权在代码，不在模型的自我感觉。
SELF_CHECK_TRIGGERS = ("卖出", "卖了", "销量", "销售额", "毛利", "成交", "客单价")
SELF_CHECK_MISS_REASON = "已执行的 SQL 里没有订单状态过滤（需按有效订单口径重算）"

SELF_CHECK_SYS = """你是一个字面核对员。只看下面给出的 SQL 原文，回答一个非常简单的判断题。
不要查数据库，不要解释，不要写 SQL，不要评价 SQL 对不对。

只回答一个词：有 或 没有。"""

SELF_CHECK_TPL = """已执行过的 SQL 原文：
{evidence}

判断题：这些 SQL 里，有没有任何一处出现过单词 status（不区分大小写）？
只回答一个词：有 或 没有。"""


def selfcheck_needed(question):
    """代码侧的门控：只有成交口径题才需要检查「有没有订单状态过滤」。

    纯题面关键词，不读 expect / check_sql / decoys；非成交题整条闸门都不触发，
    于是这些题的提示词与流程跟对照组逐字节一致（先验阴性对照）。
    """
    return any(w in question for w in SELF_CHECK_TRIGGERS)


# 自评说「没有」时追加的话：只说明缺什么，不替模型写 SQL、不给答案数值
SELF_CHECK_MORE_MSG = ("收口检查没通过：{reason}。"
                       "请重新写一条 SQL，把有效订单的条件带上，再给出 FINAL: <答案>。"
                       "不要直接沿用刚才那个数。")
# 答题模型。默认仍为 glm-4-flash，保证历史数据可比；
# 换模型跑对照：EVAL_MODEL=glm-4-plus python 03b_batch_eval.py
# 注意：只换这里，judge() 用的判卷模型不动，否则判定标准跟着变，数据不可比。
MODEL = os.getenv("EVAL_MODEL", "glm-4-flash")
RUNS_SUMMARY = "runs_summary.jsonl"

NUDGE_MSG_AFTER = ("库里有回答这道题所需的数据。不要直接说做不到，"
                   "请调用 run_sql 查询后再回答。")
NUDGE_MSG_BASELINE = ("你还没有调用 run_sql。任何数字都必须来自查询结果，"
                      "请先调用 run_sql 再回答。")
# 改造 E 专用：区别在于说清「你写了，但没发出去」—— 旧提示语会让它再抄一遍
SQL_TEXT_MSG = ("你刚才把 SQL 写在了回答正文里，那只是文字，数据库并没有执行。"
                "请真正调用 run_sql 工具（把 SQL 放进工具参数里）发出去；"
                "工具返回结果之后再回答。不要再把 SQL 写在回答里。")


MAX_API_RETRY = 6


def send_messages(messages, tools=None):
    """A/B 要连跑几十分钟，临时 500 / 代理 502 / 超时不该让整轮白跑。

    重试只针对请求本身，不改变两组的判定逻辑，对 A/B 是对称的。
    2026-11-10：实验十曾被本机代理 `502 Bad Gateway` 打断在第 1 遍 C1，
    3 次重试不够，改为 6 次指数退避（上限 20s）。余额不足属不可逆错误，
    直接抛出，不浪费几十秒重试。
    """
    last = None
    for attempt in range(MAX_API_RETRY):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            return response.choices[0].message
        except Exception as e:                 # noqa: BLE001 - 临时错误一律重试
            last = e
            if "余额不足" in str(e) or "insufficient" in str(e).lower():
                raise                          # 充值类错误重试也没用
            if attempt < MAX_API_RETRY - 1:
                wait = min(2 ** attempt, 20)
                print(f"    (API 临时错误，{wait}s 后重试 {attempt + 1}/{MAX_API_RETRY - 1}：{type(e).__name__})")
                time.sleep(wait)
    raise last


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


# ---------------------------------------------------------------- 读考卷
# 只取 id / type / q / expect / decoys。
# check_sql 和 note 一律不读进来 —— 它们含完整答案 SQL，进 prompt 就是泄题。
with open(CASES_FILE, encoding="utf-8") as f:
    _raw = json.load(f)

CASES = [{
    "id": c["id"],
    "type": c["type"],
    "q": c["q"],
    "expect": c["expect"],
    "decoys": c.get("decoys", {}),
} for c in _raw["cases"]]

RULES = "\n".join(f"{i}. {r}" for i, r in enumerate(_raw["meta"]["system_rules"], 1))

SYSTEM_TPL = """你是一个数据分析助手，可以调用 run_sql 查询 SQLite 数据库。

表结构：
{ddl}

数据字典与业务规则（必须遵守）：
{rules}

工作规则：
1. 任何数字都必须来自 run_sql 的返回结果，不许推测、估算或凭印象回答。
2. 必须先调用 run_sql 看到数据，才能下结论。
3. 需要差值、比例、增长率时，让 SQL 直接算出最终数字，不要拿到两个数后自己心算。
4. 如果操作被拒绝（例如非 SELECT 语句），如实说明，不要假装已经完成。

回答的最后一行必须是：FINAL: <数字或"拒绝">
只写数字，不加单位、不加解释。金额单位是元，数值减少用负号表示。"""

# 关闭改造 F 时用这一份：DDL 和 12 条规则全给，与 11-09 之前的历史记录完全一致。
SYSTEM = SYSTEM_TPL.format(ddl=DDL_ALL, rules=RULES)


def build_system(question):
    """上下文的几种形态，表结构一律冻结为 DDL_ALL：

    off       → 12 条平铺（历史 SYSTEM，逐字节一致）
    fixed8    → 5 常驻 + 固定 3 条（实验六的基线）
    routed    → 5 常驻 + 按题 top-3（改造 F 的实验组）
    +rel      → 上述任意形态，再在 DDL 后接 2 张按题挑的「表关系卡」（实验七）

    关系卡不新增模板占位符，而是接在 ddl 参数后面 —— 这样关闭时 SYSTEM 才能
    与历史逐字节一致，DDL_ALL 也始终是前缀（离线验收会查）。
    """
    if SCHEMA_FIXED:
        r = retrieve_fixed(question)
        return SYSTEM_TPL.format(ddl=r["ddl"], rules=r["rules"])
    if not SCHEMA_LINK and not SCHEMA_REL:
        return SYSTEM
    rules = retrieve(question)["rules"] if SCHEMA_LINK else RULES
    ddl = DDL_ALL
    if SCHEMA_REL:
        blk = relation_block(question, gated=SCHEMA_REL_GATE)
        if blk:        # 门控未命中时整段不出现，与无卡组逐字节相同（阴性对照）
            ddl = DDL_ALL + "\n\n" + blk
    return SYSTEM_TPL.format(ddl=ddl, rules=rules)


# ---------------------------------------------------------------- 改造 C：工具返回值派生
def scalar_value(rows, cols):
    """结果恰好 1 行 1 列且是数值时返回 float，否则 None。
    严格限定形状，是为了不给「查 id 列表」「查多列分组」这类结果乱补差值。"""
    if len(rows) != 1 or len(cols) != 1:
        return None
    v = rows[0][0]
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def derive_text(prev, cur):
    """相邻两次单值查询的确定性派生值：只做算术，不做业务判断。

    prev = 上一轮返回值、cur = 本轮返回值，顺序完全由模型的查询顺序决定。
    工具不判断「哪个月是基准」「题目要变化量还是变化率」，那些是语义活，
    留给模型 —— 所以两个候选都摆出来、标签写死，但不替它选。
    """
    delta = prev - cur
    # 用 str(float) 而不是 :g，保证和 SQL 原始返回的数字一模一样，
    # 否则 80836.75 会被显示成 80836.8，模型会以为是两个不同的数。
    tail = (f"[工具派生值] 相邻两次查询：上一轮 {prev}，本轮 {cur}\n"
            f"  差值(上一轮 − 本轮) = {delta:.2f}")
    if cur != 0:
        tail += f"\n  变化率(相对本轮) = {delta / cur * 100:.2f}%"
    tail += "\n  （由工具计算；题目问变化量用差值、问变化率用百分比，符号按题意判断）"
    return tail


# 只读白名单：SELECT，以及以 WITH 开头、最终是 SELECT 的只读 CTE。
# 11-10 发现：关系卡教会模型用递归 CTE 查类目树，旧白名单却把 WITH 一律拒掉，
# 模型受挫后直接改答「拒绝」—— 护栏把「本可自修复的 SQL 错误」变成了死路。
# 仍拦：写操作关键字（含 WITH ... INSERT/UPDATE/DELETE）、分号拼接的多语句。
_WRITE_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|pragma|attach|detach|reindex|vacuum|replace\s+into)\b",
    re.I)


def is_readonly_query(sql) -> bool:
    s = str(sql).strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", s, re.I):
        return False
    if _WRITE_SQL.search(s):
        return False
    body = re.sub(r"--[^\n]*", "", s)   # 去掉行注释再看有没有塞第二条语句
    return ";" not in body


def run_sql(sql, session=None, derive=False):
    # 安全护栏放在代码里，不放在 prompt 里：模型看不见这条规则也绕不过去
    if not is_readonly_query(sql):
        return "拒绝执行：只允许只读 SELECT（或只读 WITH ... SELECT）查询"
    try:
        conn = sqlite3.connect(DB)
        cur = conn.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        conn.close()
        # 实验十一：只在开关打开时体检，关掉时下面整段不产生任何字节
        hint = result_audit.audit(sql, rows, cols) if RESULT_AUDIT else ""
        if not rows:
            base = "查询成功，但没有返回任何行"
            return base + ("\n" + hint if hint else "")
        lines = [" | ".join(cols)]
        shown = rows[:MAX_ROWS]
        lines += [" | ".join(str(v) for v in r) for r in shown]
        if len(rows) > MAX_ROWS:
            lines.append(f"... 共 {len(rows)} 行，只显示前 {MAX_ROWS} 行")

        # session 由 run_case 在每题开头新建，题目之间不会串味。
        # 非单值结果会打断连续性并清空，避免拿不相干的数去算差值。
        val = scalar_value(rows, cols)
        if session is not None:
            if val is None:
                session.clear()
            else:
                if derive and session:
                    lines.append(derive_text(session[-1], val))
                session.append(val)
        if hint:
            lines.append(hint)
        return "\n".join(lines)
    except Exception as e:
        return f"SQL 执行失败：{e}"


def log_trace(rec):
    with open(TRACE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def normalize_final_text(tail):
    """收口解析层容错：模型已经给出答案、只是漏了 `FINAL: ` 前缀时补上。

    11-10 实验九发现：模型补对 SQL、算出正确数字后，finalize 常常只回一个裸数字
    （19 / 52842.45 / 5），judge 因找不到 FINAL 行判成 noformat，白白算错。
    只处理「整个回复就是一行裸数字或拒绝词」这一种确定情形；有多行解释、
    或者已经带 FINAL: 的一律原样返回 —— 宁可漏判也不替模型决定答案。
    这是解析层规范化，不改 judge、不碰 expect、不比对答案，两组完全对称。
    """
    if not tail:
        return tail
    s = tail.strip()
    if "\n" in s or s.upper().startswith("FINAL:"):
        return tail
    body = s.strip("<>《》「」\"' \u3000。.")
    if re.fullmatch(r"[-+]?\d[\d,]*(?:\.\d+)?", body) or body in ("拒绝", "无法", "不能"):
        return "FINAL: " + body
    return tail


def finalize(messages, answer, case_id, trace, stopped=None):
    """回答里没有 FINAL 行时，补一次请求逼它收口。
    关键是不传 tools：没有工具可选，模型只能输出文本，
    格式遵守率比在同一次请求里"既允许调工具又要求特定格式"高得多。

    stopped="dup"     ：被重复 SQL 早停打断，手里压根没有 answer
    stopped="sqltext" ：被「写 SQL 不真调工具」打回到上限，手里也没有 answer
    """
    if not stopped and (not answer or final_of(answer) is not None):
        return answer

    t0 = time.time()
    msgs = list(messages)
    if answer:
        msgs.append({"role": "assistant", "content": answer})
        tip = ("不要解释，不要写尖括号。只输出一行，格式为 FINAL: 后接最终答案"
               "（是数字就只写数字，做不到就写 拒绝）。")
    elif stopped == "sqltext":
        tip = ("不要再写 SQL，也不要再调工具（工具已经不可用）。"
               "只能用上面已有的信息输出一行：FINAL: 后接最终答案；确实定不下来就写 FINAL: 拒绝。")
    else:
        tip = ("你刚才反复提交了同一条查询，工具不会重复执行。不要再调工具，"
               "只根据上面已经拿到的结果输出一行：FINAL: 后接最终答案；"
               "确实定不下来就写 FINAL: 拒绝。")
    msgs.append({"role": "user", "content": tip})
    msg = client.chat.completions.create(model=MODEL, messages=msgs)
    tail = msg.choices[0].message.content or ""

    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "q": case_id,
        "round": len(trace) + 1,
        "kind": "finalize",
        "tool": "(none)",
        "args": {},
        "output": tail[:300],
        "ok": True,
        "ms": int((time.time() - t0) * 1000),
    }
    trace.append(rec)
    log_trace(rec)
    if FINAL_NORMALIZE:
        tail = normalize_final_text(tail)
    return (answer + "\n" + tail) if answer else tail


# ---------------------------------------------------------------- A/B 两套 nudge
def should_nudge(case, trace, round_no, content="", baseline=False):
    """是否把这次「没调工具就想交卷」的回答打回。

    baseline=True 精确复现 11-07 16:05 那次基线的旧行为：
      只有回答里含数字才打回，且**没有拒答题豁免**（当时确实误伤过）。
      模型后来学会用纯文字「我没有客户城市的信息」绕过去，5 道题一次库都没查。
    baseline=False 是改造 B：只看有没有一次成功的 run_sql，不看回答长什么样，
      并给拒答题和最后一轮留豁免。

    两套行为共用一个函数，是为了 A/B 交替跑时除了这一处以外完全相同。
    抽成纯函数也方便离线验证，不用花 API 去试。
    """
    if round_no >= MAX_ROUNDS:
        return False
    if any(t["tool"] == "run_sql" and t["ok"] for t in trace):
        return False
    if baseline:
        return bool(re.search(r"\d", content))
    if case["expect"] == "拒绝":
        return False
    return True


def sql_key(sql):
    """归一化 SQL，用来判断「这句是不是刚查过」。
    只压空白、去反引号、转小写、去尾分号 —— 不做语义等价，宁可漏判也不错杀。"""
    return " ".join(str(sql).replace("`", "").split()).lower().rstrip(";")


def looks_like_sql(text):
    """正文里是不是写了一段像 SQL 的东西（select ... from）。

    故意写得保守：必须有 select，且 600 字内跟一个 from。
    只提到「我会用 SELECT 查一下」不算，免得把正常解释也打回。
    """
    if not text:
        return False
    return re.search(r"\bselect\b[\s\S]{0,600}?\bfrom\b", text, re.I) is not None


def should_sqltext_nudge(case, trace, content):
    """改造 E 的判定：以为调了工具、其实没调。

    三个条件同时满足才打回，缺一不可：
      1. 正文里真的写了 SQL；
      2. 本题到现在还没有一次成功的 run_sql（有成功过就只是解释，不该打回）；
      3. 不是拒答题（越权题的正解本来就可能是「我不会执行 DELETE ...」，会提到 SQL）。
    """
    if not SQL_GUARD:
        return False
    if case.get("expect") == "拒绝":
        return False
    if any(t["tool"] == "run_sql" and t["ok"] for t in trace):
        return False
    return looks_like_sql(content)


# ---------------------------------------------------------------- 实验九：收口闸门（Agent 自评）
def parse_selfcheck(text):
    """把字面核对员的回答解析成 (verdict, reason)。纯函数，方便离线单测。

    只认「没有」= 判缺；其余（包括格式不合规、多话、空回复）一律放行。
    自评是「多给一轮机会」，绝不能因为模型没写对格式就把本来能交的卷打回去
    （那会重演改造 B 把「不查就答」换成「查了不答」）。
    保留 CHECK: MORE 兼容格式，方便回溯 11-10 那版开放式自评的日志。
    """
    if not text:
        return "ok", ""
    for line in reversed(text.splitlines()):
        s = line.strip().strip("。.！! ")
        if s == "没有" or s.upper() in ("NO", "NONE"):
            return "more", SELF_CHECK_MISS_REASON
        if s.upper().startswith("CHECK:") and s.upper().startswith("CHECK: MORE"):
            reason = s.split(":", 2)[2].strip() if s.count(":") >= 2 else ""
            return "more", (reason or SELF_CHECK_MISS_REASON)[:40]
        if s == "有" or s.upper().startswith("CHECK: OK"):
            return "ok", ""
    return "ok", ""


def selfcheck_evidence(trace):
    """只把本题已经成功的 SQL 和返回结果摆给复核员，不碰 expect / check_sql。"""
    good = [t for t in trace if t["tool"] == "run_sql" and t["ok"]]
    if not good:
        return "（没有成功执行过任何查询）"
    return "\n".join(f"{i}. SQL: {t['args'].get('sql', '')}\n   结果: {t['output']}"
                     for i, t in enumerate(good, 1))


def run_selfcheck(case, trace):
    """交卷前的一次字面核对。不带工具，只能回文本；门控未命中/请求失败都不打回。

    门控放在代码里：非成交口径题直接返回 None，连一次 API 都不花，
    也让这些题的行为与对照组完全一致（阴性对照）。
    """
    if not selfcheck_needed(case["q"]):
        return None
    # 只把已成功执行的 SQL 原文给它看，不给答案、不给标准 SQL、不给业务规则长文
    sqls = [t["args"].get("sql", "") for t in trace
            if t["tool"] == "run_sql" and t["ok"]]
    msgs = [
        {"role": "system", "content": SELF_CHECK_SYS},
        {"role": "user", "content": SELF_CHECK_TPL.format(evidence="\n---\n".join(sqls))},
    ]
    t0 = time.time()
    try:
        r = client.chat.completions.create(model=MODEL, messages=msgs)
        raw = r.choices[0].message.content or ""
        verdict, reason = parse_selfcheck(raw)
    except Exception as e:                 # noqa: BLE001 - 复核失败不该拖垮答题
        raw, verdict, reason = f"[复核请求失败] {type(e).__name__}", "ok", ""
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "q": case["id"],
        "round": len(trace) + 1,
        "kind": "selfcheck",
        "tool": "(none)",
        "args": {"verdict": verdict, "reason": reason, "sqls": sqls[:2]},
        "output": raw[:300],
        "ok": verdict == "more",
        "ms": int((time.time() - t0) * 1000),
    }
    return verdict, reason, rec


def run_case(case, system=None):
    """跑一道题。messages 和 trace 都建在函数内，保证题目之间互不影响。"""
    messages = [
        {"role": "system", "content": system or SYSTEM},
        {"role": "user", "content": case["q"]},
    ]
    trace = []
    answer = None
    session = []          # 改造 C：本题内的单值查询序列，每题独立，不跨题串味
    seen_sql = {}         # 改造 D：归一化 SQL -> 首次执行它的轮次
    dup_hits = 0          # 连续命中重复的次数
    sql_hits = 0          # 改造 E：正文贴 SQL 的次数
    selfcheck_used = 0    # 实验九：本题已经自评过几次（上限 SELF_CHECK_LIMIT）
    stopped = None        # 被护栏强制收口的原因，交给 finalize 用不同的提示语

    for round_no in range(1, MAX_ROUNDS + 1):
        t0 = time.time()
        message = send_messages(messages, tools=tools)

        if not message.tool_calls:
            content = message.content or ""
            # 改造 E 优先于改造 B：同样「没调工具」，但成因不同，话术必须不同
            if should_sqltext_nudge(case, trace, content):
                sql_hits += 1
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": SQL_TEXT_MSG})
                rec = {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "q": case["id"],
                    "round": round_no,
                    "kind": "sqltext",
                    "tool": "(none)",
                    "args": {},
                    "output": content[:300],
                    "ok": False,
                    "ms": int((time.time() - t0) * 1000),
                }
                trace.append(rec)
                log_trace(rec)
                if sql_hits >= SQL_NUDGE_LIMIT:
                    stopped = "sqltext"     # 提醒够了还不改，就别再耗轮数
                    answer = None
                    break
                continue
            # A/B 只在这一处不同：判定规则 + 配套提示语，其余代码路径完全一致
            if should_nudge(case, trace, round_no, content, baseline=BASELINE):
                messages.append({"role": "assistant", "content": message.content or ""})
                messages.append({"role": "user", "content":
                                 NUDGE_MSG_BASELINE if BASELINE else NUDGE_MSG_AFTER})
                nudge = {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "q": case["id"],
                    "round": round_no,
                    "kind": "nudge",
                    "tool": "(none)",
                    "args": {},
                    "output": content[:300],
                    "ok": True,
                    "ms": int((time.time() - t0) * 1000),
                }
                trace.append(nudge)
                log_trace(nudge)
                continue
            # 实验九：交卷闸门。模型这一轮没调工具 = 它想收口，此时才拦。
            # 不要求正文里有 FINAL：flash 常常只写解释、靠 finalize 兜底才出 FINAL，
            # 若卡「必须有 FINAL」会把大半题漏掉（11-10 smoke 实测 5 题只触发 1 次）。
            # 排除拒答题、没查过库的题、最后两轮 —— 避免自评制造新的空转。
            if (SELF_CHECK and selfcheck_used < SELF_CHECK_LIMIT
                    and case["expect"] != "拒绝"
                    and selfcheck_needed(case["q"])
                    and any(t["tool"] == "run_sql" and t["ok"] for t in trace)
                    and round_no < MAX_ROUNDS - 1):
                out = run_selfcheck(case, trace)
                selfcheck_used += 1
                if out is None:
                    answer = message.content
                    break
                verdict, reason, rec = out
                trace.append(rec)
                log_trace(rec)
                if verdict == "more":
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user",
                                     "content": SELF_CHECK_MORE_MSG.format(reason=reason)})
                    continue          # 再给它一轮，让它自己补查
            answer = message.content
            break

        tool_call = message.tool_calls[0]
        args = json.loads(tool_call.function.arguments)
        messages.append(message)

        dup = False
        if tool_call.function.name == "run_sql":
            key = sql_key(args.get("sql", ""))
            if DEDUP and key in seen_sql:
                # 不执行：同样的 SQL 结果不可能变，重跑只是烧 token 和轮数
                dup = True
                dup_hits += 1
                tool_output = (f"[重复查询] 这条 SQL 在第 {seen_sql[key]} 轮已经执行过，结果不会变。"
                               f"请改用不同的写法（换个 join、换过滤条件），或直接输出 FINAL: <答案>。")
            else:
                tool_output = run_sql(args["sql"], session=session, derive=DERIVE)
                seen_sql[key] = round_no
                dup_hits = 0
        else:
            tool_output = f"未知工具：{tool_call.function.name}"

        messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_output})

        ok = not tool_output.startswith(("SQL 执行失败", "拒绝执行", "[重复查询]"))

        if not trace:
            kind = "first"
        elif dup:
            kind = "dup"            # 重复 SQL：单独一类，别混进 retry/followup
        elif trace[-1]["kind"] == "nudge" or trace[-1]["ok"] is False:
            kind = "retry"          # 上一轮出错或被退回，这次是纠正后的重来
        else:
            kind = "followup"       # 上一轮成功，为完成任务再查一次

        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "q": case["id"],
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

        if dup_hits >= DUP_HIT_LIMIT:
            # 连着撞同一句 SQL：不再给它第 7 轮，直接交给 finalize 用手上的数收口
            stopped = "dup"
            answer = None
            break

    answer = finalize(messages, answer, case["id"], trace, stopped=stopped)
    return answer, trace


def final_of(answer):
    """从最后一行抠出 FINAL: 后面的内容。没有就返回 None（格式不合规）。"""
    if not answer:
        return None
    for line in reversed(answer.splitlines()):
        if line.strip().startswith("FINAL:"):
            v = line.split(":", 1)[1].strip()
            return v.strip("<>《》「」\"' \u3000") or None
    return None


def norm(s):
    """归一化：去逗号、去空格、去单位、统一各种减号写法。"""
    s = s.strip()
    for ch in (",", "，", " ", "元", "\t", "%", "％"):
        s = s.replace(ch, "")
    for ch in ("\u2212", "－", "—", "–"):
        s = s.replace(ch, "-")
    return s


def to_number(s):
    """把 '约 4,589.49 元' / '−20,025.75' / '<6999>' 之类抠成 float。"""
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", norm(str(s)))
    return float(m.group()) if m else None


def decoy_hit(case, f):
    """final 命中了哪个典型错误口径 —— 不用猜模型错在哪一步。"""
    v = to_number(f)
    if v is None:
        return None
    for name, d in (case.get("decoys") or {}).items():
        if abs(v - float(d)) <= 0.01:
            return name
    return None


def judge(case, answer, trace):
    """返回 (status, final)。
    ok / wrong / noformat / no_tool / manual —— 失败必须分类，否则无法归因。
    数值题按 float 比较，容差 0.01：6999 / 6999.0 / 6,999 是同一个答案。
    """
    if answer is None:
        return "manual", None

    f = final_of(answer)

    if f is None:
        if case["expect"] == "拒绝" and any(k in (answer or "") for k in ("拒绝", "无法", "不能")):
            return "ok", "拒绝(未标注)"
        if not any(t["tool"] == "run_sql" and t["ok"] for t in trace):
            return "no_tool", None
        return "noformat", None

    refused = any(k in f for k in ("拒绝", "无法", "不能"))

    if case["expect"] == "拒绝":
        return ("ok" if refused else "wrong"), f

    if not any(t["tool"] == "run_sql" and t["ok"] for t in trace):
        return "no_tool", f

    if refused:
        return "wrong", f

    got, want = to_number(f), to_number(case["expect"])
    if got is None or want is None:
        same = norm(f) == norm(str(case["expect"]))
    else:
        same = abs(got - want) <= 0.01
    return ("ok" if same else "wrong"), f


# ---------------------------------------------------------------- 统计（纯函数，可离线验证）
def summarize(rows):
    """把逐题结果压成一行指标。所有 A/B 对比都只用这里的字段。"""
    n = len(rows)
    ok = sum(r["status"] == "ok" for r in rows)
    need = [r for r in rows if r["needs_tool"]]
    tool_cnt = sum(r["used_tool"] for r in need)   # 只数需要数据的题，否则越权题真去查了会算出 >100%
    tried = [r for r in rows if r["retried"]]
    return {
        "n": n,
        "ok": ok,
        "acc": ok / n,
        "tool_cnt": tool_cnt,
        "need": len(need),
        "tool_rate": tool_cnt / len(need) if need else None,
        "avg_rounds": sum(r["rounds"] for r in rows) / n,
        "avg_tool_rounds": sum(r["tool_rounds"] for r in rows) / n,
        "fmt": sum(r["status"] not in ("noformat", "manual") for r in rows) / n,
        "nudged": sum(r["nudged"] for r in rows),
        "sql_fail": sum(r["sql_fail"] for r in rows),
        "dup_cnt": sum(r["dup"] for r in rows),
        "sqltext_cnt": sum(r["sqltext"] for r in rows),
        "selfcheck_cnt": sum(r["selfcheck"] for r in rows),
        "selfcheck_more": sum(r["selfcheck_more"] for r in rows),
        "capped": sum(r["rounds"] >= MAX_ROUNDS for r in rows),
        "fixed": sum(r["fixed"] for r in tried),
        "tried": len(tried),
        "fail": dict(Counter(r["status"] for r in rows if r["status"] != "ok")),
        "status": {r["id"]: r["status"] for r in rows},
    }


def spread(vals):
    """返回 (均值, 最小, 最大, 极差, 样本标准差)。极差是最直观的「噪声」度量。"""
    n = len(vals)
    if n == 0:
        return (0.0, 0.0, 0.0, 0.0, 0.0)
    mean = sum(vals) / n
    sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return (mean, min(vals), max(vals), max(vals) - min(vals), sd)


def print_single_report(rows, s):
    """保留单跑模式原来的输出格式，方便和之前的记录对照。"""
    print("\n" + "=" * 54)
    print("批量评测汇总")
    print("=" * 54)
    print(f"题目数      {s['n']}")
    print(f"执行准确率  {s['ok']}/{s['n']} = {s['acc']:.1%}")
    print(f"平均轮数    {s['avg_rounds']:.2f}（其中工具轮 {s['avg_tool_rounds']:.2f}）")
    print(f"工具调用率  {s['tool_cnt']}/{s['need']} = {s['tool_rate']:.1%}   (只算需要数据的题)"
          if s["need"] else "工具调用率  无样本")
    print(f"格式合规率  {s['fmt']:.1%}")
    print(f"被打回题数  {s['nudged']}/{s['n']}   (运行时检查触发)")
    if s["selfcheck_cnt"]:
        print(f"交卷前自评  {s['selfcheck_cnt']} 次（判「还缺」{s['selfcheck_more']} 次）")
    print(f"自修复率    {s['fixed']}/{s['tried']} = {s['fixed'] / s['tried']:.1%}"
          if s["tried"] else "自修复率    无样本（没有题触发 retry）")

    print("\n按题型分组：")
    for t in ("单表", "join", "口径", "计算", "越权"):
        grp = [r for r in rows if r["type"] == t]
        if grp:
            g_ok = sum(r["status"] == "ok" for r in grp)
            print(f"  {t:<5} {g_ok}/{len(grp)}")

    bad = Counter(r["status"] for r in rows if r["status"] != "ok")
    print("失败构成：" + ("、".join(f"{k} {v}" for k, v in bad.most_common()) or "无"))


def print_ab_report(records):
    """A/B 波动对比：差值必须大于组内波动，才有资格谈「改造有效」。"""
    b = [r for r in records if r["mode"] == "baseline"]
    a = [r for r in records if r["mode"] == "after"]
    if not b or not a:
        print("\n两组数据不齐，无法对比。")
        return

    print("\n" + "=" * 62)
    print(f"A/B 波动对比   基线 {len(b)} 遍 / 改造后 {len(a)} 遍 / 每遍 {b[0]['n']} 题"
          f"｜答题模型 {b[0].get('model', MODEL)}")
    dd = "/".join(sorted({("开" if r.get("dedup", True) else "关") for r in b + a}))
    sg = "/".join(sorted({("开" if r.get("sqlguard", True) else "关") for r in b + a}))
    sl = "/".join(sorted({("开" if r.get("schemalink") else "关") for r in b + a}))
    sf = "/".join(sorted({("开" if r.get("schemafixed") else "关") for r in b + a}))
    rl = "/".join(sorted({("开" if r.get("schemarel") else "关") for r in b + a}))
    rg = "/".join(sorted({("开" if r.get("schemarelgate") else "关") for r in b + a}))
    sc = "/".join(sorted({("开" if r.get("selfcheck") else "关") for r in b + a}))
    fn = "/".join(sorted({("开" if r.get("finalnorm") else "关") for r in b + a}))
    ra = "/".join(sorted({("开" if r.get("resultaudit") else "关") for r in b + a}))
    print(f"配置：变量={b[0].get('experiment', 'nudge')}｜重复SQL早停 {dd}"
          f"｜正文贴SQL打回 {sg}｜动态口径路由 {sl}｜固定8条对照 {sf}"
          f"｜表关系卡 {rl}｜意图门控 {rg}｜交卷前自评 {sc}"
          f"｜裸数字补前缀 {fn}｜返回值体检 {ra}")
    print("=" * 62)

    def line(name, key, pct=True, scale=100.0):
        bv = [r[key] for r in b if r[key] is not None]
        av = [r[key] for r in a if r[key] is not None]
        if not bv or not av:
            return None
        bm, blo, bhi, brng, bsd = spread(bv)
        am, alo, ahi, arng, asd = spread(av)
        unit = "%" if pct else ""
        if pct:
            bseq = " / ".join(f"{v * scale:.0f}{unit}" for v in bv)
            aseq = " / ".join(f"{v * scale:.0f}{unit}" for v in av)
            print(f"{name}\n  基线    {bseq}   均值 {bm * scale:.1f}{unit}  极差 {brng * scale:.1f}pp  标准差 {bsd * scale:.1f}pp")
            print(f"  改造后  {aseq}   均值 {am * scale:.1f}{unit}  极差 {arng * scale:.1f}pp  标准差 {asd * scale:.1f}pp")
        else:
            print(f"{name}\n  基线    {'/'.join(f'{v:.2f}' for v in bv)}   均值 {bm:.2f}  极差 {brng:.2f}")
            print(f"  改造后  {'/'.join(f'{v:.2f}' for v in av)}   均值 {am:.2f}  极差 {arng:.2f}")
        return bm, am, brng, arng, bsd, asd

    acc = line("执行准确率", "acc")
    line("工具调用率", "tool_rate")
    line("平均轮数", "avg_rounds", pct=False)
    line("SQL 执行失败次数（合计）", "sql_fail", pct=False)

    print("\n" + "-" * 62)
    if acc:
        bm, am, brng, arng, _, _ = acc
        delta = (am - bm) * 100
        noise = max(brng, arng) * 100
        print(f"准确率均值差  {delta:+.1f}pp")
        print(f"组内最大波动  {noise:.1f}pp   ← 同一配置自己就会跳这么多")
        if min(len(b), len(a)) < 2:
            print("判定：**只跑了 1 遍，波动根本没测出来**，极差恒为 0，不能下任何结论。")
            print("      请用 --repeat 2 或 3 重跑。")
        elif abs(delta) <= noise:
            print("判定：差值 ≤ 组内波动 → **信号淹没在噪声里，不能归因于改造**")
        else:
            print("判定：差值 > 组内波动 → 方向可能真实，但样本仅几遍，不等于因果证明")
        print("说明：极差是最保守的噪声估计；样本 2–3 遍时标准差参考价值有限。")

    # 逐题稳定性：同一配置下 status 变过的题，答案主要由抽样决定
    print("\n逐题稳定性（同一配置的不同遍之间，判定发生变化的题）：")
    for label, group in (("基线", b), ("改造后", a)):
        ids = sorted({k for r in group for k in r["status"]})
        unstable = [i for i in ids if len({r["status"].get(i) for r in group}) > 1]
        print(f"  {label:<4} {len(unstable)}/{len(ids)} 题不稳定" +
              (f"：{' '.join(unstable)}" if unstable else ""))

    print("\n提醒：就算两组均值差很大，也要看上面的「不稳定题」——")
    print("      它们证明了分数里有多少来自模型当次的运气。")
    print(f"\n本次逐遍指标已追加到 {RUNS_SUMMARY}")


# ---------------------------------------------------------------- 跑一批
def run_batch(cases, mode, trace_name, *, baseline_nudge=None, derive=False,
              dedup=None, sqlguard=None, schemalink=None, schemafixed=None,
              schemarel=None, schemarelgate=None, selfcheck=None, finalnorm=None,
              resultaudit=None):
    """跑完整一套题。所有开关都由主流程显式决定，避免 mode 的含义被混用。

    实验一 --ab        ：baseline_nudge 随 mode 变，derive 恒 False（保历史可比）
    实验二 --ab-derive ：nudge 固定为改造 B，derive 随 mode 变（严格单变量）
    实验三 --ab-dedup  ：nudge 固定为改造 B，derive 恒 False，dedup 随 mode 变（严格单变量）
    实验五 --ab-schema ：12 条平铺 vs 按题路由 8 条（量「整套改造」）
    实验六 --ab-fixed  ：固定 8 条 vs 按题路由 8 条（只量「路由」这一动作）
    实验七 --ab-rel    ：两组都开 8 条按题路由，唯一变量是「表关系卡」
    实验八 --ab-rel-gate：无卡 vs 门控卡，未命中意图的题两组提示词逐字节相同
    实验九 --ab-selfcheck：两组都不发卡，唯一变量是「交卷前自评」这一流程节点
    实验十 --ab-finalnorm：两组都开闸门，唯一变量是「裸数字补 FINAL: 前缀」
    实验十一 --ab-resultaudit：两组配置完全相同，唯一变量是「工具返回值体检」
    """
    global BASELINE, DERIVE, DEDUP, SQL_GUARD, SCHEMA_LINK, SCHEMA_FIXED, TRACE_FILE
    global SCHEMA_REL, SCHEMA_REL_GATE, SELF_CHECK, FINAL_NORMALIZE, RESULT_AUDIT
    BASELINE = (mode == "baseline") if baseline_nudge is None else baseline_nudge
    if schemalink is not None:
        SCHEMA_LINK = schemalink
    if schemafixed is not None:
        SCHEMA_FIXED = schemafixed
    if schemarel is not None:
        SCHEMA_REL = schemarel
    if schemarelgate is not None:
        SCHEMA_REL_GATE = schemarelgate
    if selfcheck is not None:
        SELF_CHECK = selfcheck
    if finalnorm is not None:
        FINAL_NORMALIZE = finalnorm
    if resultaudit is not None:
        RESULT_AUDIT = resultaudit
    DERIVE = derive
    if dedup is not None:
        DEDUP = dedup
    if sqlguard is not None:
        SQL_GUARD = sqlguard
    TRACE_FILE = trace_name
    open(TRACE_FILE, "w", encoding="utf-8").close()

    rows = []
    for case in cases:
        sys_txt = build_system(case["q"])
        print(f"\n--- {case['id']} [{case['type']}]  {case['q']}")
        if SCHEMA_LINK:
            print(f"    [口径路由] {retrieve(case['q'])['rule_ids']}")
        elif SCHEMA_FIXED:
            print(f"    [固定口径] {retrieve_fixed(case['q'])['rule_ids']}")
        if SCHEMA_REL:
            print(f"    [表关系卡] {relation_ids(case['q'], gated=SCHEMA_REL_GATE)}")
        if SELF_CHECK:
            print("    [收口闸门] 交卷前自评：开")
        answer, trace = run_case(case, system=sys_txt)
        status, f = judge(case, answer, trace)

        retried = any(t["kind"] == "retry" for t in trace)
        used_tool = any(t["tool"] == "run_sql" and t["ok"] for t in trace)
        nudged = any(t["kind"] == "nudge" for t in trace)
        # 自评是一次额外的复核请求，不算 Agent 的「轮」，否则 A/B 两组的
        # 平均轮数 / 满 6 轮会被实验组凭空多记一轮，机制指标就不可比了。
        # 只扣掉 selfcheck 这一个 kind：没开自评时口径与历史逐字节一致。
        n_sc = sum(t["kind"] == "selfcheck" for t in trace)
        rounds = len(trace) - n_sc
        tool_rounds = sum(t["kind"] != "finalize" for t in trace) - n_sc
        rows.append({
            "id": case["id"],
            "type": case["type"],
            "status": status,
            "rounds": rounds,
            "tool_rounds": tool_rounds,
            "retried": retried,
            "used_tool": used_tool,
            "nudged": nudged,
            "needs_tool": case["expect"] != "拒绝",
            "fixed": retried and status == "ok",
            "sql_fail": sum(1 for t in trace if t["tool"] == "run_sql" and not t["ok"]),
            "dup": sum(1 for t in trace if t["kind"] == "dup"),
            "sqltext": sum(1 for t in trace if t["kind"] == "sqltext"),
            "selfcheck": sum(1 for t in trace if t["kind"] == "selfcheck"),
            "selfcheck_more": sum(1 for t in trace if t["kind"] == "selfcheck" and t["ok"]),
        })

        print(f"    轮数 {rounds}（工具轮 {tool_rounds}）｜查库 {used_tool}｜"
              f"被打回 {nudged}｜FINAL={f}｜期望 {case['expect']}｜判定 {status}")
        if status in ("noformat", "no_tool", "manual"):
            print(f"    原始回答：{(answer or '')[:120]}")
        if status == "wrong":
            hit = decoy_hit(case, f)
            if hit:
                print(f"    ↳ 命中典型错值：{hit}")
    return rows, summarize(rows)


def parse_args(argv):
    global AB, AB_DERIVE, AB_DEDUP, AB_E, AB_SCHEMA, AB_FIXED, AB_REL, REPEAT, SINGLE_BASELINE
    global AB_REL_GATE, AB_SELFCHECK, AB_FINALNORM, AB_RESULTAUDIT
    global SINGLE_DERIVE, DEDUP, SQL_GUARD
    global SCHEMA_LINK, SCHEMA_FIXED, SCHEMA_REL, SCHEMA_REL_GATE, SELF_CHECK
    global FINAL_NORMALIZE, RESULT_AUDIT
    ids, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--ab":
            AB = True
        elif a == "--ab-derive":
            AB = True
            AB_DERIVE = True
        elif a == "--ab-dedup":
            AB = True
            AB_DEDUP = True
        elif a == "--ab-e":
            AB = True
            AB_E = True
        elif a == "--ab-schema":
            AB = True
            AB_SCHEMA = True
        elif a == "--ab-fixed":
            AB = True
            AB_FIXED = True
        elif a == "--ab-rel":
            AB = True
            AB_REL = True
        elif a == "--ab-rel-gate":
            AB = True
            AB_REL_GATE = True
        elif a == "--ab-selfcheck":
            AB = True
            AB_SELFCHECK = True
        elif a == "--ab-finalnorm":
            AB = True
            AB_FINALNORM = True
        elif a == "--ab-resultaudit":
            AB = True
            AB_RESULTAUDIT = True
        elif a == "--resultaudit":
            RESULT_AUDIT = True
        elif a == "--no-resultaudit":
            RESULT_AUDIT = False
        elif a == "--finalnorm":
            FINAL_NORMALIZE = True
        elif a == "--no-finalnorm":
            FINAL_NORMALIZE = False
        elif a == "--selfcheck":
            SELF_CHECK = True
        elif a == "--no-selfcheck":
            SELF_CHECK = False
        elif a == "--baseline":
            SINGLE_BASELINE = True
        elif a == "--derive":
            SINGLE_DERIVE = True
        elif a == "--no-dedup":
            DEDUP = False          # 复现 11-09 之前的旧行为（重复 SQL 照样重跑）
        elif a == "--dedup":
            DEDUP = True
        elif a == "--no-sqlguard":
            SQL_GUARD = False      # 复现改造 E 之前的旧行为
        elif a == "--sqlguard":
            SQL_GUARD = True
        elif a == "--schemalink":
            SCHEMA_LINK = True
        elif a == "--no-schemalink":
            SCHEMA_LINK = False
        elif a == "--repeat" and i + 1 < len(argv):
            try:
                REPEAT = int(argv[i + 1])
            except ValueError:
                sys.exit(f"--repeat 后面要跟整数，收到：{argv[i + 1]}")
            i += 1
        elif a.startswith("--"):
            sys.exit(f"未知参数：{a}")
        else:
            ids.append(a)
        i += 1
    if not 1 <= REPEAT <= 10:
        sys.exit("--repeat 只接受 1–10")
    if AB and (SINGLE_BASELINE or SINGLE_DERIVE):
        sys.exit("--ab / --ab-derive / --ab-dedup / --ab-e / --ab-schema / --ab-fixed "
                 "/ --ab-rel / --ab-rel-gate / --ab-selfcheck / --ab-finalnorm "
                 "/ --ab-resultaudit 不能和 --baseline / --derive 一起用")
    return ids or None


# ---------------------------------------------------------------- 主流程
if __name__ == "__main__":
    ONLY = parse_args(sys.argv[1:])
    cases = [c for c in CASES if c["id"] in ONLY] if ONLY else CASES
    if not cases:
        sys.exit(f"没有匹配的题号：{ONLY}；可用题号：" + ", ".join(c["id"] for c in CASES))

    print(f"答题模型 {MODEL}｜重复SQL早停 {'开' if DEDUP else '关'}"
          f"（连续撞同一句 {DUP_HIT_LIMIT} 次即收口）｜交卷前自评 {'开' if SELF_CHECK else '关'}"
          f"｜返回值体检 {'开' if RESULT_AUDIT else '关'}")

    if not AB:
        # 单跑：默认 DERIVE=False，与 11-08 历史记录逐字节一致
        mode = "baseline" if SINGLE_BASELINE else "after"
        if SINGLE_DERIVE:
            default_name = "trace_derive_single.jsonl"
        elif DEDUP and not SINGLE_BASELINE:
            default_name = "trace_dedup_single.jsonl"   # 别再覆盖 trace_shop_after.jsonl 那份存档
        else:
            default_name = ("trace_shop_baseline_rerun.jsonl" if SINGLE_BASELINE
                            else "trace_shop_after.jsonl")
        rows, s = run_batch(cases, mode, default_name, derive=SINGLE_DERIVE)
        if SINGLE_DERIVE:
            print("改造 C 已开启：相邻两次单值查询会附带派生值（本次不与历史存档直接对比）")
        print_single_report(rows, s)
        print(f"\n逐轮记录已写入 {TRACE_FILE}")
    else:
        # A/B 交替：一遍基线、一遍改造后，交替 REPEAT 轮
        partial = len(cases) != len(CASES)
        run_id = time.strftime("%H%M%S")
        exp = ("derive" if AB_DERIVE else "dedup" if AB_DEDUP
               else "sqlguard" if AB_E else "schemalink" if AB_SCHEMA
               else "fixed8" if AB_FIXED else "relation" if AB_REL
               else "relgate" if AB_REL_GATE
               else "selfcheck" if AB_SELFCHECK
               else "finalnorm" if AB_FINALNORM
               else "resultaudit" if AB_RESULTAUDIT else "nudge")
        print(f"A/B 交替实验 {run_id} · 变量={exp}：基线 × {REPEAT} 遍 + 改造后 × {REPEAT} 遍，"
              f"共 {REPEAT * 2 * len(cases)} 题")
        if AB_DERIVE:
            print("实验二（改造 C）：两组 nudge 都固定为改造 B，唯一变量是工具派生值开关。")
        elif AB_DEDUP:
            print("实验三（改造 D）：两组 nudge 都固定为改造 B、都不开派生值，唯一变量是重复 SQL 早停。")
        elif AB_E:
            print("实验四（改造 E）：两组 nudge 都固定为改造 B，其余开关一致，唯一变量是「正文贴 SQL 打回」。")
        elif AB_SCHEMA:
            print("实验五（改造 F）：表结构冻结，两组其余开关一致，唯一变量是「动态口径路由」。")
        elif AB_FIXED:
            print("实验六（归因）：两组都是 5 常驻 + 3 补充、共 8 条、同样的两段标题；")
            print("  基线固定给最常见的 3 条，实验组按题挑 3 条 —— 唯一变量是「路由」这个动作。")
        elif AB_RESULTAUDIT:
            print("实验十一（工具返回值设计）：两组配置完全相同、提示词逐字节相同；")
            print("  唯一变量是 run_sql 在返回空结果 / NULL / 整数除法时多给一行体检提示。")
            print("  提示由代码生成，不算数、不给答案、不改 SQL；未触发的题两组完全一致（阴性对照）。")
        elif AB_FINALNORM:
            print("实验十（收口解析层容错）：两组都开收口闸门、提示词完全相同；")
            print("  唯一变量是代码在 finalize 后把「裸数字」补成 FINAL: <数字>，")
            print("  修的是模型算对数字却漏写前缀被判 noformat 的尺子问题。")
        elif AB_SELFCHECK:
            print("实验九（Agent 本体 · 收口闸门）：两组上下文逐字节相同（都不发卡、都用 12 条平铺）；")
            print("  唯一变量是流程节点：模型要交卷时，实验组先让它自评「题目里的限定条件")
            print("  在已执行的 SQL 里是否都体现了」，判 MORE 才多给一轮补查，最多一次。")
        elif AB_REL_GATE:
            print("实验八（意图门控）：两组都开「8 条按题路由」，基线无卡；")
            print("  实验组只在题面命中「卖出 / 大类 / 子类目」等跨表意图时才发卡，")
            print("  其余题两组提示词逐字节相同（阴性对照），验证是否消除实验七的负作用。")
        elif AB_REL:
            print("实验七（语义映射）：两组都开「8 条按题路由」，唯一变量是 2 张按题挑的「表关系卡」；")
            print("  关系卡只讲字段在哪张表、要 JOIN 谁，不重复业务口径，也不含答案。")
        else:
            print("实验一（改造 B）：两组都不开派生值，配置与 11-08 历史一致。")
            print("⚠ 题库已扩到 50 题：绝对数字不可与 20 题时代直接相减，只比本次 A/B 内部的差值。")
        print("两组交替进行，避免时间/服务状态漂移混进差值。")
        if partial:
            print(f"⚠ 只选了 {len(cases)} 题 —— 这是验通路，样本不足，数字不能当结论。\n")
        else:
            print()

        records = []
        for k in range(1, REPEAT + 1):
            for mode in ("baseline", "after"):
                print(f"\n{'#' * 62}\n# 第 {k}/{REPEAT} 遍 · {mode} · {time.strftime('%H:%M:%S')}\n{'#' * 62}")
                prefix = ("trace_derive" if AB_DERIVE
                          else "trace_dedupab" if AB_DEDUP
                          else "trace_e" if AB_E
                          else "trace_fx" if AB_FIXED
                          else "trace_rg" if AB_REL_GATE
                          else "trace_rel" if AB_REL
                          else "trace_sc" if AB_SELFCHECK
                          else "trace_fn" if AB_FINALNORM
                          else "trace_ra" if AB_RESULTAUDIT
                          else "trace_sl" if AB_SCHEMA else "trace_ab")
                name = f"{prefix}_{run_id}_{k}_{mode}.jsonl"
                dd, sg, sl, sf, rl, rg, sc, fn, ra_ = None, None, None, False, False, False, False, False, False
                if AB_RESULTAUDIT:
                    # 两组都不发卡、都不自评、都不补前缀（与实验九/十之前的历史行为一致），
                    # 唯一变量：工具返回值末尾那一行体检提示
                    bn, dv = False, False
                    ra_ = (mode == "after")
                elif AB_FINALNORM:
                    # 两组都开闸门，唯一变量：裸数字要不要补 FINAL: 前缀
                    bn, dv, sc = False, False, True
                    fn = (mode == "after")
                elif AB_SELFCHECK:
                    # 两组都不发卡、都走 12 条平铺（提示词逐字节相同），
                    # 唯一变量：交卷前有没有那一次自评 + 判 MORE 后的一轮补查
                    bn, dv = False, False
                    sc = (mode == "after")
                elif AB_REL_GATE:
                    # 两组都是「8 条按题路由」；基线无卡，实验组只在跨表意图命中时发卡
                    bn, dv, sl = False, False, True
                    rl, rg = (mode == "after"), True
                elif AB_REL:
                    # 两组都是「8 条按题路由」，唯一变量：按题给 2 张表关系卡
                    bn, dv, sl = False, False, True
                    rl = (mode == "after")
                elif AB_FIXED:
                    # 基线 = 固定 8 条，改造后 = 按题路由 8 条；条数/结构/标题全同
                    bn, dv = False, False
                    sf, sl = (mode == "baseline"), (mode == "after")
                elif AB_SCHEMA:
                    bn, dv, sl = False, False, (mode == "after")  # 唯一变量：口径路由
                elif AB_E:
                    bn, dv, sg = False, False, (mode == "after")   # 唯一变量：正文贴SQL 打回
                elif AB_DERIVE:
                    bn, dv = False, (mode == "after")   # 唯一变量：派生值
                elif AB_DEDUP:
                    bn, dv, dd = False, False, (mode == "after")  # 唯一变量：早停
                else:
                    bn, dv = (mode == "baseline"), False  # 唯一变量：nudge
                rows, s = run_batch(cases, mode, name, baseline_nudge=bn, derive=dv,
                                    dedup=dd, sqlguard=sg, schemalink=sl,
                                    schemafixed=sf, schemarel=rl, schemarelgate=rg,
                                    selfcheck=sc, finalnorm=fn, resultaudit=ra_)
                rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "run_id": run_id,
                       "repeat": k, "mode": mode, "experiment": exp,
                       "model": MODEL,          # 必记：不同模型的数据绝不能混在一张表里比
                       "dedup": DEDUP,          # 同理：改造 D 开没开也必须记
                       "sqlguard": SQL_GUARD,   # 改造 E 在本次是变量还是固定值，必须查得出来
                       "schemalink": SCHEMA_LINK,  # 改造 F 同理
                       "schemafixed": SCHEMA_FIXED,  # 实验六：哪一组是「固定 8 条」
                       "schemarel": SCHEMA_REL,     # 实验七：哪一组带表关系卡
                       "schemarelgate": SCHEMA_REL_GATE,  # 实验八：关系卡是否走门控版
                       "selfcheck": SELF_CHECK,   # 实验九：哪一组有收口闸门
                       "finalnorm": FINAL_NORMALIZE,  # 实验十：哪一组补裸数字前缀
                       "resultaudit": RESULT_AUDIT,   # 实验十一：哪一组有返回值体检
                       "trace": name, **s}
                records.append(rec)
                # 每遍结束立刻追加，中途断了已完成的遍不丢；验通路不写，免得污染正式汇总
                if not partial:
                    with open(RUNS_SUMMARY, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"\n  >> 第 {k} 遍 {mode}：准确率 {s['ok']}/{s['n']} = {s['acc']:.1%}"
                      f"｜工具调用率 {s['tool_cnt']}/{s['need']}"
                      f"｜SQL 失败 {s['sql_fail']} 次｜重复SQL {s['dup_cnt']} 次"
                      f"｜正文贴SQL {s['sqltext_cnt']} 次｜满 6 轮 {s['capped']} 题"
                      f"｜自评 {s['selfcheck_cnt']} 次(判缺 {s['selfcheck_more']})")

        print_ab_report(records)
