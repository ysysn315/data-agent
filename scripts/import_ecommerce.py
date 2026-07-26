#!/usr/bin/env python3
"""演示数据导入脚本 —— Kaggle Brazilian E-Commerce → SQLite。

对应 roadmap P0-1：让 ./data/ecommerce.db（settings.sqlite_db_path 默认值）
能被一条命令生成，demo 不依赖网络。

两种模式：
1. 真实导入：从 Kaggle 官方 CSV 目录导入（olist_*_dataset.csv）。
       python scripts/import_ecommerce.py --csv-dir ~/Downloads/olist
2. 合成演示：无 Kaggle 数据时，用标准库生成分布像样的合成数据（固定随机种子，可复现）。
       python scripts/import_ecommerce.py --synthetic

表结构见 REQUIREMENTS.md §5，统一导入为简名表：
orders / order_items / customers / products / sellers / payments。
时间统一以 TEXT（ISO 字符串）存储，可被 SQLite date()/datetime() 解析。
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 表结构（DDL）—— 简名表，字段类型：id TEXT、金额 REAL、时间 TEXT(ISO)
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE orders (
    order_id                        TEXT PRIMARY KEY,   -- 订单ID
    customer_id                     TEXT,               -- 客户ID（→ customers）
    order_status                    TEXT,               -- 订单状态
    order_purchase_timestamp        TEXT,               -- 下单时间(ISO)
    order_approved_at               TEXT,               -- 支付确认时间(ISO)
    order_delivered_customer_date   TEXT,               -- 送达客户时间(ISO)
    order_estimated_delivery_date   TEXT                -- 预计送达时间(ISO)
);

CREATE TABLE order_items (
    order_id        TEXT,               -- 订单ID（→ orders）
    order_item_id   INTEGER,            -- 订单内序号(1..n)
    product_id      TEXT,               -- 商品ID（→ products）
    seller_id       TEXT,               -- 卖家ID（→ sellers）
    price           REAL,               -- 商品单价
    freight_value   REAL,               -- 运费
    PRIMARY KEY (order_id, order_item_id)
);

CREATE TABLE customers (
    customer_id         TEXT PRIMARY KEY,   -- 客户ID
    customer_unique_id  TEXT,               -- 客户唯一标识（同一人多次下单）
    customer_city       TEXT,               -- 城市
    customer_state      TEXT                -- 州（巴西两位州代码）
);

CREATE TABLE products (
    product_id              TEXT PRIMARY KEY,   -- 商品ID
    product_category_name   TEXT,               -- 品类
    product_name            TEXT                -- 商品名
);

CREATE TABLE sellers (
    seller_id       TEXT PRIMARY KEY,   -- 卖家ID
    seller_city     TEXT,               -- 城市
    seller_state    TEXT                -- 州
);

CREATE TABLE payments (
    order_id                TEXT,       -- 订单ID（→ orders）
    payment_sequential      INTEGER,    -- 支付序号(1..n)
    payment_type            TEXT,       -- 支付方式
    payment_installments    INTEGER,    -- 分期数
    payment_value           REAL,       -- 支付金额
    PRIMARY KEY (order_id, payment_sequential)
);
"""

# 常用索引：覆盖外键关联与高频聚合维度（州、状态、时间、品类）
INDEX_SQL = """
CREATE INDEX idx_orders_customer_id   ON orders(customer_id);
CREATE INDEX idx_orders_status        ON orders(order_status);
CREATE INDEX idx_orders_purchase_ts   ON orders(order_purchase_timestamp);
CREATE INDEX idx_items_order_id       ON order_items(order_id);
CREATE INDEX idx_items_product_id     ON order_items(product_id);
CREATE INDEX idx_items_seller_id      ON order_items(seller_id);
CREATE INDEX idx_customers_state      ON customers(customer_state);
CREATE INDEX idx_sellers_state        ON sellers(seller_state);
CREATE INDEX idx_products_category    ON products(product_category_name);
CREATE INDEX idx_payments_order_id    ON payments(order_id);
CREATE INDEX idx_payments_type        ON payments(payment_type);
"""

TABLES = ["orders", "order_items", "customers", "products", "sellers", "payments"]


# ---------------------------------------------------------------------------
# 字段类型转换器：空串一律归一为 None，避免 "" 混入数值列
# ---------------------------------------------------------------------------
def _text(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


def _real(v: str | None) -> float | None:
    v = _text(v)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int(v: str | None) -> int | None:
    v = _text(v)
    if v is None:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Kaggle CSV → 简名表映射
# 键：目标表；值：(官方文件名, [(目标列, 源列, 转换器), ...])
# 说明：Kaggle products 数据集没有真正的商品名，只有品类，故 product_name
#       回退用品类填充（IMPLEMENTATION.md 有说明）。
# ---------------------------------------------------------------------------
CSV_SPECS = {
    "orders": (
        "olist_orders_dataset.csv",
        [
            ("order_id", "order_id", _text),
            ("customer_id", "customer_id", _text),
            ("order_status", "order_status", _text),
            ("order_purchase_timestamp", "order_purchase_timestamp", _text),
            ("order_approved_at", "order_approved_at", _text),
            ("order_delivered_customer_date", "order_delivered_customer_date", _text),
            ("order_estimated_delivery_date", "order_estimated_delivery_date", _text),
        ],
    ),
    "order_items": (
        "olist_order_items_dataset.csv",
        [
            ("order_id", "order_id", _text),
            ("order_item_id", "order_item_id", _int),
            ("product_id", "product_id", _text),
            ("seller_id", "seller_id", _text),
            ("price", "price", _real),
            ("freight_value", "freight_value", _real),
        ],
    ),
    "customers": (
        "olist_customers_dataset.csv",
        [
            ("customer_id", "customer_id", _text),
            ("customer_unique_id", "customer_unique_id", _text),
            ("customer_city", "customer_city", _text),
            ("customer_state", "customer_state", _text),
        ],
    ),
    "products": (
        "olist_products_dataset.csv",
        [
            ("product_id", "product_id", _text),
            ("product_category_name", "product_category_name", _text),
            # 官方数据无商品名，用品类回退
            ("product_name", "product_category_name", _text),
        ],
    ),
    "sellers": (
        "olist_sellers_dataset.csv",
        [
            ("seller_id", "seller_id", _text),
            ("seller_city", "seller_city", _text),
            ("seller_state", "seller_state", _text),
        ],
    ),
    "payments": (
        "olist_order_payments_dataset.csv",
        [
            ("order_id", "order_id", _text),
            ("payment_sequential", "payment_sequential", _int),
            ("payment_type", "payment_type", _text),
            ("payment_installments", "payment_installments", _int),
            ("payment_value", "payment_value", _real),
        ],
    ),
}


# ---------------------------------------------------------------------------
# 建库
# ---------------------------------------------------------------------------
def connect_fresh(db_path: str | Path) -> sqlite3.Connection:
    """创建（或覆盖）目标库：确保父目录存在，删除旧文件后重建 schema。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_SQL)
    return conn


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(INDEX_SQL)


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


# ---------------------------------------------------------------------------
# 模式一：从 Kaggle CSV 导入
# ---------------------------------------------------------------------------
def import_from_csv(conn: sqlite3.Connection, csv_dir: str | Path) -> dict[str, int]:
    csv_dir = Path(csv_dir)
    if not csv_dir.is_dir():
        raise FileNotFoundError(f"CSV 目录不存在: {csv_dir}")

    for table, (filename, colspec) in CSV_SPECS.items():
        path = csv_dir / filename
        if not path.exists():
            raise FileNotFoundError(
                f"缺少 Kaggle 文件: {path}（期望官方命名 {filename}）"
            )
        target_cols = [c[0] for c in colspec]
        placeholders = ", ".join(["?"] * len(target_cols))
        insert_sql = (
            f"INSERT OR IGNORE INTO {table} ({', '.join(target_cols)}) "
            f"VALUES ({placeholders})"
        )
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            batch = (
                tuple(conv(row.get(src)) for _, src, conv in colspec)
                for row in reader
            )
            conn.executemany(insert_sql, batch)
    conn.commit()
    return table_counts(conn)


# ---------------------------------------------------------------------------
# 模式二：合成演示数据（仅标准库 + 固定种子，可复现）
# ---------------------------------------------------------------------------
# 巴西州代码及权重（近似真实分布：SP 一家独大，东南沿海次之）
STATE_WEIGHTS: list[tuple[str, int]] = [
    ("SP", 42), ("RJ", 13), ("MG", 12), ("RS", 6), ("PR", 5),
    ("SC", 4), ("BA", 3), ("DF", 2), ("GO", 2), ("ES", 2),
    ("PE", 2), ("CE", 1), ("PA", 1), ("MT", 1), ("MA", 1),
    ("MS", 1), ("PB", 1), ("RN", 1), ("AL", 1), ("PI", 1),
]

# 各州代表城市（简化，仅取一两个大城市；其余用通用名兜底）
STATE_CITIES: dict[str, list[str]] = {
    "SP": ["sao paulo", "campinas", "guarulhos"],
    "RJ": ["rio de janeiro", "niteroi"],
    "MG": ["belo horizonte", "uberlandia"],
    "RS": ["porto alegre", "caxias do sul"],
    "PR": ["curitiba", "londrina"],
    "SC": ["florianopolis", "joinville"],
    "BA": ["salvador", "feira de santana"],
    "DF": ["brasilia"],
    "GO": ["goiania"],
    "ES": ["vitoria", "vila velha"],
    "PE": ["recife"],
    "CE": ["fortaleza"],
    "PA": ["belem"],
    "MT": ["cuiaba"],
    "MA": ["sao luis"],
    "MS": ["campo grande"],
    "PB": ["joao pessoa"],
    "RN": ["natal"],
    "AL": ["maceio"],
    "PI": ["teresina"],
}

# 真实 olist 品类（葡语），取常见若干
CATEGORIES = [
    "cama_mesa_banho", "beleza_saude", "esporte_lazer", "moveis_decoracao",
    "informatica_acessorios", "utilidades_domesticas", "relogios_presentes",
    "telefonia", "automotivo", "brinquedos", "cool_stuff", "perfumaria",
    "bebes", "eletronicos", "papelaria", "fashion_bolsas_e_acessorios",
    "pet_shop", "moveis_escritorio", "consoles_games", "construcao_ferramentas",
]

# 订单状态权重（delivered 绝对主导）
STATUS_WEIGHTS = [
    ("delivered", 88), ("shipped", 4), ("canceled", 3),
    ("invoiced", 2), ("processing", 2), ("unavailable", 1),
]

# 支付方式权重
PAYMENT_WEIGHTS = [
    ("credit_card", 74), ("boleto", 19), ("voucher", 5), ("debit_card", 2),
]

# 合成规模
N_CUSTOMERS = 2000
N_SELLERS = 150
N_PRODUCTS = 600
N_ORDERS = 3000

# 时间跨度：2016-09 ~ 2018-10（贴近真实 olist 区间）
_START_ORDINAL = 736208   # 2016-09-01
_END_ORDINAL = 736984     # 2018-10-16


def _weighted_pool(weights: list[tuple[str, int]]) -> list[str]:
    """把 (值, 权重) 展开成可 random.choice 的池子。"""
    pool: list[str] = []
    for value, w in weights:
        pool.extend([value] * w)
    return pool


def _hex_id(rng: random.Random) -> str:
    """生成 32 位十六进制 ID（模仿 olist 的 id 形态，受种子控制可复现）。"""
    return "".join(rng.choice("0123456789abcdef") for _ in range(32))


def _iso(rng: random.Random, ordinal: int) -> str:
    """把日历序号 + 随机时分秒拼成 ISO 字符串 'YYYY-MM-DD HH:MM:SS'。"""
    from datetime import date

    d = date.fromordinal(ordinal)
    h, m, s = rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59)
    return f"{d.isoformat()} {h:02d}:{m:02d}:{s:02d}"


def _price(rng: random.Random) -> float:
    """对数正态价格：中位数约 37，长尾到数百，裁剪到 [5, 2500]。"""
    val = math.exp(rng.gauss(3.6, 0.8))
    return round(min(max(val, 5.0), 2500.0), 2)


def generate_synthetic(conn: sqlite3.Connection, seed: int = 42) -> dict[str, int]:
    """生成合成演示数据；保证订单→订单项→支付的外键一致。"""
    rng = random.Random(seed)
    state_pool = _weighted_pool(STATE_WEIGHTS)
    status_pool = _weighted_pool(STATUS_WEIGHTS)
    payment_pool = _weighted_pool(PAYMENT_WEIGHTS)

    # --- customers ---
    customers: list[tuple] = []
    customer_ids: list[str] = []
    for _ in range(N_CUSTOMERS):
        cid = _hex_id(rng)
        state = rng.choice(state_pool)
        city = rng.choice(STATE_CITIES.get(state, [f"cidade_{state.lower()}"]))
        customers.append((cid, _hex_id(rng), city, state))
        customer_ids.append(cid)
    conn.executemany(
        "INSERT INTO customers VALUES (?, ?, ?, ?)", customers
    )

    # --- sellers ---
    sellers: list[tuple] = []
    seller_ids: list[str] = []
    for _ in range(N_SELLERS):
        sid = _hex_id(rng)
        state = rng.choice(state_pool)
        city = rng.choice(STATE_CITIES.get(state, [f"cidade_{state.lower()}"]))
        sellers.append((sid, city, state))
        seller_ids.append(sid)
    conn.executemany("INSERT INTO sellers VALUES (?, ?, ?)", sellers)

    # --- products ---
    products: list[tuple] = []
    product_ids: list[str] = []
    for i in range(N_PRODUCTS):
        pid = _hex_id(rng)
        category = rng.choice(CATEGORIES)
        products.append((pid, category, f"{category}_item_{i:04d}"))
        product_ids.append(pid)
    conn.executemany("INSERT INTO products VALUES (?, ?, ?)", products)

    # --- orders + order_items + payments ---
    orders: list[tuple] = []
    items: list[tuple] = []
    payments: list[tuple] = []
    for _ in range(N_ORDERS):
        oid = _hex_id(rng)
        customer_id = rng.choice(customer_ids)
        status = rng.choice(status_pool)

        purchase_ord = rng.randint(_START_ORDINAL, _END_ORDINAL)
        purchase_ts = _iso(rng, purchase_ord)
        approved_ts = _iso(rng, purchase_ord)  # 同日稍后确认（时分秒随机）
        # 送达/预计送达仅对已送达订单填充，其余留空（贴近真实缺失情况）
        if status == "delivered":
            delivered_ts = _iso(rng, purchase_ord + rng.randint(3, 20))
        else:
            delivered_ts = None
        estimated_ts = _iso(rng, purchase_ord + rng.randint(10, 30))
        orders.append(
            (oid, customer_id, status, purchase_ts, approved_ts,
             delivered_ts, estimated_ts)
        )

        # 每单 1~4 个订单项（偏向 1~2）
        n_items = rng.choices([1, 2, 3, 4], weights=[60, 25, 10, 5])[0]
        order_total = 0.0
        for item_no in range(1, n_items + 1):
            price = _price(rng)
            freight = round(price * rng.uniform(0.05, 0.25) + rng.uniform(5, 20), 2)
            order_total += price + freight
            items.append(
                (oid, item_no, rng.choice(product_ids), rng.choice(seller_ids),
                 price, freight)
            )

        # 支付：多数一笔付清；小概率拆成两笔（含一张 voucher）
        ptype = rng.choice(payment_pool)
        installments = rng.choice([1, 1, 1, 2, 3, 4, 6, 10]) if ptype == "credit_card" else 1
        if rng.random() < 0.08 and order_total > 40:
            voucher_val = round(order_total * rng.uniform(0.1, 0.3), 2)
            payments.append((oid, 1, "voucher", 1, voucher_val))
            payments.append(
                (oid, 2, ptype, installments, round(order_total - voucher_val, 2))
            )
        else:
            payments.append((oid, 1, ptype, installments, round(order_total, 2)))

    conn.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", orders
    )
    conn.executemany(
        "INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?)", items
    )
    conn.executemany(
        "INSERT INTO payments VALUES (?, ?, ?, ?, ?)", payments
    )
    conn.commit()
    return table_counts(conn)


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------
def build_demo_db(
    db_path: str | Path,
    csv_dir: str | Path | None = None,
    synthetic: bool = False,
    seed: int = 42,
) -> dict[str, int]:
    """建库并填充数据，返回各表行数。供脚本 main 与测试共用。"""
    conn = connect_fresh(db_path)
    try:
        if synthetic:
            counts = generate_synthetic(conn, seed=seed)
        else:
            if not csv_dir:
                raise ValueError("非 --synthetic 模式必须提供 --csv-dir")
            counts = import_from_csv(conn, csv_dir)
        create_indexes(conn)
    finally:
        conn.close()
    return counts


def _print_counts(db_path: str | Path, counts: dict[str, int]) -> None:
    print(f"\n演示库已生成: {db_path}")
    print("各表行数:")
    for table in TABLES:
        print(f"  {table:<12} {counts[table]:>8}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="导入/生成演示用 Brazilian E-Commerce SQLite 库"
    )
    parser.add_argument(
        "--csv-dir",
        help="Kaggle 官方 CSV 目录（含 olist_*_dataset.csv）",
    )
    parser.add_argument(
        "--db",
        default="./data/ecommerce.db",
        help="输出 SQLite 路径（默认 ./data/ecommerce.db）",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="无 Kaggle 数据时生成合成演示数据（标准库 + 固定种子）",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="合成模式随机种子（默认 42，保证可复现）",
    )
    args = parser.parse_args(argv)

    if not args.synthetic and not args.csv_dir:
        parser.error("请指定 --csv-dir <Kaggle目录> 或 --synthetic")

    counts = build_demo_db(
        db_path=args.db,
        csv_dir=args.csv_dir,
        synthetic=args.synthetic,
        seed=args.seed,
    )
    _print_counts(args.db, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
