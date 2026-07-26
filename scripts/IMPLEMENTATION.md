# 演示数据导入（import_ecommerce.py）实现说明

对应路线图 **P0-1**（`docs/openspec/roadmap.md` §1）：让 `./data/ecommerce.db`
（`settings.sqlite_db_path` 默认值）能被**一条命令**生成，demo 全程不依赖网络。
这是"一句中文问题 → SQL → 真实数据 → 回答"闭环的数据底座。

## 一、这个功能是什么、怎么用

`scripts/import_ecommerce.py` 把电商数据装进一个 SQLite 库，供 `execute_sql` 工具
（`app/agents/tools/sql_tool.py`）只读查询。提供两种模式：

### 模式一：从 Kaggle 官方 CSV 导入

下载 Kaggle **Brazilian E-Commerce (Olist)** 数据集，解压得到一批
`olist_*_dataset.csv`，指定目录即可：

```bash
# 默认输出到 ./data/ecommerce.db
python scripts/import_ecommerce.py --csv-dir ~/Downloads/olist
# 或自定义输出路径
python scripts/import_ecommerce.py --csv-dir ~/Downloads/olist --db ./data/ecommerce.db
```

### 模式二：合成演示数据（无 Kaggle 数据 / 离线 demo）

不下载任何数据，用标准库生成分布像样的演示数据（固定随机种子，结果可复现）：

```bash
python scripts/import_ecommerce.py --synthetic
python scripts/import_ecommerce.py --synthetic --db ./data/ecommerce.db --seed 42
```

两种模式导入完都会打印各表行数。合成模式默认规模：
customers 2000 / sellers 150 / products 600 / orders 3000 /
order_items ≈4849 / payments ≈3192。

导入后即可用 `execute_sql` 跑查询，例如"各州客户数 Top5"：

```sql
SELECT customer_state, COUNT(*) AS n FROM customers
GROUP BY customer_state ORDER BY n DESC LIMIT 5;
```

## 二、实现原理与关键技术点

### 1. 表结构设计（简名表）

按 `REQUIREMENTS.md` §5 规划，统一导入为六张**简名表**：
`orders / order_items / customers / products / sellers / payments`。字段类型约定：

- **ID 一律 TEXT**：Olist 的主键是 32 位十六进制串，天然是字符串；用 TEXT 避免
  被误当数字截断。
- **金额一律 REAL**：`price / freight_value / payment_value`。
- **时间一律 TEXT（ISO 字符串）**：`'YYYY-MM-DD HH:MM:SS'`。

主键：`orders(order_id)`、`customers(customer_id)`、`products(product_id)`、
`sellers(seller_id)`；`order_items(order_id, order_item_id)` 与
`payments(order_id, payment_sequential)` 用复合主键（一单多项 / 一单多笔支付）。

### 2. 索引

围绕**外键关联**与**高频聚合维度**建 11 个索引：
`order_items` 的 order_id / product_id / seller_id（JOIN 用），
`orders` 的 customer_id / status / purchase_timestamp，
`customers.state`、`sellers.state`、`products.category`、
`payments` 的 order_id / type。覆盖"按州/按状态/按品类/按时间"这类 demo 查询。

### 3. CSV 导入

用一张 `CSV_SPECS` 映射表描述"目标表 ← 官方文件名 + 列映射 + 类型转换器"，
`csv.DictReader` 逐行读、`executemany` 批量写。三个转换器
（`_text/_real/_int`）把空串统一归一为 `None`，避免 `""` 混进数值列。
用 `INSERT OR IGNORE` 对主键去重（Olist 个别表有重复行）。

### 4. 合成数据的分布设计

只用标准库（`random` + `math` + `datetime`），关键点是"像真的"：

- **可复现**：全程走单个 `random.Random(seed)`，连 32 位 ID 都用它逐字符抽
  （不用 `uuid4`，因为那走 `os.urandom` 不可复现）。同种子两次生成完全一致。
- **巴西州分布**：`STATE_WEIGHTS` 按真实情况让 SP 一家独大（权重 42），RJ/MG
  次之，展开成加权池后抽样。城市按州取代表城市。
- **时间跨度 2016-09~2018-10**：用 `date.toordinal` 的序号区间随机取日期，
  再拼随机时分秒成 ISO 串；已送达订单的 `delivered_date` = 下单 + 3~20 天，
  未送达则留空（贴近真实缺失）。
- **价格分布**：对数正态 `exp(gauss(3.6, 0.8))`，中位数约 37、长尾到数百，
  裁剪到 `[5, 2500]`；运费按单价比例 + 基础值。
- **外键一致性**：先建 customers/sellers/products 并留下 ID 列表，再生成
  orders 时从中抽引用，order_items/payments 再引用 orders，从构造上保证无孤儿行。
- **订单项/支付**：每单 1~4 项（偏 1~2）；支付方式按 credit_card 主导加权，
  信用卡带分期数；小概率把一单拆成 voucher + 主支付两笔，金额之和对齐订单总额。

### 5. 顶层复用

`build_demo_db()` 封装"建库 → 填充 → 建索引"，脚本 `main()` 与测试共用同一入口，
避免测试重造建库逻辑。

## 三、参考来源

- **`REQUIREMENTS.md` §5「数据准备」**：六张简名表的表结构规划与字段清单，
  以及 `sqlite3 .import` 的原始导入思路。
- **Kaggle Brazilian E-Commerce (Olist) 数据集**：真实文件命名
  （`olist_orders_dataset.csv` 等）、字段构成（订单 8 个时间戳字段、订单项含
  `freight_value`、支付含 `payment_sequential/installments`）、州代码与葡语品类名。
- **`app/agents/tools/sql_tool.py`**：导入产物要被 `create_execute_sql_tool` 只读
  查询，故字段命名与类型对齐该工具的使用场景（M-Schema 示例里的
  `order_id/customer_id/order_status/order_purchase_timestamp` 保持同名）。

## 四、与参考的区别与取舍

- **为什么用简名表**：Olist 官方文件名冗长（`olist_orders_dataset`），且分散在
  9 个文件。demo 只需核心 6 表，用简名（`orders`）让 LLM 生成的 SQL 更短更稳，
  也贴合 §5 的规划。列上只取分析需要的子集，砍掉 zip_code_prefix、商品尺寸等
  与 demo 问题无关的字段。
- **为什么合成模式只用标准库**：demo 要"离线一条命令跑起来"，不能依赖 Kaggle 账号
  / 网络 / pandas 等重依赖。标准库 + 固定种子既零依赖又可复现，面试时能随时重建。
- **为什么 TEXT 存时间**：SQLite 没有原生 DATE 类型，其
  `date()/datetime()/strftime()` 函数直接吃 ISO 字符串。存 TEXT 既符合 SQLite 惯例，
  又让"按月/按州"聚合的 demo SQL 无需任何转换即可跑通（测试已验证 `date()` 可解析）。
- **product_name 的取舍**：Kaggle products 数据集其实**没有商品名**，只有品类。
  真实导入时 `product_name` 回退用 `product_category_name` 填充（保证 §5 要求的
  name 列非空）；合成模式则造 `<品类>_item_0001` 这样的可读名。这是刻意的欠账：
  真实数据里没有的字段不硬编，宁可回退并在此记账。
- **未建物理外键约束**：SQLite 默认不强制外键，且导入顺序/性能考虑，这里靠生成逻辑
  保证一致性并用测试兜底，而非声明 `FOREIGN KEY`。demo 规模下这个取舍成本可控。
