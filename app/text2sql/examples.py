"""SQL 示例库（ExampleStore）—— roadmap P1-3「越问越准」的运营闭环。

存 question→SQL 对，few-shot 注入 prompt；答对的问答经反馈接口入库，示例越攒越多、
生成越来越准。检索用 jieba 词元重叠打分（与 app/skills/service.py 的 _tokenize 对齐），
几十条量级足够；示例上千后再换 app/rag 向量召回（见 IMPLEMENTATION-knowledge.md）。

持久化：save_path 指向的 JSON 文件（原子写，参考 app/mcp/service.py 的注册表写法）。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Optional

from loguru import logger

from app.skills.service import SkillService

# 内置演示示例：均针对 Kaggle Brazilian E-Commerce 演示库、真实可执行（见 tests 校验）。
# verified=True 表示"人工确认过结果正确"，可放心 few-shot 注入。
SEED_EXAMPLES: list[dict] = [
    {
        "question": "各州的客户数量分布",
        "sql": (
            "SELECT customer_state, COUNT(*) AS customer_count "
            "FROM customers GROUP BY customer_state "
            "ORDER BY customer_count DESC LIMIT 1000"
        ),
    },
    {
        "question": "销售额最高的前 10 个商品类目",
        "sql": (
            "SELECT p.product_category_name AS category, ROUND(SUM(oi.price), 2) AS gmv "
            "FROM order_items oi JOIN products p ON oi.product_id = p.product_id "
            "GROUP BY p.product_category_name ORDER BY gmv DESC LIMIT 10"
        ),
    },
    {
        "question": "各支付方式的订单数量分布",
        "sql": (
            "SELECT payment_type, COUNT(*) AS order_count "
            "FROM payments GROUP BY payment_type "
            "ORDER BY order_count DESC LIMIT 1000"
        ),
    },
    {
        "question": "2018 年各月的成交金额（GMV）趋势",
        "sql": (
            "SELECT strftime('%Y-%m', o.order_purchase_timestamp) AS month, "
            "ROUND(SUM(oi.price), 2) AS gmv "
            "FROM orders o JOIN order_items oi ON o.order_id = oi.order_id "
            "WHERE o.order_purchase_timestamp >= '2018-01-01' "
            "AND o.order_purchase_timestamp < '2019-01-01' "
            "GROUP BY month ORDER BY month LIMIT 1000"
        ),
    },
    {
        "question": "复购率：购买 2 次以上的客户占比",
        "sql": (
            "WITH order_counts AS ("
            "SELECT c.customer_unique_id AS uid, COUNT(DISTINCT o.order_id) AS order_cnt "
            "FROM customers c JOIN orders o ON c.customer_id = o.customer_id "
            "GROUP BY c.customer_unique_id) "
            "SELECT ROUND(100.0 * SUM(CASE WHEN order_cnt >= 2 THEN 1 ELSE 0 END) "
            "/ COUNT(*), 2) AS repurchase_rate_pct FROM order_counts"
        ),
    },
]


class ExampleStore:
    """SQL 示例库：JSON 持久化 + jieba 词元重叠检索 + 反馈入库。"""

    def __init__(
        self,
        save_path: Optional[str | Path] = None,
        seed: bool = True,
    ):
        """
        Args:
            save_path: 持久化 JSON 文件路径（None=纯内存，测试用）
            seed: 首次初始化（无持久化文件时）是否写入内置演示示例
        """
        self.save_path = Path(save_path) if save_path else None
        self._examples: list[dict] = []
        loaded = self._load()

        # 无历史文件时灌入种子；已有文件（哪怕被清空）则尊重现状，不重复灌种。
        if not loaded and seed:
            for item in SEED_EXAMPLES:
                self._examples.append(self._make_record(item["question"], item["sql"], True))
            self._save()

    # ========== 持久化 ==========

    def _load(self) -> bool:
        """读取持久化文件，返回是否成功从文件加载。"""
        if self.save_path is None or not self.save_path.exists():
            return False
        try:
            data = json.loads(self.save_path.read_text(encoding="utf-8"))
            self._examples = list(data.get("examples", []))
            logger.info(f"加载 SQL 示例库: {len(self._examples)} 条")
            return True
        except Exception as e:
            # 文件损坏应显式暴露而不是静默清空（与 MCPService 一致）
            raise ValueError(f"SQL 示例库解析失败 {self.save_path}: {e}")

    def _save(self) -> None:
        if self.save_path is None:
            return
        self.save_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"examples": self._examples}
        tmp = self.save_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(self.save_path)

    @staticmethod
    def _make_record(question: str, sql: str, verified: bool) -> dict:
        return {
            "id": uuid.uuid4().hex[:12],
            "question": question.strip(),
            "sql": sql.strip(),
            "verified": bool(verified),
        }

    # ========== 增删查 ==========

    def add(self, question: str, sql: str, verified: bool = True) -> dict:
        """新增一条示例（反馈接口：答对的 question→SQL 入库）。

        同一 question 已存在时视为更新（覆盖 SQL 与 verified），避免重复堆积。
        """
        if not question or not question.strip():
            raise ValueError("question 不能为空")
        if not sql or not sql.strip():
            raise ValueError("sql 不能为空")

        q = question.strip()
        for rec in self._examples:
            if rec["question"] == q:
                rec["sql"] = sql.strip()
                rec["verified"] = bool(verified)
                self._save()
                return rec

        rec = self._make_record(question, sql, verified)
        self._examples.append(rec)
        self._save()
        return rec

    def list(self) -> list[dict]:
        """返回全部示例（副本，避免外部误改内部状态）。"""
        return [dict(rec) for rec in self._examples]

    def delete(self, example_id: str) -> bool:
        """按 id 删除，返回是否删掉。"""
        before = len(self._examples)
        self._examples = [r for r in self._examples if r["id"] != example_id]
        if len(self._examples) == before:
            return False
        self._save()
        return True

    def search(self, question: str, top_k: int = 3) -> list[dict]:
        """按问题检索最相似的示例（jieba 词元重叠打分，降序取 top_k）。

        打分：候选示例 question 的词元集合与查询词元集合的交集大小。
        与 SkillService._tokenize 共用分词，保证中文切分口径一致。
        无重叠（score=0）的示例不返回；示例上千后应换向量召回。
        """
        if not question or not question.strip():
            return []

        query_tokens = SkillService._tokenize(question)
        if not query_tokens:
            return []

        scored: list[tuple[dict, int]] = []
        for rec in self._examples:
            overlap = len(SkillService._tokenize(rec["question"]) & query_tokens)
            if overlap > 0:
                scored.append((rec, overlap))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [dict(rec) for rec, _ in scored[:top_k]]
