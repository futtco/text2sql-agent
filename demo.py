#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
项目演示（离线回放真实 trace，0 次 API、0 花费、不改任何文件）

    python demo.py              # 连放三道真题：一次答对、一次悄悄答错、一次假修复
    python demo.py B5           # 只看某一道题的完整过程
    python demo.py --list       # 列出所有能回放的题号

真想现场跑模型（需要 .env 里的 key，会花一点点额度）：
    python 03b_batch_eval.py A1
"""
import glob
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
EVIDENCE = "evidence"          # 全部历史 trace 统一放在这个目录
BY_ID = {c["id"]: c for c in json.load(
    open(os.path.join(BASE, "cases.json"), encoding="utf-8"))["cases"]}


def _path(name):
    """证据文件优先在 evidence/ 找；兼容旧版本放在根目录的情况。"""
    p = os.path.join(BASE, name)
    if os.path.exists(p):
        return p
    return os.path.join(BASE, EVIDENCE, name)

# 三道演示题，全部来自真实运行的 trace 文件，不是编出来的例子
TOUR = [
    ("A1",  "trace_ra_160523_1_baseline.jsonl",
     "简单题：查一下就对，说明链路是通的。"),
    ("B5",  "trace_ra_160523_1_baseline.jsonl",
     "最危险的错：SQL 语法完全正确，数据库也没报错，答案却错了。\n"
     "      差的 5 件来自已取消/退款的订单，按口径不算「卖出」。"),
    ("C10", "trace_ra_160523_2_after.jsonl",
     "假修复：模型把年份写成 2022（应为 2026）导致查不到；工具提示 NULL 后，\n"
     "      它没去查年份，而是加 COALESCE 把 NULL 抹成 0 交卷。0 是合法数字、\n"
     "      库不报错，比留着 NULL 更难发现 —— 提示被采纳了，错却被藏得更深。"),
]
TOUR_MAP = {q: (path, note) for q, path, note in TOUR}


def load_trace(path):
    with open(_path(path), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def all_traces():
    names = set()
    for pattern in ("trace_*.jsonl", os.path.join(EVIDENCE, "trace_*.jsonl")):
        names.update(os.path.basename(p) for p in
                     glob.glob(os.path.join(BASE, pattern)))
    return sorted(names)


def find_file(qid):
    """优先用首页演示选定的那份证据，保证单看这道题讲的故事和连放时一致。"""
    if qid in TOUR_MAP:
        return TOUR_MAP[qid][0]
    best, count = None, 0
    for name in all_traces():
        recs = [r for r in load_trace(name) if r["q"] == qid]
        if len(recs) > count:
            best, count = name, len(recs)
    return best


def final_answer(recs):
    for r in reversed(recs):
        if r["kind"] in ("finalize", "nudge") or r["tool"] == "(none)":
            text = str(r.get("output", "")).strip()
            m = re.search(r"FINAL\s*[:：]\s*(.+)", text)
            if m:
                return m.group(1).strip(), text
            if text:
                return None, text
    return None, ""


def judge(ans, expect):
    if ans is None:
        return False, "未作答 / 格式不合规"
    try:
        return abs(float(str(ans).replace(",", "")) - float(expect)) <= 0.01, None
    except (ValueError, TypeError):
        return str(ans).strip() == str(expect).strip(), None


def show(qid, path, note):
    c = BY_ID[qid]
    recs = [r for r in load_trace(path) if r["q"] == qid]
    if not recs:
        print(f"  {qid}: {path} 里没有这道题的记录")
        return
    ans, raw = final_answer(recs)
    ok, why = judge(ans, c["expect"])

    print("=" * 72)
    print(f"【{qid}】{c['type']}题  ·  标准答案：{c['expect']}")
    print(f"问题：{c['q']}")
    print(f"证据：{EVIDENCE}/{path}（真实运行记录，共 {len(recs)} 步）")
    print("-" * 72)
    for r in recs:
        step = f"  第 {r['round']} 轮  [{r['kind']}]"
        if r["tool"] == "run_sql":
            sql = r["args"].get("sql", "")
            print(f"{step} 模型提出 SQL：")
            for line in sql.strip().splitlines():
                print("      " + line.strip())
            out = str(r.get("output", "")).replace("\n", "  /  ")
            print(f"      数据库返回：{out[:220]}")
        else:
            text = str(r.get("output", "")).replace("\n", " / ").strip()
            print(f"{step} 模型回答：{text[:200]}")
    print("-" * 72)
    mark = "PASS 答对" if ok else "FAIL 答错"
    shown = ans if ans is not None else (raw or "（空）")
    print(f"  判定：{mark}   模型交卷 = {shown}   标准答案 = {c['expect']}"
          + (f"   [{why}]" if why else ""))
    if note:
        print(f"  看点：{note}")
    print()


def main():
    args = sys.argv[1:]
    if args and args[0] == "--list":
        ids = set()
        for name in all_traces():
            ids.update(r["q"] for r in load_trace(name))
        # 早期教程的 q1/q2/q3 不在 50 题题库里，不列出来免得点了没结果
        ids &= set(BY_ID)
        print(f"可回放的题号（共 {len(ids)} 个，来自 {len(all_traces())} 份 trace）：")
        print("  " + "  ".join(sorted(ids)))
        return

    print("\nText-to-SQL Agent · 真实运行记录回放（0 次 API、0 花费）")
    print("题库：50 道中文题 | 数据库：6 张表 | 每轮最多 6 步 | 工具最多返回 20 行")
    print("注意：下面每一句 SQL、每一个返回值都是当时真实留下的，不是示例文案。\n")

    if not args:
        items = TOUR
    else:
        items = []
        for q in args:
            qid = q.upper()
            note = TOUR_MAP.get(qid, (None, "从 trace 中回放的完整过程。"))[1]
            items.append((qid, find_file(qid), note))

    for qid, path, note in items:
        if qid not in BY_ID:
            print(f"  题库里没有 {qid}，用 --list 看可用题号。")
            continue
        if not path:
            print(f"  找不到 {qid} 的 trace 记录。")
            continue
        show(qid, path, note)

    if not args:
        print("=" * 72)
        print("这三道题合起来就是这个项目要研究的事：")
        print("  1. 语法错不可怕，数据库会报错；「跑通了但口径算错」才危险。")
        print("  2. 所以主指标不比 SQL 字符串，只比执行结果和标准答案。")
        print("  3. 同一套配置自己就会跳 8 个点，任何改造都要 A/B 交替 + 留一组没被改动的题当参照。")
        print()
        print("想现场跑模型： python 03b_batch_eval.py A1   （需要 .env，会花一点额度）")
        print("想看全部结论： NOTES.md 第 34 节（简历素材）/ 第 36 节（最干净的一次假阳性）")
        print()


if __name__ == "__main__":
    main()
