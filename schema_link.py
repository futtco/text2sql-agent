# -*- coding: utf-8 -*-
"""
schema_link.py —— schema linking / 动态口径检索（项目本体第一块）

动机：SYSTEM 里一次塞 6 张表 DDL + 12 条业务规则，模型记不住。
      50 题复核里 B5/B8/B13 六遍全错，全是漏了「有效订单」这条口径；
      B4/B6 递归子类目也和「categories.parent_id 自连接」说明被埋没有关。

做法：把业务规则拆成卡片，按题目文本用「中文字符 bigram + IDF」路由 top-k 条，
      只把相关规则拼进 SYSTEM。不依赖题号，不读标准答案及其 SQL（防泄题）。

【为什么表结构不检索】09_test_schema.py 离线验收测过：6 张表取 top-4 时召回只有
      76.6%，完全覆盖的题仅 44% —— orders 几乎每道金额题都要用，但题目里常常
      不出现「订单」二字，纯词汇检索天然召不回。带着一个已知会漏表的组件上 A/B，
      分数掉了根本分不清是「规则检索没用」还是「漏表」。
      所以这一步：表结构全给（6 张 DDL 很短，本来也不是上下文瓶颈），
      另外按题附上 top-3 表的业务提示（只增不减）。规则检索是唯一变量。
      真正的 schema linking 留到表数量变多、检索值得做的时候。

设计边界（面试要自己说得清）：
  * 卡片的 tags 是「适用场景描述」，属于检索单元的元数据，就像给文档加标题；
    它不含答案、不含 SQL、不针对具体题号。换一套题库照样能用。
  * 【规则分层】09_test_schema.py 试过「12 条全交给检索」：有效订单口径 22 道里
    漏了 7 道（68.2%）。全局口径（有效订单、成交价、日期、只读、不编造）
    **不能靠检索决定给不给**，它们是每条 SQL 的前提，必须常驻；
    只有题型专属规则（客单价、毛利、变化量、百分比、归月、件数）才路由。
  * 【严格单变量】表结构冻结为 DDL_ALL，不注入表提示，也不删任何表。
    A/B 两组唯一差别就是「规则上下文」这一段。表结构的 schema linking 留到以后。
  * 检索器是纯函数，可离线量召回率，不用花 API。

红线：本模块只接收题目字符串。答案 SQL、标准答案、题号一律不进检索器。
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter

CASES_FILE = os.path.join(os.path.dirname(__file__), "cases.json")

# ---------------------------------------------------------------- 卡片库
# ddl 是唯一的 DDL 来源：03b_batch_eval.py 构造 SYSTEM 也从这里取，避免两处漂移。
TABLE_CARDS = [
    {
        "name": "categories",
        "ddl": "CREATE TABLE categories  (id INTEGER, name TEXT, parent_id INTEGER);        -- parent_id 为空表示顶级类目",
        "tags": "类目 分类 大类 子类 顶级 父类 层级 递归 上下级 数码 属于哪个类目 商品类别 子类目下的商品",
        "hint": "类目有父子层级：查大类下的商品要连同子类目一起算（parent_id 自连接或递归 CTE）。",
    },
    {
        "name": "products",
        "ddl": "CREATE TABLE products    (id INTEGER, name TEXT, category_id INTEGER, price REAL, cost REAL, launched_at TEXT);",
        "tags": "商品 产品 上架 价格 售价 现价 标价 成本 毛利 名称 商品数 最贵 最便宜 多少钱 上架日期",
        "hint": "price 是现价（标价），成交价在 order_items.unit_price；launched_at 是上架日期。",
    },
    {
        "name": "customers",
        "ddl": "CREATE TABLE customers   (id INTEGER, name TEXT, city TEXT, register_date TEXT);",
        "tags": "客户 顾客 用户 城市 地区 北京 上海 深圳 杭州 成都 注册 来自哪里 手机号",
        "hint": "表里没有 phone 列；按客户去重要用 COUNT(DISTINCT customers.id)。",
    },
    {
        "name": "orders",
        "ddl": "CREATE TABLE orders      (id INTEGER, customer_id INTEGER, order_date TEXT, status TEXT);",
        "tags": "订单 下单 笔数 状态 取消 退款 有效订单 月份 日期 哪个月 订单数 客单价 平均每单",
        "hint": "status 取值 paid / shipped / cancelled / refunded；月份一律按 order_date 归。",
    },
    {
        "name": "order_items",
        "ddl": "CREATE TABLE order_items (id INTEGER, order_id INTEGER, product_id INTEGER, qty INTEGER, unit_price REAL);",
        "tags": "明细 卖出 件数 销量 数量 销售额 成交价 金额 消费 收入 客单价 毛利 明细条数 商品明细",
        "hint": "销售额用 qty × unit_price（成交价快照）；卖出件数是 SUM(qty)，明细条数是 COUNT(id)。",
    },
    {
        "name": "payments",
        "ddl": "CREATE TABLE payments    (id INTEGER, order_id INTEGER, amount REAL, paid_at TEXT, method TEXT);",
        "tags": "支付 付款 实收 到账 收款 退款 支付方式 实付金额 收到多少钱",
        "hint": "paid_at 是到账日，不用来归月；归月一律用 orders.order_date。",
    },
]

DDL_ALL = "\n".join(c["ddl"] for c in TABLE_CARDS)
ALL_TABLE_NAMES = ", ".join(c["name"] for c in TABLE_CARDS)

# 规则原文从 cases.json 读，标签按序号配。新增规则没写标签时只靠原文检索（可用，但会弱一些）。
RULE_TAGS = {
    1: "有效订单 销售额 卖出件数 消费金额 收入 合计 毛利 客单价 统计金额 排除取消 排除退款 paid shipped status 过滤",
    2: "销售额 成交价 单价 现价 明细 金额 收入 商品价格 不要用标价",
    3: "客单价 平均每单 平均金额 每单 除以订单数",
    4: "毛利 利润 成本 毛利润 类目毛利",
    5: "数量 商品数量 订单笔数 一共多少 计数 不排除状态",
    6: "变化量 差值 环比 比上月 增减 增加 减少 正负号",
    7: "百分比 占比 比例 百分之 0到100 不带百分号",
    8: "今天 日期 当前时间",
    9: "删除 修改 清空 导出 更新 插入 拒绝 只读 权限 越权 注入 忽略规则",
    10: "不存在的字段 编造 幻觉 列名 假id 查不到 如实说明",
    11: "月份 按月 归月 9月 10月 哪个月 日期 到账日",
    12: "卖出多少件 件数 销量 明细条数 数量 求和 计数",
}


def load_rule_cards(cases_file: str = CASES_FILE):
    with open(cases_file, encoding="utf-8") as f:
        rules = json.load(f)["meta"]["system_rules"]
    return [
        {"id": i, "text": r, "tags": RULE_TAGS.get(i, "")}
        for i, r in enumerate(rules, 1)
    ]


RULE_CARDS = load_rule_cards()

# ---------------------------------------------------------------- 检索器
_STOP = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_CJK = re.compile(r"^[\u4e00-\u9fff]+$")


def bigrams(text: str) -> list[str]:
    """中文按字符 2-gram，英文/数字按整词。不需要分词器，也不依赖任何外部模型。"""
    out = []
    for tok in _STOP.sub(" ", str(text).lower()).split():
        if _CJK.match(tok):
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
            out.append(tok)           # 整词留一份，照顾「数码」这类两字词精确命中
        else:
            out.append(tok)
    return out or [str(text).lower()]


def _idf(docs: list[str]) -> dict[str, float]:
    df = Counter()
    for d in docs:
        df.update(set(bigrams(d)))
    n = len(docs) + 1
    return {g: math.log(n / (c + 1)) + 1.0 for g, c in df.items()}


_TABLE_TEXT = [c["name"] + " " + c["ddl"] + " " + c["tags"] + " " + c["hint"] for c in TABLE_CARDS]
_TABLE_IDF = _idf(_TABLE_TEXT)
_TABLE_GRAMS = [set(bigrams(t)) for t in _TABLE_TEXT]

# 全局口径与安全规则：每条 SQL 的前提，必须每道题都在，不参与检索打分。
# 1 有效订单 / 2 成交价 / 8 日期 / 9 只读 / 10 不编造
ALWAYS_RULES = (1, 2, 8, 9, 10)

# 【实验六对照臂：固定 8 条，不按题挑】
# 改造 F 的 A/B 里，基线是「12 条平铺」，实验组是「5 常驻 + 按题 top-3 + 分成两段」，
# 于是「精简/分层」和「按题路由」捆在一起，差值无法单独归因给路由。
# 这一组把 8 条规则的结构、条数、标题全部固定，只把「按题挑 3 条」换成「固定给 3 条」。
# 固定哪 3 条才公平？离线统计 50 道题里每条题型规则被路由召回的次数：
#   5→35 次、6→34 次、12→32 次、3→29 次、4→15 次、11→3 次、7→2 次
# 取最常被召回的 (5, 6, 12) —— 也就是对整份题库「平均最有用」的 3 条，
# 这是一个偏强的对照：如果按题路由还能赢它，才说明路由本身有贡献。
FIXED_EXTRA = (5, 6, 12)

_RULE_POOL = [c for c in RULE_CARDS if c["id"] not in ALWAYS_RULES]
_RULE_TEXT = [c["text"] + " " + c["tags"] for c in _RULE_POOL]
_RULE_IDF = _idf(_RULE_TEXT)
_RULE_GRAMS = [set(bigrams(t)) for t in _RULE_TEXT]


def _score(q_grams: set[str], card_grams: set[str], idf: dict[str, float]) -> float:
    """命中词的 IDF 之和 ÷ √卡片长度（cosine 式长度归一，避免长卡片永远赢）。"""
    hit = sum(idf.get(g, 1.0) for g in q_grams if g in card_grams)
    return hit / math.sqrt(len(card_grams) + 1)


def _topk(q_grams: set[str], card_grams: list[set[str]], idf: dict[str, float], k: int):
    scored = [(_score(q_grams, g, idf), i) for i, g in enumerate(card_grams)]
    scored.sort(key=lambda x: (-x[0], x[1]))       # 同分时按原始顺序，保证可复现
    return scored[:k]


def _format_rules(extra_ids) -> str:
    """规则段的唯一拼装入口：路由组与固定组共用，保证两段结构、标题逐字节一致。

    标题「与本题最相关的补充规则」对固定组其实不准确，但故意不改 ——
    改文案等于往 A/B 里塞进第二个变量（提示词措辞），还会让历史 after 数据不可比。
    """
    always = "\n".join(f"{i}. {RULE_CARDS[i - 1]['text']}" for i in ALWAYS_RULES)
    extra = "\n".join(f"{i}. {RULE_CARDS[i - 1]['text']}" for i in extra_ids)
    return (f"核心口径（**每条 SQL 写完都要逐条核对**）：\n{always}\n\n"
            f"与本题最相关的补充规则：\n{extra}")


def retrieve(question: str, k_rules: int = 3) -> dict:
    """按题目路由题型专属规则。纯函数，不读题库答案。

    返回 ddl / rules 两段文本（直接可拼进 SYSTEM），以及调试信息（离线算召回率用）。
    ddl 恒等于 DDL_ALL —— 表结构冻结，A/B 的唯一变量是规则段。
    """
    q = set(bigrams(question))
    r_hits = _topk(q, _RULE_GRAMS, _RULE_IDF, k_rules)

    routed = sorted(_RULE_POOL[i]["id"] for _, i in r_hits)
    rule_ids = sorted([*ALWAYS_RULES, *routed])
    return {
        "ddl": DDL_ALL,
        "rules": _format_rules(routed),
        "tables": [c["name"] for c in TABLE_CARDS],
        "table_scores": {},
        "rule_ids": rule_ids,
        "routed_ids": routed,
        "rule_scores": {_RULE_POOL[i]["id"]: round(s, 3) for s, i in r_hits},
    }


def retrieve_fixed(question: str) -> dict:
    """实验六对照臂：5 条常驻 + 固定的 3 条，不按题挑。

    question 只为签名兼容与日志可读，不参与任何打分 —— 两道不同的题必须拿到
    逐字节相同的规则段。这一组的意义就是「把路由这个动作单独拿掉」。
    """
    routed = sorted(FIXED_EXTRA)
    return {
        "ddl": DDL_ALL,
        "rules": _format_rules(routed),
        "tables": [c["name"] for c in TABLE_CARDS],
        "table_scores": {},
        "rule_ids": sorted([*ALWAYS_RULES, *routed]),
        "routed_ids": routed,
        "rule_scores": {},
    }


# ---------------------------------------------------------------- 表关系卡（实验七）
# 【和「业务规则」的区别，面试要讲得清】
#   规则 1 说「什么算有效订单」（paid / shipped）—— 这是业务口径；
#   关系卡说「status 这个字段在 orders 表里、qty 在 order_items 表里、
#            要 JOIN 才拿得到」—— 这是物理结构知识。
# B5/B8/B13 六遍全错、固定组与路由组 SQL 一模一样，根因不是没看见规则，
# 而是模型不知道「卖出」这个概念要跨两张表。规则挪位置、精简条数都救不了。
#
# 红线：卡里只有通用的表/字段/JOIN 路径，不含题号、不含标准答案数值、
#      不含任何一道题的完整答案 SQL。真实项目里这些本来就该由数据字典给出。
RELATION_CARDS = [
    {
        "id": "R1",
        "title": "成交量与订单状态",
        "tags": "卖出 销量 件数 销售额 消费 收入 金额 一共卖 卖出多少 卖了多少 有效订单 成交 明细 数量 总共 商品",
        "text": "order_items 只有明细（qty、unit_price），**没有订单状态**；status 在 orders 表。"
                "凡是问「卖出 / 销售额 / 消费 / 收入 / 件数」，SQL 必须 "
                "JOIN orders ON orders.id = order_items.order_id，再按核心口径过滤 orders.status；"
                "只查 order_items 会把取消、退款的明细也算进去。",
    },
    {
        "id": "R2",
        "title": "类目树与递归",
        "tags": "大类 子类目 类目 分类 属于 层级 递归 数码 家居 父类 上下级 下有多少商品 下的商品 多少件",
        "text": "categories.parent_id 是一棵树。问「某大类下有多少商品 / 卖了多少钱」时，"
                "不能只取 category_id = 父类 id，要先用递归 CTE 或自连接展开所有后代类目 id，"
                "再拿这些 id 去过滤 products。",
    },
    {
        "id": "R3",
        "title": "月份归属",
        "tags": "月份 按月 归月 9月 10月 11月 哪个月 下单 日期 到账 时间",
        "text": "按月份统计一律用 orders.order_date；payments.paid_at 是到账日，不用来归月。",
    },
    {
        "id": "R4",
        "title": "价格与成本",
        "tags": "价格 标价 售价 现价 成本 毛利 利润 成交额 单价 多少钱 最贵 最便宜 商品",
        "text": "products.price 是标价，成交价快照是 order_items.unit_price；"
                "销售额 = qty × unit_price，算毛利还要再减 products.cost（需 JOIN products）。",
    },
    {
        "id": "R5",
        "title": "客户与城市",
        "tags": "客户 顾客 用户 城市 北京 上海 深圳 杭州 成都 来自 注册 消费金额 不同客户 多少位",
        "text": "客户属性在 customers，订单归属在 orders.customer_id；"
                "按城市或客户统计要 JOIN customers，人数用 COUNT(DISTINCT customers.id)。",
    },
    {
        "id": "R6",
        "title": "实收与支付记录",
        "tags": "实收 到账 收款 支付 银行卡 支付记录 收到多少钱 支付方式 付款 实际收到",
        "text": "实际收到的钱在 payments.amount、方式在 payments.method，需 orders JOIN payments；"
                "payments 里没有 order_date，归月仍要回到 orders。",
    },
]

_REL_TEXT = [c["title"] + " " + c["tags"] + " " + c["text"] for c in RELATION_CARDS]
_REL_IDF = _idf(_REL_TEXT)
_REL_GRAMS = [set(bigrams(t)) for t in _REL_TEXT]


# ---------------------------------------------------------------- 实验八：意图门控（按需注入）
# 实验七的负作用：top-2 对 50 道题「每题都发 2 张卡」，R4/R5 等无关卡落到
# D3/D4/D8/D11 这类本来三遍全对的题上，诱发 flash 写出复杂 SQL 后把它贴在
# 正文里而不调工具，打回用尽就放弃作答。修法不是调 k，而是给检索器加一道
# 候选池门：题面必须出现明确的跨表意图词，对应卡才有资格参与 IDF 打分；
# 一题都没命中就整段不注入 —— 这些题的提示词与无卡组逐字节相同，
# 天然形成「上下文完全相同」的阴性对照（第 26 节教训的标准做法）。
#
# 门控词是「适用问法」的通用描述，不含题号、不含答案、不含任何库的专有名词，
# 换一套题库照样能用；打分排序仍复用同一套 bigram+IDF，检索器本身没换。
RELATION_GATES = {
    "R1": ("卖出", "卖了", "销量"),
    "R2": ("大类", "子类目", "类目下", "父类"),
}


def _gated_candidates(question: str) -> list[int]:
    return [i for i, c in enumerate(RELATION_CARDS)
            if any(w in question for w in RELATION_GATES.get(c["id"], ()))]


def relation_hits(question: str, k: int = 2, gated: bool = False):
    """按题目挑 top-k 张关系卡。纯函数，只用题面文本，不碰答案。

    gated=False（实验七）：全库 6 张卡一起排序，每题都发 top-k。
    gated=True （实验八）：先按意图词筛候选池，没命中返回空列表；
                          命中后仍在池内用同一套 IDF 打分排序。
    """
    q = set(bigrams(question))
    if gated:
        allowed = _gated_candidates(question)
        if not allowed:
            return []
        scored = [(_score(q, _REL_GRAMS[i], _REL_IDF), i) for i in allowed]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [RELATION_CARDS[i] for _, i in scored[:k]]
    return [RELATION_CARDS[i] for _, i in _topk(q, _REL_GRAMS, _REL_IDF, k)]


def relation_ids(question: str, k: int = 2, gated: bool = False) -> list[str]:
    return [c["id"] for c in relation_hits(question, k, gated)]


def relation_block(question: str, k: int = 2, gated: bool = False) -> str:
    """拼进 SYSTEM 的 ddl 段后面。无卡时返回空串 —— 调用方不要拼空段落，
    否则会多出两个换行，把「无卡题」的提示词弄得不等于对照组。"""
    cards = relation_hits(question, k, gated)
    if not cards:
        return ""
    return ("表关系与概念落点（写 SQL 前先确认字段在哪张表、要 JOIN 谁）：\n"
            + "\n".join(f"- {c['text']}" for c in cards))
