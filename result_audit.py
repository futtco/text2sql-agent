# -*- coding: utf-8 -*-
"""工具返回值体检（实验十一用）。

设计原则：
  1. 纯函数：只看 SQL 文本和执行结果，绝不读题库 / 标准答案 / 题号，
     也不连数据库，不可能泄题。
  2. 只加一行提示，不替模型算数、不给答案、不改 SQL，也不改 SYSTEM 提示词。
  3. 由调用方用开关 RESULT_AUDIT 控制，默认关 —— 历史实验逐字节可复现。
  4. 宁可少报：只覆盖离线诊断里确实出现的三类确定性陷阱
     （空结果 / 聚合出 NULL / 整数除法），多列结果不报（那是正常查询形态）。

离线依据（实验九 4 遍 × 50 题 = 200 题次，逐条真跑过库核对）：
  空结果 1 题次(错 1)、NULL 3 题次(错 3)、疑似整数除法 2 题次(错 2)；
  这 6 个题次全是「SQL 执行成功、返回值本身错」，数据库不会报错。
"""
import re

MSG_EMPTY = ("[结果体检] 本次查询没有返回任何行。可能是条件过严、JOIN 方向写反，"
             "或者该用 LEFT JOIN；空结果不等于 0，请检查后重新查询。")
MSG_NULL = ("[结果体检] 返回值里有 NULL。SUM/AVG 作用在空集或全 NULL 列上会得到 NULL，"
            "通常是 JOIN 没连上或过滤条件把行全排掉了；必要时用 COALESCE 处理。")
MSG_INTDIV = ("[结果体检] 返回值是整数，但 SQL 里有除法。SQLite 整数相除会直接截断成整数，"
              "若需要小数请把被除数乘 1.0（或 CAST 成 REAL）后重查。")

_DIV = re.compile(r"/")
_FLOAT_HINT = re.compile(r"1\.0|\bcast\s*\(|\breal\b|\bround\s*\(", re.I)
_AGG = re.compile(r"sum|avg|total|amount|sales|profit|received|qty|count", re.I)


def audit(sql: str, rows: list, cols: list) -> str:
    """返回要追加到 run_sql 结果末尾的一行提示；没触发就返回空串。

    sql  : 本次实际执行的 SQL（小写与否均可）
    rows : 游标 fetchall() 的结果（tuple 列表）
    cols : 列名列表
    """
    if not rows:
        return MSG_EMPTY

    first = rows[0]
    # 聚合出 NULL：只对「看起来是聚合结果」的列报，普通明细里的 NULL 不管
    looks_agg = bool(cols) and any(_AGG.search(str(c)) for c in cols)
    if looks_agg and any(v is None for v in first):
        return MSG_NULL

    # 整数除法：SQL 有除号、没有明显的浮点处理、且返回的第一个值恰是 int
    if (len(first) >= 1 and isinstance(first[0], int)
            and not isinstance(first[0], bool)
            and _DIV.search(sql) and not _FLOAT_HINT.search(sql)):
        return MSG_INTDIV

    return ""
