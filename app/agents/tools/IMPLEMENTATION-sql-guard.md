# SQL 校验层实现说明（sql_guard）

roadmap P1-1。在 `execute_sql` 真正把 SQL 交给 SQLite 之前，用 sqlglot 把语句解析成
AST 做一遍语法 / 只读 / 表列存在性校验，并在缺 LIMIT 时自动补齐。核心文件：

- `app/agents/tools/sql_guard.py` —— 纯函数 `validate_sql`，不依赖数据库连接
- `app/agents/tools/sql_tool.py` —— `execute_sql` 集成校验（现取 schema）
- `tests/test_sql_guard.py` —— 校验层 + 集成测试

---

## ① 功能与用法

```python
from app.agents.tools.sql_guard import validate_sql

r = validate_sql(
    "SELECT price FROM orders",
    schema={"orders": ["order_id", "customer_state", "price"]},
    default_limit=1000,
)
# r.ok == True
# r.fixed_sql == "SELECT price FROM orders LIMIT 1000"   # 自动补了 LIMIT
```

返回 `GuardResult(ok, fixed_sql, error)`：

- `ok=True`：`fixed_sql` 为规范化后的可执行 SQL（可能已补 LIMIT），`error=None`
- `ok=False`：`error` 是中文报错，含语法位置或候选表 / 列，`fixed_sql=None`

`schema` 为 `None` 时跳过表 / 列校验，只做语法、只读、单语句、自动 LIMIT。

几个实际输入 → 输出：

| 输入 | 输出 |
|---|---|
| `SELECT price FROM orders` | ok，`... LIMIT 1000` |
| `WITH r AS (SELECT price FROM orders) SELECT SUM(price) FROM r` | ok（CTE 名 `r` 不报未知表） |
| `SELECT bad FROM orders` | err：表 orders 不存在列「bad」。该表可用列：order_id、customer_state、price |
| `SELECT * FROM ghost` | err：引用了未知表：ghost。可用表：orders |
| `UPDATE orders SET price=0` | err：只允许 SELECT / WITH 查询（禁止 UPDATE 等写操作） |
| `SELECT FROM WHERE` | err：SQL 语法错误：… Line 1, Col: 17. |

---

## ② 实现原理

### AST 解析与只读判断

`sqlglot.parse(sql, dialect="sqlite")` 把文本解析成语句列表。据此：

- **单语句约束**：`parse` 会把分号分隔的多条语句拆成多个元素，`len > 1` 直接拒。
- **只读约束**：判断**最外层节点类型**，只放行 `(exp.Select, exp.Union)`。这是「AST 层
  判断而非字符串首词」的关键 —— `WITH x AS (...) INSERT ...` 的首词是 `WITH`，字符串判断
  会误放，但 sqlglot 解析后最外层是 `exp.Insert`（`WITH` 只是挂在 `args["with"]` 上），
  于是被拦下。`WITH ... SELECT` 最外层则是 `exp.Select`，正常放行。

### CTE 排除算法（本任务的核心坑）

提取「真实引用的物理表名」时必须排除 CTE 别名，否则 `WITH cte AS (...) SELECT * FROM cte`
里的 `cte` 会被当成一张不存在的表误报。做法（`_extract_real_tables`）：

```python
cte_names = {cte.alias for cte in root.find_all(exp.CTE) if cte.alias}
tables = {t.name for t in root.find_all(exp.Table)
          if t.name and t.name not in cte_names}
```

先遍历所有 `exp.CTE` 收集别名，再遍历 `exp.Table`，名字落在 CTE 别名集合里的一律跳过。
注意这只排除「CTE 名本身」，CTE **内部**引用的真实表照常进入校验 —— 所以
`WITH t AS (SELECT * FROM ghost_table) SELECT * FROM t` 仍会因 `ghost_table` 未知而报错。

### 列名校验（保守，避免误报）

列校验最容易误报，策略是「只在能可靠定位到某张已知物理表时才校验」：

- **限定列 `t.col`**：把 `t` 经别名表（仅由物理 `exp.Table` 构造）解析成真实表名，
  若该表已知且不含 `col` → 报错。派生表 `(...) d` 是 `exp.Subquery` 不是 `exp.Table`，
  不会进别名表，故 `SELECT d.x FROM (...) d` 的 `d.x` 解析不到物理表 → 安全跳过。
- **非限定列 `col`**：仅当整条 SQL「只引用一张物理表、无 CTE、无子查询」这种能唯一
  确定列归属的简单场景才校验；JOIN、子查询、CTE 等多作用域场景一律跳过，宁可漏报不误报。

### 自动 LIMIT 的 AST 改写

```python
if root.args.get("limit") is None:      # 最外层没有 LIMIT
    root = root.limit(default_limit)     # AST 层挂上 LIMIT 节点
fixed_sql = root.sql(dialect="sqlite")   # 由 AST 回写文本
```

判断和改写都在 AST 上做：`root.args["limit"]` 直接看最外层查询有没有 LIMIT 节点
（子查询里的 LIMIT 不影响判断），`.limit()` 生成新节点，最后 `.sql()` 回写。因此
`SELECT a FROM t LIMIT 5` 不会被重复加 LIMIT，`WITH ... SELECT` 补 LIMIT 后 WITH 结构完整保留。

---

## ③ SQLBot 是怎么做的

参考仓库 `sqlbot-reference`（DataEase SQLBot）同样用 sqlglot 做 SQL 安全校验：

- `backend/apps/db/db.py` `check_sql_read()`：`sqlglot.parse` 后按数据源方言
  （`get_sqlglot_dialect`：mysql / tsql / hive…）解析，通过 `find_all(exp.Anonymous)`
  查危险函数（`version` / `LOAD_FILE` / `xp_cmdshell`…），并用 `isinstance` 判断
  `exp.Insert/Update/Delete/Create/Drop/Alter/Merge/Copy` 拦写操作 —— 与本实现的
  「AST 判断最外层类型」思路一致。
- `backend/apps/chat/task/llm.py:71` `extract_tables_from_sql()`：**这就是本任务要复刻的
  CTE 排除逻辑**，先 `find_all(exp.CTE)` 收别名，再 `find_all(exp.Table)` 排除别名后取真实表名。
  调用处（同文件约 1326 行）拿真实表名和「授权表清单」做差集，做行 / 表级权限校验。

**提交 7118b40**（`fix: extract_tables_from_sql incorrectly includes CTE names as table
names`，issue #1278）修的正是：早期 `extract_tables_from_sql` 直接把所有 `exp.Table`
当真实表，导致 CTE 别名被误当物理表，权限校验时报「未授权表」。修复方式就是加上上面的
CTE 别名排除。本实现从一开始就按修复后的写法规避了这个坑。

---

## ④ 区别与取舍

- **校验失败返回中文错误、不抛异常**：`execute_sql` 是给 LLM 用的工具，返回值会回到模型
  上下文。返回「表 orders 不存在列 bad，可用列：…」这类带候选的中文报错，模型能据此改写
  SQL 自纠；抛异常会中断 Agent 循环，白白丢掉自纠机会。SQLBot 在服务端流程里是 `raise
  SingleMessageError`（面向后端异常处理），场景不同。
- **保留引擎级只读（mode=ro）双保险**：`validate_sql` 是「逻辑校验」，仍可能有 sqlglot 未
  覆盖的边角（新语法、方言差异）。`sqlite URI mode=ro` 是「物理兜底」，任何漏网写操作在
  引擎层直接失败。再加上最前面的 `_is_select_only` 关键词快筛，构成三层防护，任何一层都
  不单独可信。
- **schema 现取不缓存**：演示库可能刚被导入 / 替换，缓存会读到过期结构造成误报；而单库
  `sqlite_master + PRAGMA table_info` 开销极小。简单、不易错，优先于微优化。
- **列校验偏保守**：多表 / 子查询 / CTE 等无法唯一确定列归属的场景一律跳过，宁可漏报不
  误报 —— 误报会把合法 SQL 拦下、误导模型反复重写，代价高于漏报（漏报由引擎兜底）。
- **允许查 sqlite_master**：`execute_sql` 在 schema 里显式放行 `sqlite_master`，让模型能
  查表结构（文档承诺的用法），否则会被当未知表拦下。
