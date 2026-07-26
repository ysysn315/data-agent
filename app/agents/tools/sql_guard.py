# SQL 校验层（roadmap P1-1）—— 基于 sqlglot 的 AST 级只读校验与自动 LIMIT
#
# 设计目标：在 execute_sql 真正执行前，用 sqlglot 把 SQL 解析成 AST，做四件事：
#   1. 语法校验（解析失败 → 带位置信息的中文报错，让模型能定位）
#   2. 只读与单语句校验（AST 层判断最外层是不是 SELECT / WITH ... SELECT，
#      而不是拿字符串首词猜 —— 例如 `WITH x AS (...) INSERT ...` 首词是 WITH，
#      但 AST 最外层是 Insert，必须拦下）
#   3. 表名 / 列名校验（schema 已知时，未知表、未知列直接报错并列出候选，
#      模型据此自纠）—— CTE 名必须排除，见下方 _extract_real_tables
#   4. 最外层缺 LIMIT 时在 AST 层自动补 LIMIT，fixed_sql 由 sqlglot 回写生成
#
# 为什么返回中文错误而不是抛异常：execute_sql 是给 LLM 用的工具，错误文案会回到
# 模型上下文，模型据此重写 SQL 自纠。抛异常会中断 Agent 循环，拿不到自纠机会。
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ParseError

# sqlglot 报错里带的 ANSI 颜色转义（高亮出错 token），回给模型前先清掉
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")

# 允许的最外层查询类型：SELECT 及其派生（UNION / INTERSECT / EXCEPT 都是只读集合运算）。
# `WITH ... SELECT` 经 sqlglot 解析后最外层就是 Select（with 挂在 args 上），
# 而 `WITH ... INSERT` 最外层是 Insert，不在此列 —— 这正是 AST 判断优于字符串首词之处。
_ALLOWED_ROOT = (exp.Select, exp.Union)


@dataclass
class GuardResult:
    """SQL 校验结果。

    ok=True 时 fixed_sql 为可执行的规范化 SQL（可能已自动补 LIMIT）；
    ok=False 时 error 为中文错误信息（含位置 / 候选表列，供模型自纠）。
    """

    ok: bool
    fixed_sql: str | None = None
    error: str | None = None


def _clean_parse_error(err: ParseError) -> str:
    """把 sqlglot 的解析错误压成一行中文可读信息（保留行列位置）。"""
    msg = _ANSI_PATTERN.sub("", str(err))
    # sqlglot 的报错常是多行（含 SQL 片段与 ^ 指示），取首行关键信息即可
    first_line = msg.strip().splitlines()[0] if msg.strip() else "语法错误"
    return first_line


def _extract_real_tables(root: exp.Expression) -> set[str]:
    """提取 SQL 真正引用的物理表名，**排除 CTE 别名**。

    这是 SQLBot 提交 7118b40 修的坑：`WITH cte AS (...) SELECT * FROM cte` 里的
    `cte` 是 CTE 别名，不是真实表，早期实现把它当未知表误报。做法与 SQLBot 一致
    （backend/apps/chat/task/llm.py:extract_tables_from_sql）：先收集所有 CTE 别名，
    再遍历 exp.Table，名字落在 CTE 别名集合里的一律跳过。
    """
    cte_names = {cte.alias for cte in root.find_all(exp.CTE) if cte.alias}
    tables: set[str] = set()
    for table in root.find_all(exp.Table):
        if table.name and table.name not in cte_names:
            tables.add(table.name)
    return tables


def _build_alias_map(root: exp.Expression, cte_names: set[str]) -> dict[str, str]:
    """构造 {别名或表名(小写) -> 真实表名} 映射，仅收录物理表（跳过 CTE）。

    派生表（子查询）在 sqlglot 里是 exp.Subquery 而非 exp.Table，天然不会进这个
    映射 —— 因此对 `SELECT d.x FROM (...) d` 里的限定列 d.x，别名解析不到物理表，
    会被安全跳过，不会误报。
    """
    alias_map: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        if not table.name or table.name in cte_names:
            continue
        key = (table.alias or table.name).lower()
        alias_map[key] = table.name
    return alias_map


def _check_columns(
    root: exp.Expression,
    real_tables: set[str],
    cte_names: set[str],
    schema_ci: dict[str, tuple[str, dict[str, str]]],
) -> str | None:
    """列名校验。仅在能可靠定位到某张已知物理表时才校验，避免误报。

    - 限定列 `t.col`：把 t 解析为物理表，若该表已知且不含 col → 报错。
    - 非限定列 `col`：仅当整条 SQL 只引用了一张物理表、且无 CTE、无子查询
      （无法判断列来自哪个作用域）时，才against 该表校验。
    schema_ci: {表名小写: (原始表名, {列名小写: 原始列名})}
    """
    alias_map = _build_alias_map(root, cte_names)

    # 判断是否"简单"到可以做非限定列推断：只有一层 SELECT、没有子查询、没有 CTE
    select_count = len(list(root.find_all(exp.Select)))
    has_subquery = any(True for _ in root.find_all(exp.Subquery))
    is_simple = select_count <= 1 and not has_subquery and not cte_names
    single_table = next(iter(real_tables)) if len(real_tables) == 1 else None

    for column in root.find_all(exp.Column):
        col_name = column.name
        if not col_name:
            continue
        qualifier = column.table  # 限定符（表名或别名），无则为 ""

        target_table: str | None = None
        if qualifier:
            resolved = alias_map.get(qualifier.lower())
            if resolved is None:
                continue  # 限定符指向 CTE / 派生表 / 未知作用域，跳过不误报
            target_table = resolved
        elif is_simple and single_table is not None:
            target_table = single_table
        else:
            continue  # 无限定符且无法唯一定位，跳过

        entry = schema_ci.get(target_table.lower())
        if entry is None:
            continue  # 表本身未知，交给表名校验去报，这里不重复
        _orig_table, cols_ci = entry
        if col_name.lower() not in cols_ci:
            avail = "、".join(cols_ci.values())
            return (
                f"表 {entry[0]} 不存在列「{col_name}」。"
                f"该表可用列：{avail}"
            )
    return None


def validate_sql(
    sql: str,
    schema: dict[str, list[str]] | None = None,
    default_limit: int = 1000,
) -> GuardResult:
    """校验并规范化一条 SQLite 只读查询。

    参数:
        sql: 待校验的 SQL 文本。
        schema: {表名: [列名, ...]}，提供时校验表 / 列是否存在；None 则跳过该项。
        default_limit: 最外层无 LIMIT 时自动补的行数上限。

    返回:
        GuardResult。ok=True 时 fixed_sql 可直接执行（可能已补 LIMIT）；
        ok=False 时 error 为中文报错，包含语法位置或候选表 / 列，供模型自纠。
    """
    if not sql or not sql.strip():
        return GuardResult(ok=False, error="SQL 为空")

    # 1. 语法解析（多语句会被拆成多条，便于单语句校验）
    try:
        statements = sqlglot.parse(sql, dialect="sqlite")
    except ParseError as e:
        return GuardResult(ok=False, error=f"SQL 语法错误：{_clean_parse_error(e)}")

    statements = [s for s in statements if s is not None]
    if not statements:
        return GuardResult(ok=False, error="SQL 语法错误：未解析到任何语句")

    # 2. 单语句约束
    if len(statements) > 1:
        return GuardResult(
            ok=False,
            error=f"只允许执行单条查询，但解析到 {len(statements)} 条语句（禁止分号分隔的多语句）",
        )

    root = statements[0]

    # 3. 只读约束：AST 层判断最外层是不是查询（SELECT / WITH ... SELECT / 集合运算）
    if not isinstance(root, _ALLOWED_ROOT):
        kind = type(root).__name__.upper()
        return GuardResult(
            ok=False,
            error=f"只允许 SELECT / WITH 查询（禁止 {kind} 等写操作）",
        )

    # 4. 表名 / 列名校验（schema 已知时）
    real_tables = _extract_real_tables(root)
    if schema is not None:
        # 大小写不敏感索引：{表名小写: (原表名, {列名小写: 原列名})}
        schema_ci: dict[str, tuple[str, dict[str, str]]] = {
            t.lower(): (t, {c.lower(): c for c in cols}) for t, cols in schema.items()
        }
        cte_names = {cte.alias for cte in root.find_all(exp.CTE) if cte.alias}

        unknown = [t for t in real_tables if t.lower() not in schema_ci]
        if unknown:
            avail = "、".join(schema.keys()) if schema else "（无）"
            return GuardResult(
                ok=False,
                error=f"引用了未知表：{'、'.join(unknown)}。可用表：{avail}",
            )

        col_err = _check_columns(root, real_tables, cte_names, schema_ci)
        if col_err:
            return GuardResult(ok=False, error=col_err)

    # 5. 自动补 LIMIT（仅当最外层没有 LIMIT 时；有则原样保留）
    if root.args.get("limit") is None:
        root = root.limit(default_limit)

    fixed_sql = root.sql(dialect="sqlite")
    return GuardResult(ok=True, fixed_sql=fixed_sql)
