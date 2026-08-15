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
        repo=None,
        runner=None,
    ):
        """
        Args:
            save_path: 持久化 JSON 文件路径（None=纯内存，测试用）
            seed: 首次初始化（无持久化数据时）是否写入内置演示示例
            repo: 数据库存储后端（app.db.repositories.SQLExampleRepository）。
                  传入即启用「DB 版」：示例从数据库读写，save_path 仅作历史 JSON 迁移源。
            runner: sync→async 桥（app.db.run_sync），DB 版必传。
        """
        self.save_path = Path(save_path) if save_path else None
        self._repo = repo
        self._run = runner
        self._examples: list[dict] = []
        loaded = self._load()

        # 无历史数据时灌入种子；已有数据（哪怕被清空）则尊重现状，不重复灌种。
        if not loaded and seed:
            for item in SEED_EXAMPLES:
                self._examples.append(self._make_record(item["question"], item["sql"], True))
            self._save()

    # ========== 持久化 ==========

    def _load(self) -> bool:
        """读取持久化数据，返回是否成功加载到已有数据。"""
        if self._repo is not None:
            return self._load_db()
        if self.save_path is None or not self.save_path.exists():
            return False
        try:
            data = json.loads(self.save_path.read_text(encoding="utf-8"))
            self._examples = [self._normalize(r) for r in data.get("examples", [])]
            logger.info(f"加载 SQL 示例库: {len(self._examples)} 条")
            return True
        except Exception as e:
            # 文件损坏应显式暴露而不是静默清空（与 MCPService 一致）
            raise ValueError(f"SQL 示例库解析失败 {self.save_path}: {e}")

    def _load_db(self) -> bool:
        """DB 版加载：表空且有历史 JSON 时一次性迁移入库，否则交由种子逻辑处理。"""
        rows = self._run(self._repo.list_all())
        if rows:
            self._examples = [self._normalize(r) for r in rows]
            return True
        if self.save_path is not None and self.save_path.exists():
            try:
                data = json.loads(self.save_path.read_text(encoding="utf-8"))
            except Exception as e:
                raise ValueError(f"SQL 示例库解析失败 {self.save_path}: {e}")
            legacy = [self._normalize(r) for r in data.get("examples", [])]
            if legacy:
                self._examples = legacy
                self._run(self._repo.replace_all(legacy))
                logger.info(f"SQL 示例库 JSON→DB 一次性迁移: {len(legacy)} 条")
                return True
        return False

    def _save(self) -> None:
        if self._repo is not None:
            # 整表替换，对应 JSON 版的整文件原子重写（示例规模小，代价可忽略）
            self._run(self._repo.replace_all(self._examples))
            return
        if self.save_path is None:
            return
        self.save_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"examples": self._examples}
        tmp = self.save_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(self.save_path)

    @staticmethod
    def _make_record(
        question: str,
        sql: str,
        verified: bool,
        datasource_id: Optional[int] = None,
        workspace_id: int = 0,
        source: str = "manual",
        meta: Optional[dict] = None,
    ) -> dict:
        return {
            "id": uuid.uuid4().hex[:12],
            "question": question.strip(),
            "sql": sql.strip(),
            "verified": bool(verified),
            "datasource_id": datasource_id,
            "workspace_id": int(workspace_id or 0),
            "source": source,
            "meta": dict(meta or {}),
        }

    @staticmethod
    def _normalize(rec: dict) -> dict:
        """统一记录结构：旧数据（无作用域字段）缺省归入演示作用域（与 DB 默认值一致）。"""
        return {
            "id": rec.get("id") or uuid.uuid4().hex[:12],
            "question": (rec.get("question") or "").strip(),
            "sql": (rec.get("sql") or "").strip(),
            "verified": bool(rec.get("verified", True)),
            "datasource_id": rec.get("datasource_id"),
            "workspace_id": int(rec.get("workspace_id") or 0),
            "source": rec.get("source") or "manual",
            "meta": dict(rec.get("meta") or {}),
        }

    @staticmethod
    def _in_scope(rec: dict, datasource_id: Optional[int], workspace_id: int) -> bool:
        """作用域匹配：平台数据源按 datasource_id（租户内隔离，workspace 不参与）；
        演示库按 (NULL, workspace)——鉴权开启后不同 workspace 的演示库知识互不可见。"""
        if datasource_id is not None:
            return rec.get("datasource_id") == datasource_id
        return rec.get("datasource_id") is None and int(rec.get("workspace_id") or 0) == int(workspace_id or 0)

    # ========== 增删查 ==========

    def add(
        self,
        question: str,
        sql: str,
        verified: bool = True,
        datasource_id: Optional[int] = None,
        workspace_id: int = 0,
        source: str = "manual",
        meta: Optional[dict] = None,
    ) -> dict:
        """新增/更新一条示例（反馈接口：答对的 question→SQL 入库）。

        去重键是 (question, datasource_id, workspace_id)（演示作用域）：同一问题在不同
        作用域互不覆盖，平台数据源的示例不会把演示库同题示例顶掉，鉴权开启后不同
        workspace 的演示库同题示例也各自独立。verified=False 即候选（对话待确认 /
        评测失败导入），转正 = 再次 add 覆盖为 True。
        """
        if not question or not question.strip():
            raise ValueError("question 不能为空")
        if not sql or not sql.strip():
            raise ValueError("sql 不能为空")

        q = question.strip()
        rec = self._make_record(question, sql, verified, datasource_id, workspace_id, source, meta)
        for i, existing in enumerate(self._examples):
            if existing["question"] == q and self._in_scope(existing, datasource_id, workspace_id):
                # 覆盖时保留原 id，外部（前端列表）持有的引用不失效
                rec["id"] = existing["id"]
                self._examples[i] = rec
                self._save()
                return rec

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

    def search(
        self,
        question: str,
        top_k: int = 3,
        datasource_id: Optional[int] = None,
        workspace_id: int = 0,
        verified_only: bool = True,
    ) -> list[dict]:
        """按问题检索当前作用域内最相似的示例（jieba 词元重叠打分，降序取 top_k）。

        打分：候选示例 question 的词元集合与查询词元集合的交集大小。
        与 SkillService._tokenize 共用分词，保证中文切分口径一致。
        verified_only=True 时候选（未转正）示例不返回——few-shot 只注入已验证
        知识，候选经人工转正后才生效（防污染）；无重叠（score=0）不返回。
        """
        if not question or not question.strip():
            return []

        query_tokens = SkillService._tokenize(question)
        if not query_tokens:
            return []

        scored: list[tuple[dict, int]] = []
        for rec in self._examples:
            if verified_only and not rec.get("verified"):
                continue
            if not self._in_scope(rec, datasource_id, workspace_id):
                continue
            overlap = len(SkillService._tokenize(rec["question"]) & query_tokens)
            if overlap > 0:
                scored.append((rec, overlap))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [dict(rec) for rec, _ in scored[:top_k]]
