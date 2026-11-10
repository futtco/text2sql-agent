# -*- coding: utf-8 -*-
"""
make_shop_db.py —— 建一个六表电商库 shop.db

为什么不用 demo.db：那个库只有 4 个商品、6 个订单，出不了 20 道不重复的题。

表结构（4 张主表 + 2 张明细）：

  categories   类目，有父子层级（数码 → 手机/电脑/配件）
  products     商品，有成本价，可算毛利
  customers    客户，有城市、注册时间
  orders       订单，有状态 paid/shipped/cancelled/refunded
  order_items  订单明细，**存成交价快照**，不是商品现价
  payments     支付记录，未支付订单没有对应行

四个坑（出题就靠它们）：
  1. cancelled / refunded 的订单不该计入销售额
  2. 销售额要用 order_items.unit_price，不是 products.price（有折扣）
  3. categories 有父子层级，按大类统计要 join 两次
  4. 部分订单没有 payment 记录，JOIN 写错会漏数据

跑法：
    python make_shop_db.py
"""

import sqlite3
import random

# 固定种子：数据必须每次重建都一样，否则 expect（标准答案）会跟着变
random.seed(42)

DB = "shop.db"

SCHEMA = """
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS categories;

CREATE TABLE categories (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    parent_id INTEGER           -- NULL 表示顶级类目
);

CREATE TABLE products (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    price       REAL NOT NULL,  -- 现价
    cost        REAL NOT NULL,  -- 成本价，可算毛利
    launched_at TEXT NOT NULL   -- 上架日期
);

CREATE TABLE customers (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    city          TEXT NOT NULL,
    register_date TEXT NOT NULL
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    order_date  TEXT NOT NULL,
    status      TEXT NOT NULL   -- paid / shipped / cancelled / refunded
);

CREATE TABLE order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    qty        INTEGER NOT NULL,
    unit_price REAL NOT NULL    -- 成交价快照，可能低于现价
);

CREATE TABLE payments (
    id       INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    amount   REAL NOT NULL,
    paid_at  TEXT NOT NULL,
    method   TEXT NOT NULL      -- alipay / wechat / card
);
"""

# ---------------------------------------------------------------- 类目
# 三层结构：顶级 → 子类。按"数码"统计时要把手机+电脑+配件都算进去
CATEGORIES = [
    (1, "数码", None),
    (2, "手机", 1),
    (3, "电脑", 1),
    (4, "配件", 1),
    (5, "家居", None),
    (6, "厨具", 5),
    (7, "清洁", 5),
    (8, "服饰", None),      # 没有子类，测"空类目"
]

# ---------------------------------------------------------------- 商品
# (id, 名称, 类目, 现价, 成本, 上架日期)
PRODUCTS = [
    (1,  "手机A",    2, 3999.0, 2800.0, "2026-01-10"),
    (2,  "手机B",    2, 2599.0, 1800.0, "2026-03-05"),
    (3,  "手机C",    2, 1299.0,  900.0, "2026-06-20"),
    (4,  "笔记本A",  3, 6999.0, 5200.0, "2026-02-11"),
    (5,  "笔记本B",  3, 4599.0, 3400.0, "2026-05-08"),
    (6,  "键盘",     4,  399.0,  180.0, "2026-01-15"),
    (7,  "鼠标",     4,  129.0,   60.0, "2026-02-20"),
    (8,  "显示器",   4, 1299.0,  950.0, "2026-04-01"),
    (9,  "台灯",     6,  199.0,   90.0, "2026-03-12"),
    (10, "炒锅",     6,  349.0,  190.0, "2026-07-02"),
    (11, "拖把",     7,   89.0,   40.0, "2026-01-20"),
    (12, "洗衣液",   7,   49.0,   25.0, "2026-02-25"),
    (13, "T恤",      8,   99.0,   40.0, "2026-05-15"),
    (14, "牛仔裤",   8,  259.0,  120.0, "2026-06-01"),
    (15, "充电器",   4,   79.0,   35.0, "2026-03-18"),
    (16, "耳机",     4,  599.0,  320.0, "2026-04-22"),
    (17, "平板",     2, 3299.0, 2400.0, "2026-08-05"),   # 8 月才上架
    (18, "机械键盘", 4,  549.0,  260.0, "2026-09-01"),   # 9 月才上架
    (19, "电饭煲",   6,  459.0,  260.0, "2026-02-14"),
    (20, "吸尘器",   7,  899.0,  560.0, "2026-07-19"),
]

# ---------------------------------------------------------------- 客户
CITIES = ["北京"] * 5 + ["上海"] * 5 + ["深圳"] * 4 + ["杭州"] * 3 + ["成都"] * 3
random.shuffle(CITIES)

CUSTOMERS = [
    (i + 1, f"客户{i + 1:02d}", CITIES[i],
     f"2026-{random.randint(1, 9):02d}-{random.randint(1, 28):02d}")
    for i in range(20)
]

# ---------------------------------------------------------------- 订单
# 60 单，跨 9/10/11 三个月（11 月只到 6 号，因为"今天"是 2026-11-07）
#
# 状态按月份分层，不要全局 shuffle：
#   全局 shuffle 会让 cancelled / refunded 恰好堆在某一个月（实测全落在 9 月），
#   凭空造出「9 月订单特别容易被取消」这种假模式，也让 9→10 月环比失真到 +188%。
#   现在每个月按同一比例独立分配、各自 shuffle，只保留随机性，不保留时间相关性。
MONTHS = [("09", 30, 20), ("10", 31, 22), ("11", 6, 18)]
STATUS_NAMES = ("paid", "shipped", "cancelled", "refunded")
STATUS_RATIO = (40, 10, 6, 4)      # 全局比例，总计 60


def month_status_pool(count):
    """按全局比例给本月的 count 个订单分状态，最后一类吃余数，保证合计不变。"""
    ratio_total = sum(STATUS_RATIO)
    nums = [round(count * r / ratio_total) for r in STATUS_RATIO[:-1]]
    nums.append(count - sum(nums))
    pool = []
    for name, n in zip(STATUS_NAMES, nums):
        pool.extend([name] * n)
    random.shuffle(pool)
    return pool


ORDERS = []
oid = 0
for month, max_day, count in MONTHS:
    pool = month_status_pool(count)
    for i in range(count):
        oid += 1
        ORDERS.append((
            oid,
            random.randint(1, 20),
            f"2026-{month}-{random.randint(1, max_day):02d}",
            pool[i],
        ))

# ---------------------------------------------------------------- 订单明细
# 每单 1-3 个商品；成交价 = 现价 × 折扣（0.80 ~ 1.00）
ITEMS = []
iid = 0
order_total = {}          # order_id -> 明细合计，给 payments 用
for o in ORDERS:
    for _ in range(random.randint(1, 3)):
        iid += 1
        pid = random.randint(1, 20)
        qty = random.randint(1, 3)
        price = PRODUCTS[pid - 1][3]
        discount = random.choice([0.80, 0.85, 0.90, 0.95, 1.00])
        unit = round(price * discount, 2)
        ITEMS.append((iid, o[0], pid, qty, unit))
        order_total[o[0]] = round(order_total.get(o[0], 0) + qty * unit, 2)

# ---------------------------------------------------------------- 支付
# 只有 paid / shipped 的订单有支付记录，其余为空
PAYMENTS = []
pid = 0
for o in ORDERS:
    if o[3] in ("paid", "shipped"):
        pid += 1
        day = int(o[2].split("-")[2])
        PAYMENTS.append((
            pid, o[0], order_total.get(o[0], 0.0),
            o[2].replace(f"-{day:02d}", f"-{min(day + 1, 28):02d}"),
            random.choice(["alipay", "wechat", "card"]),
        ))

# ---------------------------------------------------------------- 写库
conn = sqlite3.connect(DB)
conn.executescript(SCHEMA)
conn.executemany("INSERT INTO categories VALUES (?,?,?)", CATEGORIES)
conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", PRODUCTS)
conn.executemany("INSERT INTO customers VALUES (?,?,?,?)", CUSTOMERS)
conn.executemany("INSERT INTO orders VALUES (?,?,?,?)", ORDERS)
conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", ITEMS)
conn.executemany("INSERT INTO payments VALUES (?,?,?,?,?)", PAYMENTS)
conn.commit()

print(f"{DB} 建好了\n")
print(f"categories  {len(CATEGORIES):>4} 条")
print(f"products    {len(PRODUCTS):>4} 条")
print(f"customers   {len(CUSTOMERS):>4} 条")
print(f"orders      {len(ORDERS):>4} 条")
print(f"order_items {len(ITEMS):>4} 条")
print(f"payments    {len(PAYMENTS):>4} 条")

# ---------------------------------------------------------------- 自检
print("\n订单状态分布：")
for row in conn.execute("SELECT status, COUNT(*) FROM orders GROUP BY status ORDER BY 2 DESC"):
    print(f"  {row[0]:<10} {row[1]:>3}")

print("\n各类目销售额（只算 paid + shipped，用成交价）：")
sql = """
SELECT c.name,
       ROUND(SUM(oi.qty * oi.unit_price), 2) AS amount
FROM order_items oi
JOIN orders o   ON o.id = oi.order_id
JOIN products p ON p.id = oi.product_id
JOIN categories c ON c.id = p.category_id
WHERE o.status IN ('paid', 'shipped')
GROUP BY c.name
ORDER BY amount DESC
"""
for row in conn.execute(sql):
    print(f"  {row[0]:<8} {row[1]:>12,.2f}")

total = conn.execute("""
SELECT ROUND(SUM(oi.qty * oi.unit_price), 2)
FROM order_items oi JOIN orders o ON o.id = oi.order_id
WHERE o.status IN ('paid', 'shipped')
""").fetchone()[0]
print(f"\n全期有效销售额：{total:,.2f}")

no_pay = conn.execute("""
SELECT COUNT(*) FROM orders o
LEFT JOIN payments p ON p.order_id = o.id
WHERE p.id IS NULL
""").fetchone()[0]
print(f"没有支付记录的订单：{no_pay} 条（含 cancelled / refunded）")

# 分层是否生效：看每个月的状态构成和有效销售额，环比不该再出现 +188% 这种数
print("\n各月状态分布（订单数）：")
for ym, in conn.execute("SELECT DISTINCT substr(order_date,1,7) FROM orders ORDER BY 1"):
    parts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM orders WHERE substr(order_date,1,7)=? GROUP BY status",
        (ym,)).fetchall())
    print(f"  {ym}  " + "  ".join(f"{s}={parts.get(s,0):>2}" for s in STATUS_NAMES))

print("\n各月有效销售额：")
monthly = conn.execute("""
SELECT substr(o.order_date,1,7) AS ym, ROUND(SUM(oi.qty*oi.unit_price),2)
FROM order_items oi JOIN orders o ON o.id=oi.order_id
WHERE o.status IN ('paid','shipped') GROUP BY 1 ORDER BY 1
""").fetchall()
for ym, amt in monthly:
    print(f"  {ym}  {amt:>12,.2f}")
if len(monthly) >= 2:
    print(f"  9→10 月环比：{(monthly[1][1]/monthly[0][1]-1)*100:+.1f}%")

conn.close()
