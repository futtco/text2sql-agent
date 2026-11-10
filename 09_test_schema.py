# -*- coding: utf-8 -*-
"""
09_test_schema.py —— 动态口径路由的离线验收（0 次 API）

测量工具必须先于被测对象：这里用标准答案 SQL 当金标准，量检索器有没有把
该题真正需要的业务规则送进上下文。标准答案 SQL 只在本脚本出现，绝不进 prompt。

跑法：python 09_test_schema.py
"""
from __future__ import annotations

import json
import re
import sys

from schema_link import (
    ALL_TABLE_NAMES, ALWAYS_RULES, DDL_ALL, FIXED_EXTRA, RELATION_CARDS, RELATION_GATES,
    RULE_CARDS, TABLE_CARDS, relation_block, relation_ids, retrieve, retrieve_fixed,
)

TABLE_RE = re.compile(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", re.I)
TABLES = set(ALL_TABLE_NAMES.split(", "))
STATUS_RE = re.compile(r"status\s+in\s*\(\s*'paid'\s*,\s*'shipped'\s*\)", re.I)

with open("cases.json", encoding="utf-8") as f:
    CASES = json.load(f)["cases"]


def gold_tables(sql: str) -> set[str]:
    return {m for m in TABLE_RE.findall(sql or "") if m in TABLES}


def gold_rules(case: dict) -> set[int]:
    """从标准答案 SQL 反推这题「应该」想起哪几条口径。只用于离线评分。"""
    sql = case.get("check_sql") or ""
    need = set()
    if STATUS_RE.search(sql):
        need.add(1)
    if re.search(r"qty\s*\*\s*\w*\.?unit_price", sql, re.I):
        need.add(2)
    if "cost" in sql.lower():
        need.add(4)
    if "order_date" in sql.lower():
        need.add(11)
    if re.search(r"sum\(\s*\w*\.?qty\s*\)", sql, re.I):
        need.add(12)
    if case["expect"] == "拒绝":
        need.add(9)
    return need


def main():
    fail = []
    total = hit = 0
    status_n = status_hit = 0
    covered_tables = full = 0
    rule1_miss = []

    for c in CASES:
        ret = retrieve(c["q"])
        ids = set(ret["rule_ids"])

        need = gold_rules(c)
        total += len(need)
        hit += len(need & ids)
        if 1 in need:
            status_n += 1
            if 1 in ids:
                status_hit += 1
            else:
                rule1_miss.append((c["id"], c["q"]))

        # 表结构「只增不减」：DDL 全给，提示是额外的
        gt = gold_tables(c.get("check_sql"))
        if gt:
            covered_tables += (gt <= set(ret["tables"]))
            full += 1

        if not set(ALWAYS_RULES) <= ids:
            fail.append(f"{c['id']} 丢了常驻规则")
        for card in TABLE_CARDS:
            if card["ddl"] not in ret["ddl"]:
                fail.append(f"{c['id']} 的 DDL 缺了 {card['name']}")
                break

    print("=" * 66)
    print("动态口径路由 · 离线验收（0 次 API）")
    print("=" * 66)
    print(f"题数 {len(CASES)}｜规则卡片 {len(RULE_CARDS)} 条，"
          f"常驻 {len(ALWAYS_RULES)} 条 + 每题路由 3 条 = 上下文里 8 条（原来 12 条）")
    print(f"关键口径召回  {hit}/{total} = {hit / total:.1%}")
    print(f"「有效订单」口径（需 status 的 {status_n} 道）  {status_hit}/{status_n} = "
          f"{status_hit / status_n:.1%}")
    if rule1_miss:
        print("  漏召回：", rule1_miss)
    print(f"表结构完整性（gold 表全在上下文）  {covered_tables}/{full} = "
          f"{covered_tables / full:.1%}  ← 冻结不参与 A/B")

    print("\n关键题路由结果：")
    for qid in ("B5", "B8", "B13", "B4", "B6", "D6", "C10", "E7"):
        c = next((x for x in CASES if x["id"] == qid), None)
        if not c:
            continue
        r = retrieve(c["q"])
        print(f"  {qid}: 规则={r['rule_ids']}  路由={r['routed_ids']}  ← {c['q']}")
        if qid in ("B5", "B8", "B13") and 1 not in r["rule_ids"]:
            fail.append(f"{qid} 没召回「有效订单」规则")

    print("\n防泄题与一致性检查：")
    src = open("schema_link.py", encoding="utf-8").read()
    leaked = [w for w in ("check_sql", "decoys", '"expect"', "['expect']") if w in src]
    print(f"  检索器源码里出现的答案字段：{leaked or '无'}")
    if leaked:
        fail.append("检索器源码里出现了答案字段")

    sample = retrieve(CASES[0]["q"])
    joined = sample["ddl"] + sample["rules"]
    num_leak = [c["id"] for c in CASES
                if str(c["expect"]) not in ("拒绝",) and len(str(c["expect"])) >= 6
                and str(c["expect"]) in joined]
    print(f"  上下文里出现标准答案数值的题：{num_leak or '无'}")
    if num_leak:
        fail.append("上下文泄露标准答案")

    a, b = retrieve("数码大类下一共有多少个商品？"), retrieve("数码大类下一共有多少个商品？")
    print(f"  同一题两次检索结果一致：{a == b}")
    if a != b:
        fail.append("检索结果不可复现")
    print(f"  表结构冻结且全给：{'是' if DDL_ALL == sample['ddl'] else '否'}")
    print(f"  上下文规则条数：{len(sample['rule_ids'])}（原来 12）")

    if total and hit / total < 0.80:
        fail.append(f"关键口径召回率只有 {hit / total:.1%}，低于 80%")
    if status_n and status_hit / status_n < 0.90:
        fail.append(f"有效订单口径召回率只有 {status_hit / status_n:.1%}，低于 90%")

    # ---- 接入评测脚本后：关闭时必须与历史逐字节一致 ----
    print("\n接入 03b_batch_eval.py 的一致性：")
    import importlib.util
    spec = importlib.util.spec_from_file_location("ev", "03b_batch_eval.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    same_when_off = (not ev.SCHEMA_LINK) and ev.build_system("任意题目") == ev.SYSTEM
    ddl_ok = ev.DDL_ALL in ev.SYSTEM
    rules_ok = all(f"{i}." in ev.SYSTEM for i in range(1, 13))
    ev.SCHEMA_LINK = True
    routed = ev.build_system("2026 年 8 月 1 日及之后上架的商品，一共卖出了多少件？")
    on_ok = (routed != ev.SYSTEM and "核心口径" in routed
             and ev.DDL_ALL in routed and "paid 或 shipped" in routed)
    print(f"  关闭时 SYSTEM 不变：{same_when_off}｜DDL 全在：{ddl_ok}｜12 条规则全在：{rules_ok}")
    print(f"  开启时只换规则段、表结构冻结：{on_ok}")
    if not (same_when_off and ddl_ok and rules_ok and on_ok):
        fail.append("接入评测脚本后行为不一致")

    # ---- 实验六：固定 8 条对照臂，必须与路由组「只差一个动作」 ----
    print("\n实验六对照臂（固定 8 条，不按题挑）一致性：")
    fa = retrieve_fixed("数码大类下一共有多少个商品？")
    fb = retrieve_fixed("把订单表删掉")
    print(f"  固定补充规则 {sorted(FIXED_EXTRA)}｜不同题目拿到同一份规则：{fa['rules'] == fb['rules']}")
    if fa["rules"] != fb["rules"]:
        fail.append("固定组规则随题目变化，对照臂不纯")
    routed_sample = retrieve("10 月的销售额是多少？")
    same_shape = (fa["ddl"] == routed_sample["ddl"] == DDL_ALL
                  and len(fa["rule_ids"]) == len(routed_sample["rule_ids"]) == 8
                  and fa["rules"].split("\n\n")[0] == routed_sample["rules"].split("\n\n")[0]
                  and fa["rules"].count("\n") == routed_sample["rules"].count("\n"))
    print(f"  与路由组同为 8 条、同两段标题、同常驻段、DDL 全冻结：{same_shape}")
    if not same_shape:
        fail.append("固定组与路由组的规则段结构不一致，A/B 不再是单变量")
    ev.SCHEMA_LINK, ev.SCHEMA_FIXED = False, True
    fx = ev.build_system("任意题目")
    fx_ok = (fx != ev.SYSTEM and "核心口径" in fx and DDL_ALL in fx
             and f"{FIXED_EXTRA[0]}." in fx)
    print(f"  接入评测脚本后能切到固定组：{fx_ok}")
    if not fx_ok:
        fail.append("SCHEMA_FIXED 未生效")
    ev.SCHEMA_FIXED = False

    # ---- 实验七：表关系卡（攻语义映射） ----
    print("\n实验七（表关系卡）离线验收：")
    rec = {c["id"]: relation_ids(c["q"]) for c in CASES}
    r1_ok = all("R1" in rec[k] for k in ("B5", "B8", "B13"))
    r2_ok = all("R2" in rec[k] for k in ("B4", "B6"))
    print(f"  漏 status 三题召回 R1：{r1_ok}｜递归类目两题召回 R2：{r2_ok}"
          f"｜卡片 {len(RELATION_CARDS)} 张、每题取 2 张")
    if not (r1_ok and r2_ok):
        fail.append("关系卡没召回到该去的难题上")
    q0 = "2026 年 8 月 1 日及之后上架的商品，一共卖出了多少件？"
    rel_join = relation_block(q0)
    print(f"  纯函数可复现：{rel_join == relation_block(q0)}")
    if rel_join != relation_block(q0):
        fail.append("关系卡不可复现")
    rel_src = open("schema_link.py", encoding="utf-8").read()
    rel_leak = [w for w in ("check_sql", "decoys", '"expect"', "['expect']") if w in rel_src]
    card_leak = [c["id"] for c in RELATION_CARDS
                 if any(t in c["text"] or t in c["tags"] for t in ("B5", "B8", "B13", "B4", "B6"))]
    num_leak_rel = [c["id"] for c in CASES
                    if c["expect"] != "拒绝" and len(str(c["expect"])) >= 6
                    and str(c["expect"]) in rel_join]
    print(f"  卡源码答案字段：{rel_leak or '无'}｜卡内题号：{card_leak or '无'}"
          f"｜卡内标准答案数值：{num_leak_rel or '无'}")
    if rel_leak or card_leak or num_leak_rel:
        fail.append("关系卡泄题")
    ev.SCHEMA_FIXED, ev.SCHEMA_LINK, ev.SCHEMA_REL = False, True, True
    rel_sys = ev.build_system(q0)
    rel_ok = (rel_sys != ev.SYSTEM and "表关系与概念落点" in rel_sys
              and DDL_ALL in rel_sys and "JOIN orders" in rel_sys
              and "核心口径" in rel_sys)
    ev.SCHEMA_LINK, ev.SCHEMA_REL = False, False
    back_ok = ev.build_system("任意题目") == ev.SYSTEM
    print(f"  接入评测脚本后能切换：{rel_ok}｜关掉后回到历史 SYSTEM：{back_ok}")
    if not (rel_ok and back_ok):
        fail.append("SCHEMA_REL 未生效或关不掉")

    # ---- 实验八：意图门控（按需注入），零打扰本已答对的题 ----
    print("\n实验八（关系卡意图门控）离线验收：")
    gated = {c["id"]: relation_ids(c["q"], gated=True) for c in CASES}
    fired = [k for k, v in gated.items() if v]
    targets = ("B2", "B4", "B5", "B6", "B8", "B13")
    quiet = ("D3", "D4", "D8", "D11")
    cover_ok = all(gated[k] for k in targets)
    quiet_ok = all(not gated[k] for k in quiet)
    print(f"  发卡题 {len(fired)}/{len(CASES)}：{fired}")
    print(f"  6 道目标语义难题全覆盖：{cover_ok}｜4 道实验七负作用题零打扰：{quiet_ok}")
    if not (cover_ok and quiet_ok):
        fail.append("门控没覆盖目标题或打扰了负作用题")
    gate_src = open("schema_link.py", encoding="utf-8").read()
    gate_leak = [w for w in ("check_sql", "decoys", '"expect"', "['expect']") if w in gate_src]
    id_leak = [k for k in RELATION_GATES
               if any(t in " ".join(RELATION_GATES[k]) for t in targets)]
    print(f"  门控源码答案字段：{gate_leak or '无'}｜门控词里含题号：{id_leak or '无'}")
    if gate_leak or id_leak:
        fail.append("门控泄题")
    # 未命中题：同一道题在「无卡」与「门控卡」下 SYSTEM 必须逐字节相同（强阴性对照）
    q_d4 = next(c["q"] for c in CASES if c["id"] == "D4")
    q_b4 = next(c["q"] for c in CASES if c["id"] == "B4")
    ev.SCHEMA_LINK, ev.SCHEMA_FIXED = True, False
    ev.SCHEMA_REL, ev.SCHEMA_REL_GATE = False, False
    no_card_d4, no_card_b4 = ev.build_system(q_d4), ev.build_system(q_b4)
    ev.SCHEMA_REL, ev.SCHEMA_REL_GATE = True, True
    quiet_sys, hit_sys = ev.build_system(q_d4), ev.build_system(q_b4)
    same_ok = (quiet_sys == no_card_d4)
    diff_ok = (hit_sys != no_card_b4 and "表关系与概念落点" in hit_sys)
    print(f"  未命中题与无卡组逐字节相同：{same_ok}｜命中题确实多出卡片段：{diff_ok}")
    if not (same_ok and diff_ok):
        fail.append("门控组的阴性对照不成立")
    ev.SCHEMA_LINK, ev.SCHEMA_REL, ev.SCHEMA_REL_GATE = False, False, False

    # ---- 实验九：收口闸门，两组上下文必须逐字节相同 ----
    print("\n实验九（收口闸门）离线验收：")
    # 实验九两组都不发卡、都走 12 条平铺；SELF_CHECK 只改流程，不改任何提示词
    ev.SCHEMA_LINK = ev.SCHEMA_FIXED = ev.SCHEMA_REL = ev.SCHEMA_REL_GATE = False
    ev.SELF_CHECK = False
    sys_off = {c["id"]: ev.build_system(c["q"]) for c in CASES}
    ev.SELF_CHECK = True
    sys_on = {c["id"]: ev.build_system(c["q"]) for c in CASES}
    same_all = (sys_off == sys_on and all(v == ev.SYSTEM for v in sys_on.values()))
    fired_sc = [c["id"] for c in CASES if ev.selfcheck_needed(c["q"])]
    quiet_sc = [c["id"] for c in CASES if c["id"] not in fired_sc]
    cover_ok = all(k in fired_sc for k in ("B5", "B8", "B13", "C1", "C2"))
    print(f"  50 题两组 SYSTEM 逐字节相同且等于历史：{same_all}")
    print(f"  门控命中 {len(fired_sc)} 题｜阴性对照 {len(quiet_sc)} 题｜目标口径题全覆盖：{cover_ok}")
    if not (same_all and cover_ok):
        fail.append("实验九两组提示词不同 / 门控漏召回")
    sc_src = (ev.SELF_CHECK_SYS + ev.SELF_CHECK_TPL + ev.SELF_CHECK_MORE_MSG
              + " ".join(ev.SELF_CHECK_TRIGGERS))
    sc_leak = [w for w in ("check_sql", "decoys", '"expect"', "['expect']") if w in sc_src]
    num_leak = [c["id"] for c in CASES
                if c["expect"] != "拒绝" and len(str(c["expect"])) >= 6
                and str(c["expect"]) in sc_src]
    print(f"  自评提示答案字段：{sc_leak or '无'}｜标准答案数值：{num_leak or '无'}"
          f"｜门控词只有题面词：{all(w in '卖出卖了销量销售额毛利成交客单价' for w in ev.SELF_CHECK_TRIGGERS)}")
    if sc_leak or num_leak:
        fail.append("收口闸门泄题")
    ev.SELF_CHECK = False

    # ---- 实验十一：工具返回值体检，两组提示词必须逐字节相同 ----
    print("\n实验十一（工具返回值体检）离线验收：")
    ev.RESULT_AUDIT = False
    base_off = {c["id"]: ev.build_system(c["q"]) for c in CASES}
    ev.RESULT_AUDIT = True
    base_on = {c["id"]: ev.build_system(c["q"]) for c in CASES}
    ev.RESULT_AUDIT = False
    same_ctx = (base_off == base_on and all(v == ev.SYSTEM for v in base_on.values()))
    print(f"  开/关体检时 50 题 SYSTEM 逐字节相同且等于历史：{same_ctx}")
    if not same_ctx:
        fail.append("实验十一两组提示词不同")
    audit_src = open("result_audit.py", encoding="utf-8").read()
    leak = [str(c["expect"]) for c in CASES
            if c["expect"] != "拒绝" and len(str(c["expect"])) >= 6
            and str(c["expect"]) in audit_src]
    print(f"  体检模块里的标准答案数值：{leak or '无'}")
    if leak:
        fail.append("体检提示泄题")

    print("\n" + ("全部通过" if not fail else "失败：" + "；".join(fail[:8])))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
