"""Text-to-SQL 执行准确率评估 —— 结果集归一化与对比（纯函数，可独立测试）。

评估口径是 **execution accuracy（执行准确率）**：不比 SQL 文本，只比"把 golden SQL
和模型生成 SQL 分别在同一个库上跑出来的结果集是否等价"。这样能容忍写法差异
（别名不同、JOIN 顺序不同、子查询 vs. CTE），只要答案对就算对。

等价判定的三条归一化规则（对应 roadmap P1-2 的验收口径）：

1. **列序无关**：`SELECT state, gmv` 与 `SELECT gmv, state` 视为等价。实现上把每一行
   的单元格按稳定 key 排序后再比较 —— 交换列只是交换了行内单元格的顺序。
2. **行序**：仅当 golden SQL **没有** ORDER BY 时才忽略行序（无序结果集，行的先后
   没有业务含义，按多重集合比较）；golden 显式写了 ORDER BY 时，说明"排序"本身是
   题目的一部分，必须按顺序逐行比较。
3. **浮点容差**：金额/均值这类浮点列，把单元格量化到固定小数位（默认 2 位）再比较，
   吸收 `ROUND(...)` 与原始浮点、以及浮点累加误差带来的末位抖动。

为什么把"列序无关"做成"行内单元格排序"而不是按列名对齐：evals 只拿到结果集的
values（execute 返回的 rows），不保证 golden 与预测的列名一致（模型常给列换个别名）。
按值归一是最稳的口径 —— 代价是极端情况下 `(1,2)` 与 `(2,1)` 会判等，对本 demo 的
业务查询可接受（IMPLEMENTATION.md 的"取舍"一节有说明）。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Sequence

import sqlglot
from sqlglot import expressions as exp

# 默认浮点量化小数位：演示库金额多为两位小数，SUM/AVG 也按两位对齐足够。
DEFAULT_FLOAT_DECIMALS = 2

# 单元格类型分档，保证异构行（None / 数值 / 文本混排）能有稳定排序 key。
_RANK_NONE = 0
_RANK_NUM = 1
_RANK_STR = 2


def _cell_key(value: Any, decimals: int) -> tuple[int, Any]:
    """把单元格映射成 (类型档位, 归一值) 的可比较、可排序 key。

    - None → (0, "")
    - 数值（int/float，含 bool）→ (1, round(float, decimals))：统一成量化浮点，
      吸收整数/浮点、ROUND/未 ROUND 的差异。
    - 其余（文本等）→ (2, str(value))
    类型档位在前，确保 None/数值/文本之间有确定顺序，不会因类型不可比抛异常。
    """
    if value is None:
        return (_RANK_NONE, "")
    if isinstance(value, bool):
        # bool 是 int 子类，显式归到数值档，避免 True/1 判定歧义
        return (_RANK_NUM, round(float(value), decimals))
    if isinstance(value, (int, float)):
        return (_RANK_NUM, round(float(value), decimals))
    return (_RANK_STR, str(value))


def _row_key(row: Iterable[Any], decimals: int) -> tuple[tuple[int, Any], ...]:
    """把一行归一成"行内单元格排序后的 key 元组"，从而实现列序无关。"""
    return tuple(sorted(_cell_key(c, decimals) for c in row))


def golden_has_order_by(sql: str) -> bool:
    """判断 golden SQL 的**最外层**查询是否带 ORDER BY。

    只看最外层：子查询/CTE 内部的 ORDER BY 不决定最终结果集的行序
    （对最终输出无序而言，内部排序无意义）。解析失败时保守返回 True
    （当作有序，比较更严格，避免把该判错的例子放过）。
    """
    try:
        root = sqlglot.parse_one(sql, dialect="sqlite")
    except Exception:  # noqa: BLE001 —— 解析失败按"有序"从严处理
        return True
    if root is None:
        return True
    # UNION 等集合运算取其最外层；ORDER BY 作为 arg 挂在最外层表达式上
    return root.args.get("order") is not None


def normalize_result_set(
    rows: Sequence[Sequence[Any]],
    order_sensitive: bool,
    decimals: int = DEFAULT_FLOAT_DECIMALS,
):
    """把结果集归一成可直接 == 比较的结构。

    - order_sensitive=True：返回按原行序排列的 list（逐行比较，行序敏感）。
    - order_sensitive=False：返回行 key 的多重集合（Counter，行序无关但计重）。
    两种情况都已对列序（行内排序）和浮点（量化）做过归一。
    """
    row_keys = [_row_key(r, decimals) for r in rows]
    if order_sensitive:
        return row_keys
    return Counter(row_keys)


def compare_result_sets(
    golden_rows: Sequence[Sequence[Any]],
    pred_rows: Sequence[Sequence[Any]],
    order_sensitive: bool,
    decimals: int = DEFAULT_FLOAT_DECIMALS,
) -> bool:
    """比较两个结果集是否等价（execution accuracy 的核心判定）。

    order_sensitive 通常由 golden_has_order_by(golden_sql) 得出。
    """
    return normalize_result_set(golden_rows, order_sensitive, decimals) == \
        normalize_result_set(pred_rows, order_sensitive, decimals)


def build_report(case_results: list[dict]) -> dict:
    """把逐例结果聚合成报告字典：总分 + 按 tags 分桶 + 每例明细。

    每个 case_result 至少包含：
        id: str
        tags: list[str]
        correct: bool          —— 是否判为执行等价
    可选（原样带入 cases 明细）：question / golden_sql / pred_sql / error / ...

    一个用例可带多个 tag，会在每个所属桶里各计一次（分桶用于定位"哪类查询最弱"，
    不要求各桶之和等于总数）。
    """
    total = len(case_results)
    correct = sum(1 for c in case_results if c.get("correct"))

    by_tag: dict[str, dict] = {}
    for c in case_results:
        for tag in c.get("tags", []):
            bucket = by_tag.setdefault(tag, {"total": 0, "correct": 0})
            bucket["total"] += 1
            if c.get("correct"):
                bucket["correct"] += 1
    for bucket in by_tag.values():
        bucket["accuracy"] = round(bucket["correct"] / bucket["total"], 4) if bucket["total"] else 0.0

    return {
        "summary": {
            "total": total,
            "correct": correct,
            "accuracy": round(correct / total, 4) if total else 0.0,
        },
        "by_tag": dict(sorted(by_tag.items())),
        "cases": case_results,
    }
