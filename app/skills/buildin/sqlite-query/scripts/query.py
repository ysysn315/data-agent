#!/usr/bin/env python3
"""sqlite-query 技能随附脚本：只读执行 SELECT 并输出 JSON

用法:
    python query.py --db <path> --sql "SELECT ..." [--limit 100]

仅用标准库，无第三方依赖。
"""
import argparse
import json
import re
import sqlite3
import sys

MAX_LIMIT = 1000
_COMMENT_PATTERN = re.compile(r"(--[^\n]*|/\*.*?\*/)", re.DOTALL)


def is_select_only(sql: str) -> bool:
    stripped = _COMMENT_PATTERN.sub(" ", sql).strip().rstrip(";").strip()
    if ";" in stripped:
        return False
    first_word = stripped.split(None, 1)[0].lower() if stripped else ""
    return first_word in ("select", "with")


def main() -> int:
    parser = argparse.ArgumentParser(description="只读执行 SQLite SELECT")
    parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    parser.add_argument("--sql", required=True, help="SELECT 语句")
    parser.add_argument("--limit", type=int, default=100, help="最大返回行数")
    args = parser.parse_args()

    if not is_select_only(args.sql):
        print(json.dumps({"error": "仅允许单条 SELECT / WITH 查询"}, ensure_ascii=False))
        return 1

    limit = max(1, min(args.limit, MAX_LIMIT))
    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
        try:
            cursor = conn.execute(args.sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(limit)
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(json.dumps({"error": f"SQL 执行失败: {e}"}, ensure_ascii=False))
        return 1

    print(json.dumps(
        {"columns": columns, "rows": [list(r) for r in rows], "row_count": len(rows)},
        ensure_ascii=False,
        default=str,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
