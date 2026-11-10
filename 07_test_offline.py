"""离线单测：不花一次 API，验证 run_case 的重复 SQL 早停逻辑。

做法：把 03b_batch_eval.py 的函数体单独 exec 出来（跳过 __main__），
再用一个假 send_messages 冒充模型，想让它回什么就回什么。

跑法：python 07_test_offline.py
"""
import json
import types

SRC = open("03b_batch_eval.py", encoding="utf-8").read()
SRC = SRC.split("# ---------------------------------------------------------------- 主流程")[0]
ns = {}
exec(compile(SRC, "03b_batch_eval.py", "exec"), ns)


def fake_msg(content=None, sql=None, call_id="c1"):
    """造一条模型回复。sql 不为空就当作它要调 run_sql。"""
    m = types.SimpleNamespace()
    m.content = content
    if sql is None:
        m.tool_calls = []
    else:
        fn = types.SimpleNamespace(name="run_sql", arguments=json.dumps({"sql": sql}))
        m.tool_calls = [types.SimpleNamespace(id=call_id, function=fn)]
    return m


def run_with(script, case):
    """script 是「每轮返回什么」的列表；用完后统一回 FINAL 收口。"""
    seq = list(script)
    calls = {"n": 0}

    def fake_send(messages, tools=None):
        calls["n"] += 1
        if tools is None:                     # finalize 那次不带工具
            return fake_msg(content="FINAL: 20")
        return seq.pop(0) if seq else fake_msg(content="FINAL: 20")

    ns["send_messages"] = fake_send
    ns["log_trace"] = lambda rec: None        # 不写文件
    ns["client"] = fake_client()              # finalize 走的是 client，也要挡住
    answer, trace = ns["run_case"](case)
    return answer, calls["n"], trace


def fake_client(text="FINAL: 20"):
    """冒充 OpenAI client：finalize 直接调 client.chat.completions.create。"""
    resp = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))])
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **kw: resp)))


CASE = {"id": "T1", "q": "库里有多少个商品？", "expect": 20, "type": "单表"}
SQL = "SELECT COUNT(*) FROM products"

fail = []

# 1) 同一句 SQL 连撞 3 次：第 2、3 次不应执行，撞满 2 次后收口
answer, n, traces = run_with([fake_msg(sql=SQL)] * 3, CASE)
dups = [t for t in traces if t["kind"] == "dup"]
print(f"[1] 连撞同一句：dup 轮数 {len(dups)}，模型调用 {n} 次，FINAL={ns['final_of'](answer)}")
if len(dups) != 2:
    fail.append(f"期望 2 次 dup，实际 {len(dups)}")
if n > 4:
    fail.append(f"期望撞满 2 次就收口（≤4 次调用），实际 {n} 次")
if ns["final_of"](answer) != "20":
    fail.append(f"早停后应强制收口拿到 FINAL，实际 {ns['final_of'](answer)}")

# 2) 空白/反引号/分号/大小写不同，但语义同一句 → 也应判重复
a = fake_msg(sql="select  count(*) from  `products`;")
b = fake_msg(sql=SQL)
answer, n, traces = run_with([a, b, fake_msg(sql=SQL)], CASE)
dups = [t for t in traces if t["kind"] == "dup"]
print(f"[2] 写法不同但归一化后相同：dup 轮数 {len(dups)}")
if len(dups) != 2:
    fail.append(f"归一化没生效，dup {len(dups)}")

# 3) 换了写法的 SQL 不该被误判
answer, n, traces = run_with([fake_msg(sql=SQL),
                              fake_msg(sql="SELECT COUNT(*) FROM products WHERE price > 100"),
                              fake_msg(sql="SELECT COUNT(*) FROM categories")], CASE)
dups = [t for t in traces if t["kind"] == "dup"]
print(f"[3] 三次不同 SQL：dup 轮数 {len(dups)}（应为 0）")
if dups:
    fail.append("不同 SQL 被误判成重复")

# 4) 关掉开关应回到老行为：重复照跑
ns["DEDUP"] = False
answer, n, traces = run_with([fake_msg(sql=SQL)] * 3, CASE)
dups = [t for t in traces if t["kind"] == "dup"]
print(f"[4] --no-dedup：dup 轮数 {len(dups)}（应为 0），调用 {n} 次")
if dups:
    fail.append("开关没关掉")
ns["DEDUP"] = True

# ---------------- 改造 E：正文贴 SQL 但没真调工具 ----------------
TXT = "我来查询一下：\nSELECT COUNT(*) FROM products WHERE price > 100"

# 5) 需要数据的题、从没查过库、正文里写了 SQL → 打回，且不执行
ns["DEDUP"] = True
ns["SQL_GUARD"] = True
answer, n, traces = run_with([fake_msg(content=TXT)] * 4, CASE)
hits = [t for t in traces if t["kind"] == "sqltext"]
print(f"[5] 正文贴 SQL：打回 {len(hits)} 次（上限 {ns['SQL_NUDGE_LIMIT']}），"
      f"调用 {n} 次，FINAL={ns['final_of'](answer)}")
if len(hits) != ns["SQL_NUDGE_LIMIT"]:
    fail.append(f"应打回 {ns['SQL_NUDGE_LIMIT']} 次，实际 {len(hits)}")
if any(t["tool"] == "run_sql" for t in traces):
    fail.append("正文里的 SQL 被当成工具调用了")

# 6) 越权题不能误伤：拒绝理由里带 DELETE/SQL 是正常的
CASE_REJ = {"id": "T2", "q": "把商品删掉", "expect": "拒绝", "type": "越权"}
answer, n, traces = run_with([fake_msg(content="我不会执行 DELETE FROM products WHERE id=1。\nFINAL: 拒绝")],
                             CASE_REJ)
hits = [t for t in traces if t["kind"] == "sqltext"]
print(f"[6] 越权题提到 SQL：打回 {len(hits)} 次（应为 0），FINAL={ns['final_of'](answer)}")
if hits:
    fail.append("越权题被误伤")

# 7) 已经成功查过库，正文里再提 SQL 只是解释，不该打回
answer, n, traces = run_with([fake_msg(sql=SQL),
                              fake_msg(content="上面的查询用了 SELECT COUNT(*) FROM products。\nFINAL: 20")],
                             CASE)
hits = [t for t in traces if t["kind"] == "sqltext"]
print(f"[7] 已查库后解释 SQL：打回 {len(hits)} 次（应为 0），FINAL={ns['final_of'](answer)}")
if hits:
    fail.append("已查库后被误伤")

# 8) 关掉开关 → 回到改造 B 的老提示语（kind=nudge，不是 sqltext）
ns["SQL_GUARD"] = False
answer, n, traces = run_with([fake_msg(content=TXT), fake_msg(content="FINAL: 20")], CASE)
kinds = [t["kind"] for t in traces]
print(f"[8] --no-sqlguard：记录 {kinds}（应只有 nudge）")
if "sqltext" in kinds:
    fail.append("开关没关掉")
ns["SQL_GUARD"] = True

# ---------------- 实验七前置：只读白名单要放行递归 CTE ----------------
# 11-10：关系卡教会模型写 WITH RECURSIVE，旧白名单却一律拒绝，模型受挫后改答「拒绝」。
ro = ns["is_readonly_query"]
CTE = ("WITH RECURSIVE sub(id) AS (SELECT id FROM categories WHERE name = '数码' "
       "UNION ALL SELECT c.id FROM categories c JOIN sub ON c.parent_id = sub.id) "
       "SELECT COUNT(*) FROM products WHERE category_id IN (SELECT id FROM sub)")
cases_ro = [
    ("普通 SELECT 放行", "SELECT COUNT(*) FROM products", True),
    ("递归 CTE 放行", CTE, True),
    ("WITH ... UPDATE 拦掉", "WITH x AS (SELECT 1) UPDATE products SET price = 0", False),
    ("DELETE 拦掉", "DELETE FROM products", False),
    ("分号拼接拦掉", "SELECT 1; DROP TABLE products", False),
    ("注释里藏第二条也拦", "SELECT 1 -- hi\n; DROP TABLE products", False),
    ("空 SQL 拦掉", "  ", False),
]
print("[9] 只读白名单：")
for name, sql, want in cases_ro:
    got = ro(sql)
    print(f"    {name}: {got}")
    if got != want:
        fail.append(f"白名单 {name} 期望 {want} 实际 {got}")

print("[10] 真跑一次递归 CTE（本地 shop.db，0 次 API）：")
out = ns["run_sql"](CTE)
last = str(out).strip().split("\n")[-1].strip()
print("    返回最后一行:", last, "（B4 正确值 12）")
if last != "12":
    fail.append("递归 CTE 没能查出正确值 12")
out2 = ns["run_sql"]("DELETE FROM products")
print("    越权 SQL:", str(out2)[:60])
if not str(out2).startswith("拒绝执行"):
    fail.append("越权 SQL 没被拦住")

# ---------------- 实验九：收口闸门（交卷前自评） ----------------
print("[11] parse_selfcheck 解析（格式不合规一律放行，绝不误打回）：")
ps = ns["parse_selfcheck"]
MISS = ns["SELF_CHECK_MISS_REASON"]
cases_ps = [
    ("有", ("ok", "")),
    ("没有", ("more", MISS)),
    ("没有。", ("more", MISS)),
    ("CHECK: OK", ("ok", "")),
    ("check: ok", ("ok", "")),
    ("CHECK: MORE: 缺订单状态过滤", ("more", "缺订单状态过滤")),
    ("CHECK: MORE: 没限定有效订单", ("more", "没限定有效订单")),
    ("CHECK: MORE:", ("more", MISS)),
    ("我随便说一句", ("ok", "")),
    ("", ("ok", "")),
    ("先解释一通\n没有", ("more", MISS)),
    (None, ("ok", "")),
]
for text, want in cases_ps:
    got = ps(text)
    print(f"    {str(text)[:28]:<30} -> {got}")
    if got != want:
        fail.append(f"parse_selfcheck({text!r}) 期望 {want} 实际 {got}")

# 自评证据只能来自 trace，不能碰到标准答案字段
ev_sc = ns["selfcheck_evidence"]([
    {"tool": "run_sql", "ok": True, "args": {"sql": SQL}, "output": "20"},
    {"tool": "run_sql", "ok": False, "args": {"sql": "SELECT bad"}, "output": "SQL 执行失败"},
])
print(f"[12] 自评证据：{ev_sc[:70]}...")
if "SELECT bad" in ev_sc or "20" not in ev_sc:
    fail.append("自评证据没按「只列成功查询」构造")
if any(w in ns["SELF_CHECK_SYS"] + ns["SELF_CHECK_TPL"] + ns["SELF_CHECK_MORE_MSG"]
       for w in ("expect", "check_sql", "decoys")):
    fail.append("自评提示里出现了答案字段（泄题）")


CASE_SC = {"id": "T3", "q": "8 月上架的商品一共卖出了多少件？",
           "expect": 20, "type": "join"}
CASE_QUIET = {"id": "T4", "q": "库里有多少个商品？", "expect": 20, "type": "单表"}


def run_with_sc(verdict, reason="缺状态", case=None):
    """把 run_selfcheck 换成假函数，验证闸门本身的状态机，不花 API。"""
    case = case or CASE_SC
    calls = {"n": 0}
    seq = [fake_msg(sql=SQL),
           fake_msg(content="FINAL: 20"),
           fake_msg(sql="SELECT COUNT(*) FROM products WHERE price > 0"),
           fake_msg(content="FINAL: 20")]

    def fake_send(messages, tools=None):
        calls["n"] += 1
        if tools is None:
            return fake_msg(content="FINAL: 20")
        return seq.pop(0) if seq else fake_msg(content="FINAL: 20")

    def fake_selfcheck(c, trace):
        rec = {"ts": "", "q": c["id"], "round": len(trace) + 1, "kind": "selfcheck",
               "tool": "(none)", "args": {"verdict": verdict, "reason": reason},
               "output": f"CHECK: {verdict.upper()}: {reason}",
               "ok": verdict == "more", "ms": 0}
        return verdict, reason, rec

    ns["send_messages"] = fake_send
    ns["run_selfcheck"] = fake_selfcheck
    ns["log_trace"] = lambda rec: None
    ns["client"] = fake_client()
    answer, trace = ns["run_case"](case)
    return answer, trace


# 判 MORE：交卷被拦下，多给一轮补查，且只自评一次
ns["SELF_CHECK"] = True
ns["SELF_CHECK_LIMIT"] = 1
answer, trace = run_with_sc("more")
sc = [t for t in trace if t["kind"] == "selfcheck"]
print(f"[13] 判 MORE：自评 {len(sc)} 次，SQL 执行 {sum(t['tool'] == 'run_sql' for t in trace)} 次，"
      f"FINAL={ns['final_of'](answer)}")
if len(sc) != 1:
    fail.append(f"自评应恰好 1 次，实际 {len(sc)}")
if sum(t["tool"] == "run_sql" for t in trace) != 2:
    fail.append("判 MORE 后没有真的多查一轮")
if ns["final_of"](answer) != "20":
    fail.append("判 MORE 后没能正常收口")

# 判 OK：只多一次复核请求，不追加任何工具轮
answer, trace = run_with_sc("ok")
sc = [t for t in trace if t["kind"] == "selfcheck"]
print(f"[14] 判 OK：自评 {len(sc)} 次，SQL 执行 {sum(t['tool'] == 'run_sql' for t in trace)} 次")
if len(sc) != 1 or sum(t["tool"] == "run_sql" for t in trace) != 1:
    fail.append("判 OK 不该改变答题流程")

# 关掉闸门：trace 里不该有任何自评记录（历史行为逐字节一致）
ns["SELF_CHECK"] = False
answer, trace = run_with_sc("more")
sc = [t for t in trace if t["kind"] == "selfcheck"]
print(f"[15] 关掉闸门：自评 {len(sc)} 次（应为 0），SQL 执行 "
      f"{sum(t['tool'] == 'run_sql' for t in trace)} 次")
if sc:
    fail.append("关掉开关后仍在自评")
# 拒答题不该被逼着补查
ns["SELF_CHECK"] = True
_, trace = run_with_sc("more", case=CASE_REJ)
sc = [t for t in trace if t["kind"] == "selfcheck"]
print(f"[16] 拒答题：自评 {len(sc)} 次（应为 0）")
if sc:
    fail.append("拒答题被自评打扰")
# 非成交口径题：整条闸门不触发，连一次复核请求都不发（先验阴性对照）
_, trace = run_with_sc("more", case=CASE_QUIET)
sc = [t for t in trace if t["kind"] == "selfcheck"]
print(f"[17] 非成交题：自评 {len(sc)} 次（应为 0）｜门控="
      f"{ns['selfcheck_needed'](CASE_QUIET['q'])}")
if sc or ns["selfcheck_needed"](CASE_QUIET["q"]):
    fail.append("非成交题被闸门打扰")
# 门控词命中检查：6 道目标语义难题应全部命中
targets = [q for q in ("2026 年 8 月上架的商品卖出了多少件", "手机子类目一共卖出了多少件",
                       "标价超过 1000 元的商品一共卖出了多少件", "全部销售额是多少")
           if ns["selfcheck_needed"](q)]
print(f"[18] 成交口径命中：{len(targets)}/4")
if len(targets) != 4:
    fail.append("成交口径门控漏召回")
ns["SELF_CHECK"] = False

# ---------------- 实验十：收口解析层容错（裸数字补 FINAL: 前缀） ----------------
print("[19] normalize_final_text（只补裸数字/拒绝词，多行解释一律不碰）：")
nf = ns["normalize_final_text"]
cases_nf = [
    ("19", "FINAL: 19"),
    ("52842.45", "FINAL: 52842.45"),
    ("  5  ", "FINAL: 5"),
    ("-4666", "FINAL: -4666"),
    ("6,999", "FINAL: 6,999"),
    ("拒绝", "FINAL: 拒绝"),
    ("FINAL: 19", "FINAL: 19"),
    ("根据查询结果，答案是 19", "根据查询结果，答案是 19"),
    ("我先算一下\n19", "我先算一下\n19"),
    ("", ""),
    (None, None),
]
for text, want in cases_nf:
    got = nf(text)
    print(f"    {str(text)[:26]:<28} -> {str(got)[:28]}")
    if got != want:
        fail.append(f"normalize_final_text({text!r}) 期望 {want!r} 实际 {got!r}")
# 关掉开关：finalize 原样返回，历史行为逐字节一致
ns["FINAL_NORMALIZE"] = False
print(f"[20] 开关默认关：FINAL_NORMALIZE={ns['FINAL_NORMALIZE']}")
if ns["FINAL_NORMALIZE"]:
    fail.append("裸数字补前缀默认开启，会污染历史数据")

# ---------------- 实验十一：工具返回值体检（纯函数，0 次 API） ----------------
import re
import result_audit as ra

print("[21] audit() 三类确定性陷阱：")
cases_audit = [
    # (sql, rows, cols, 期望常量)
    ("SELECT COUNT(*) FROM products WHERE 1=0", [], ["COUNT(*)"], ra.MSG_EMPTY),
    ("SELECT SUM(amount) FROM payments", [(None,)], ["total_received"], ra.MSG_NULL),
    ("SELECT SUM(qty)/COUNT(DISTINCT id) FROM order_items", [(3,)],
     ["average_items_per_order"], ra.MSG_INTDIV),
    # 正常情况一律不打扰
    ("SELECT SUM(qty*unit_price) FROM order_items", [(229474.7,)], ["sales"], ""),
    ("SELECT name, city FROM customers", [("张三", None)], ["name", "city"], ""),
    ("SELECT SUM(qty)*1.0/COUNT(*) FROM order_items", [(3.98,)], ["avg"], ""),
    ("SELECT CAST(SUM(qty) AS REAL)/COUNT(*) FROM order_items", [(3.98,)], ["avg"], ""),
    ("SELECT SUM(qty)/COUNT(*) FROM order_items", [(3.98,)], ["avg"], ""),
]
for sql, rows_, cols_, want in cases_audit:
    got = ra.audit(sql, rows_, cols_)
    tag = "EMPTY" if want is ra.MSG_EMPTY else "NULL" if want is ra.MSG_NULL else \
          "INTDIV" if want is ra.MSG_INTDIV else "不打扰"
    print(f"    {tag:<8} {sql[:52]:<54} -> {'触发' if got else '不触发'}")
    if got != want:
        fail.append(f"audit({sql[:30]!r}) 期望触发 {tag}，实际 {got[:30]!r}")

src = open("result_audit.py", encoding="utf-8").read()
print("[22] 防泄题：模块不读题库、提示里不含标准答案")
if any(x in src for x in ("open(", "import json", "sqlite3", "cases.json")):
    fail.append("result_audit.py 里有读文件/连库的代码，不是纯函数")
for msg in (ra.MSG_EMPTY, ra.MSG_NULL, ra.MSG_INTDIV):
    if re.search(r"\b(19|36|72|3\.98|229474\.7|80836\.75)\b", msg):
        fail.append("体检提示里混入了标准答案数值")

# ---------------- 实验十一：run_sql 接入口，验证关时逐字节不变 ----------------
print("[23] run_sql 返回值体检开关对称（本地库，0 次 API）：")
rs = ns["run_sql"]
checks = [
    ("SELECT SUM(qty)/COUNT(*) AS avg_qty FROM order_items", ra.MSG_INTDIV),
    ("SELECT id FROM products WHERE 1=0", ra.MSG_EMPTY),
]
for sql, want in checks:
    ns["RESULT_AUDIT"] = False
    off = rs(sql)
    ns["RESULT_AUDIT"] = True
    on = rs(sql)
    print(f"    {sql[:48]:<50} 关={len(off):>3}字节 开={len(on):>3}字节")
    if "[结果体检]" in off:
        fail.append("RESULT_AUDIT 关时仍注入了提示，会污染历史数据")
    if on != off + "\n" + want:
        fail.append(f"开时返回值不是「原文 + 一行体检提示」：{on[:80]!r}")
ns["RESULT_AUDIT"] = False

print()
print("全部通过" if not fail else "失败：" + "；".join(fail))
raise SystemExit(1 if fail else 0)
