# -*- coding: utf-8 -*-
"""实验结果分层分析（0 次 API，只读 runs_summary.jsonl + trace）。

跑法：
  python 10_analyze.py selfcheck        # 默认取该实验最后一次 run_id
  python 10_analyze.py selfcheck 140000  # 指定 run_id
  python 10_analyze.py relgate           # 其他实验同样可用

做三件事：
  1. 两遍配对差值 + 组内极差 + 逐题不稳定数（第 26 节教训：只看总分极差会低估噪声）
  2. 按「被改动 / 未被改动」分层，看效应是不是落在它该落的题上
  3. 从 trace 里找机制证据（本题专属：自评判缺后有没有补上 status 过滤）
"""
import glob
import json
import re
import sys
from collections import Counter

import schema_link  # noqa: F401  - 仅为让脚本与评测脚本同目录可独立运行

EXP = sys.argv[1] if len(sys.argv) > 1 else "selfcheck"
WANT_ID = sys.argv[2] if len(sys.argv) > 2 else None

rows = [json.loads(l) for l in open("runs_summary.jsonl", encoding="utf-8")]
rows = [r for r in rows if r.get("experiment") == EXP]
if not rows:
    sys.exit(f"runs_summary.jsonl 里没有 experiment={EXP} 的记录")
run_ids = sorted({r["run_id"] for r in rows})
rid = WANT_ID or run_ids[-1]
rows = [r for r in rows if r["run_id"] == rid]
print(f"实验 {EXP}｜run_id={rid}｜可选 {run_ids}｜记录 {len(rows)} 行")

by = {(r["repeat"], r["mode"]): r for r in rows}
reps = sorted({r["repeat"] for r in rows})
b = [by[(i, "baseline")]["acc"] for i in reps]
a = [by[(i, "after")]["acc"] for i in reps]
print("\n== 总分 ==")
for i in reps:
    rb, ra = by[(i, "baseline")], by[(i, "after")]
    print(f"  第{i}遍  baseline {rb['ok']}/{rb['n']}={rb['acc']:.1%}"
          f"  after {ra['ok']}/{ra['n']}={ra['acc']:.1%}"
          f"  配对 {(ra['acc']-rb['acc'])*100:+.1f}pp")
print(f"  均值 {sum(b)/len(b)*100:.1f}% → {sum(a)/len(a)*100:.1f}%"
      f"  差 {(sum(a)-sum(b))/len(b)*100:+.1f}pp"
      f"  组内极差 baseline {(max(b)-min(b))*100:.1f}pp / after {(max(a)-min(a))*100:.1f}pp")
if abs((sum(a)-sum(b))/len(b)*100) <= max((max(b)-min(b))*100, (max(a)-min(a))*100):
    print("  判定：差值 ≤ 组内波动 → 不能归因于改造")

# ---- 分层：实验九的 18 道门控题 vs 32 道阴性对照题 ----
cases = json.load(open("cases.json", encoding="utf-8"))["cases"]
cases_by_id = {c["id"]: c for c in cases}
all_ids = [c["id"] for c in cases]
if EXP == "selfcheck":
    # 直接读评测脚本的门控函数，保证分层口径与实验时逐字节一致
    import importlib.util
    spec = importlib.util.spec_from_file_location("ev", "03b_batch_eval.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    fired = [c["id"] for c in cases if ev.selfcheck_needed(c["q"])]
    groups = [("门控命中（该被改动的题）", fired),
              ("阴性对照（提示词与流程全同）", [k for k in all_ids if k not in fired]),
              ("其中：稳定口径难题", [k for k in ("B2", "B5", "B8", "B13", "C1", "C2") if k in fired]),
              ("其中：非目标题（可能负作用）", [k for k in ("D2", "D4", "D6", "D7", "D9", "D10", "D11") if k in fired])]
elif EXP == "resultaudit":
    # 实验十一：触发过体检的题 vs 工具返回逐字节相同的阴性对照
    fired = set()
    for f in sorted(glob.glob(f"trace_ra_{rid}_*_after.jsonl")):
        for line in open(f, encoding="utf-8"):
            r = json.loads(line)
            if "[结果体检]" in str(r.get("output", "")):
                fired.add(r["q"])
    groups = [("触发体检（该被改动的题）", [k for k in all_ids if k in fired]),
              ("阴性对照（工具返回逐字节相同）", [k for k in all_ids if k not in fired])]
    print(f"  体检触发题：{sorted(fired)}")
else:
    groups = [("全部题", all_ids)]

print("\n== 分层（净增题次 = after 对 - baseline 对，合计） ==")
for label, ids in groups:
    if not ids:
        continue
    tb = ta = 0
    detail = []
    for i in reps:
        sb, sa = by[(i, "baseline")]["status"], by[(i, "after")]["status"]
        x, y = sum(sb[k] == "ok" for k in ids), sum(sa[k] == "ok" for k in ids)
        tb += x
        ta += y
        detail.append(f"{x}/{len(ids)}→{y}/{len(ids)}")
    print(f"  {label}（{len(ids)} 题）: {'  '.join(detail)}  合计 {tb}/{reps[-1]*len(ids)}→{ta}/{reps[-1]*len(ids)}"
          f"  净增 {ta-tb:+d} 题次")

print("\n== 逐题 GAIN / LOSS（跨遍合计） ==")
gain, loss = Counter(), Counter()
for i in reps:
    sb, sa = by[(i, "baseline")]["status"], by[(i, "after")]["status"]
    for k in all_ids:
        if sb[k] != "ok" and sa[k] == "ok":
            gain[k] += 1
        if sb[k] == "ok" and sa[k] != "ok":
            loss[k] += 1
print("  GAIN:", gain.most_common(15))
print("  LOSS:", loss.most_common(15))

print("\n== 机制指标 ==")
for mode in ("baseline", "after"):
    rs = [by[(i, mode)] for i in reps]
    keys = ("tool_rate", "avg_rounds", "capped", "sql_fail", "nudged",
            "sqltext_cnt", "dup_cnt", "selfcheck_cnt", "selfcheck_more")
    print(f"  {mode}: " + "  ".join(
        f"{k}={sum(r.get(k, 0) or 0 for r in rs)/len(rs):.2f}" for k in keys if rs[0].get(k) is not None))

# ---- 实验九专属：判缺之后，重写的 SQL 里到底有没有补上 status ----
if EXP == "selfcheck":
    print("\n== 自评判缺后的 SQL（看机制有没有真的生效） ==")
    STATUS = ("status", "paid", "shipped")
    for f in sorted(glob.glob(f"trace_sc_{rid}_*_after.jsonl")):
        recs = [json.loads(l) for l in open(f, encoding="utf-8")]
        byq = {}
        for r in recs:
            byq.setdefault(r["q"], []).append(r)
        for q, rs in byq.items():
            sc = [r for r in rs if r["kind"] == "selfcheck"]
            if not sc or not sc[0].get("ok"):
                continue
            idx = rs.index(sc[0])
            before = [r["args"].get("sql", "") for r in rs[:idx] if r["tool"] == "run_sql"]
            after = [r["args"].get("sql", "") for r in rs[idx + 1:] if r["tool"] == "run_sql"]
            fixed = any(all(w in sql.lower() for w in STATUS) for sql in after)
            print(f"  {f.split('_')[3]} {q:4} 判缺前SQL {len(before)} 条"
                  f"(含status {sum('status' in s.lower() for s in before)})"
                  f" → 判缺后 {len(after)} 条(含完整口径 {fixed})"
                  f"  {by[(int(f.split('_')[3]), 'after')]['status'][q]}")

# ---- 实验十一专属：体检提示之后，模型有没有真的改 SQL ----
if EXP == "resultaudit":
    print("\n== 体检触发后的下一轮 SQL（看机制有没有真的生效） ==")
    FIXWORDS = ("1.0", "cast(", "coalesce", "left join", "ifnull", "real")
    for f in sorted(glob.glob(f"trace_ra_{rid}_*_after.jsonl")):
        recs = [json.loads(l) for l in open(f, encoding="utf-8")]
        byq = {}
        for r in recs:
            byq.setdefault(r["q"], []).append(r)
        for q, rs in byq.items():
            hits = [i for i, r in enumerate(rs)
                    if "[结果体检]" in str(r.get("output", ""))]
            if not hits:
                continue
            i = hits[0]
            after = [r["args"].get("sql", "") for r in rs[i + 1:]
                     if r["tool"] == "run_sql" and r["args"].get("sql")]
            changed = [w for w in FIXWORDS if any(w in s.lower() for s in after)]
            st = by[(int(f.split("_")[3]), "after")]["status"][q]
            print(f"  {f.split('_')[3]} {q:4} 提示后重查 {len(after)} 次"
                  f"｜出现修正写法 {changed or '无'}｜最终 {st}")

# ---- 判分敏感性：两组都关 finalnorm 时，开尺子会不会改变方向（0 次 API） ----
# 第 31 节教训：noformat 里真正「算对被误判」的往往只有一两个，别把标签错当能力。
# 用法：python 10_analyze.py resultaudit [run_id] [log路径]
if EXP == "resultaudit":
    import importlib.util
    spec = importlib.util.spec_from_file_location("ev", "03b_batch_eval.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    logpath = sys.argv[3] if len(sys.argv) > 3 else "log_abRA_flash.txt"
    try:
        lines = open(logpath, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        print(f"\n== 判分敏感性：找不到 {logpath}，跳过 ==")
    else:
        segs, cur, buf = {}, None, []
        for l in lines:
            m = re.match(r"# 第 (\d+)/\d+ 遍 · (baseline|after)", l)
            if m:
                if cur:
                    segs[cur] = "\n".join(buf)
                cur, buf = (int(m.group(1)), m.group(2)), []
            elif cur:
                buf.append(l)
        if cur:
            segs[cur] = "\n".join(buf)

        print(f"\n== 判分敏感性（{logpath}，离线重放 normalize_final_text） ==")
        replay = {}
        for k, body in sorted(segs.items()):
            ok, gained, still = 0, [], 0
            for b in re.split(r"(?=--- \S+ \[)", body):
                v = re.search(r"判定 (\S+)", b)
                q = re.search(r"--- (\S+) \[", b)
                if not (v and q):
                    continue
                qid, status = q.group(1), v.group(1)
                if status == "ok":
                    ok += 1
                    continue
                if status != "noformat":
                    continue
                raw = re.search(r"原始回答：(.*)", b)
                if not raw:
                    still += 1
                    continue
                f = ev.final_of(ev.normalize_final_text(raw.group(1)))
                c = cases_by_id[qid]
                got, want = ev.to_number(f), ev.to_number(c["expect"])
                if f is None:
                    same = False
                elif got is not None and want is not None:
                    same = abs(got - want) <= 0.01
                else:
                    same = ev.norm(f) == ev.norm(str(c["expect"]))
                if (f is not None and same and "查库 True" in b and c["expect"] != "拒绝"
                        and not any(w in f for w in ("拒绝", "无法", "不能"))):
                    ok += 1
                    gained.append((qid, f))
                else:
                    still += 1
            replay[k] = ok - len(gained)
            print(f"  {k}: 实测 ok={ok - len(gained)} → 开尺子 ok={ok}"
                  f"  救回 {len(gained)} {gained}｜仍错 {still}")
        rb = [replay[(i, "baseline")] for i in reps if (i, "baseline") in replay]
        ra = [replay[(i, "after")] for i in reps if (i, "after") in replay]
        if rb and ra:
            d0 = ((sum(ra) / len(ra)) - (sum(rb) / len(rb))) * 2  # 每题 2pp
            d1 = ((sum(by[(i, "after")]["acc"] for i in reps) / len(reps))
                  - (sum(by[(i, "baseline")]["acc"] for i in reps) / len(reps))) * 100
            print(f"  原差值 {d1:+.1f}pp → 开尺子后 {d0:+.1f}pp"
                  f"（若符号翻转或幅度变化 > 2pp，结论依赖判分细节，必须标注）")
