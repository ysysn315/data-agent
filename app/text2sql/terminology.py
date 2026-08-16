"""业务术语库（TermStore）—— roadmap P1-4，SQLBot 术语库的轻量版。

业务黑话 → 统一口径 + 同义词映射：问题命中术语即把「定义 + SQL 口径提示」注入
prompt，让模型按公司口径算指标（如"复购率"该怎么算、GMV 用哪个字段），而不是各算各的。

命中规则：术语本身或任一同义词是问题的子串（对齐 SQLBot terminology 的 ILIKE 子串匹配，
见 backend/apps/terminology/curd/terminology.py:select_terminology_by_word）。
持久化：save_path 指向的 JSON 文件（原子写，与 ExampleStore 同思路）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from loguru import logger

# 内置种子：REQUIREMENTS §5.2 的三个核心口径，各配 1-2 个同义词与 SQL 口径提示。
SEED_TERMS: list[dict] = [
    {
        "term": "GMV",
        "synonyms": ["成交总额", "成交额", "销售额"],
        "definition": "成交总额，统计口径为订单商品金额之和（不含运费）。",
        "sql_hint": (
            "对 order_items.price 求和：SUM(oi.price)。按月趋势用 strftime('%Y-%m', order_purchase_timestamp) 分组。"
        ),
    },
    {
        "term": "复购率",
        "synonyms": ["回购率", "复购"],
        "definition": "购买 2 次以上的客户占比 = 下单次数≥2 的客户数 / 总客户数。",
        "sql_hint": "以 customers.customer_unique_id 聚合去重后的订单数 order_cnt，统计 order_cnt>=2 的客户占比。",
    },
    {
        "term": "客单价",
        "synonyms": ["平均订单金额", "单均价"],
        "definition": "客单价 = 总金额 / 订单数。",
        "sql_hint": "SUM(payment_value) / COUNT(DISTINCT order_id)，或按 order_items.price 汇总后除以订单数。",
    },
]


class TermStore:
    """术语库：JSON 持久化 + 术语/同义词子串命中。"""

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
            seed: 首次初始化（无持久化数据时）是否写入内置种子术语
            repo: 数据库存储后端（app.db.repositories.TerminologyRepository）。
                  传入即启用「DB 版」：术语从数据库读写，save_path 仅作历史 JSON 迁移源。
            runner: sync→async 桥（app.db.run_sync），DB 版必传。
        """
        self.save_path = Path(save_path) if save_path else None
        self._repo = repo
        self._run = runner
        self._terms: list[dict] = []
        loaded = self._load()

        if not loaded and seed:
            for item in SEED_TERMS:
                self._terms.append(self._normalize(item))
            self._save()

    # ========== 持久化 ==========

    def _load(self) -> bool:
        if self._repo is not None:
            return self._load_db()
        if self.save_path is None or not self.save_path.exists():
            return False
        try:
            data = json.loads(self.save_path.read_text(encoding="utf-8"))
            self._terms = [self._normalize(t) for t in data.get("terms", [])]
            logger.info(f"加载术语库: {len(self._terms)} 条")
            return True
        except Exception as e:
            raise ValueError(f"术语库解析失败 {self.save_path}: {e}")

    def _load_db(self) -> bool:
        """DB 版加载：表空且有历史 JSON 时一次性迁移入库，否则交由种子逻辑处理。"""
        rows = self._run(self._repo.list_all())
        if rows:
            self._terms = [self._normalize(t) for t in rows]
            return True
        if self.save_path is not None and self.save_path.exists():
            try:
                data = json.loads(self.save_path.read_text(encoding="utf-8"))
            except Exception as e:
                raise ValueError(f"术语库解析失败 {self.save_path}: {e}")
            legacy = [self._normalize(t) for t in data.get("terms", [])]
            if legacy:
                self._terms = legacy
                self._run(self._repo.replace_all(legacy))
                logger.info(f"术语库 JSON→DB 一次性迁移: {len(legacy)} 条")
                return True
        return False

    def _save(self) -> None:
        if self._repo is not None:
            # 整表替换，对应 JSON 版的整文件原子重写（术语规模小，代价可忽略）
            self._run(self._repo.replace_all(self._terms))
            return
        if self.save_path is None:
            return
        self.save_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"terms": self._terms}
        tmp = self.save_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(self.save_path)

    @staticmethod
    def _normalize(item: dict) -> dict:
        """统一词条结构，缺省字段补齐（旧数据无作用域字段 → 演示作用域）。"""
        return {
            "term": (item.get("term") or "").strip(),
            "synonyms": [s.strip() for s in (item.get("synonyms") or []) if s and s.strip()],
            "definition": (item.get("definition") or "").strip(),
            "sql_hint": (item.get("sql_hint") or "").strip() or None,
            "datasource_id": item.get("datasource_id"),
            "workspace_id": int(item.get("workspace_id") or 0),
        }

    @staticmethod
    def _in_scope(rec: dict, datasource_id: Optional[int], workspace_id: int = 0) -> bool:
        """作用域匹配：平台数据源按 (datasource_id, workspace)；演示库按 (NULL, workspace)。

        平台分支也比对 workspace——防御纵深：数据源归属虽由路由层校验
        （resolve_workspace 404），领域层不应依赖上层把关（CLI 直写等旁路没有这层闸）。
        """
        if datasource_id is not None:
            return rec.get("datasource_id") == datasource_id and int(rec.get("workspace_id") or 0) == int(
                workspace_id or 0
            )
        return rec.get("datasource_id") is None and int(rec.get("workspace_id") or 0) == int(workspace_id or 0)

    # ========== 增删查 ==========

    def add(
        self,
        term: str,
        synonyms: Optional[list[str]] = None,
        definition: str = "",
        sql_hint: Optional[str] = None,
        datasource_id: Optional[int] = None,
        workspace_id: int = 0,
    ) -> dict:
        """新增/更新一个术语——作用域内唯一：(term, datasource_id, workspace_id)。

        同一术语可在不同作用域各自存在（数据源 11 与 22 各配各的 GMV 口径），
        跨作用域互不覆盖；同作用域同 term 则覆盖更新。
        """
        if not term or not term.strip():
            raise ValueError("term 不能为空")

        rec = self._normalize(
            {
                "term": term,
                "synonyms": synonyms or [],
                "definition": definition,
                "sql_hint": sql_hint,
                "datasource_id": datasource_id,
                "workspace_id": workspace_id,
            }
        )

        for i, existing in enumerate(self._terms):
            if existing["term"] == rec["term"] and self._in_scope(existing, datasource_id, workspace_id):
                self._terms[i] = rec
                self._save()
                return rec

        self._terms.append(rec)
        self._save()
        return rec

    def list(self) -> list[dict]:
        return [dict(rec) for rec in self._terms]

    def delete(self, term: str, datasource_id: Optional[int] = None, workspace_id: int = 0) -> bool:
        """按 (term, 作用域) 删除，返回是否删掉。"""
        before = len(self._terms)
        self._terms = [
            t for t in self._terms if not (t["term"] == term and self._in_scope(t, datasource_id, workspace_id))
        ]
        if len(self._terms) == before:
            return False
        self._save()
        return True

    def match(self, question: str, datasource_id: Optional[int] = None, workspace_id: int = 0) -> list[dict]:
        """返回问题在指定作用域内命中的术语（term 或某个同义词是问题子串）。

        大小写不敏感（英文术语如 GMV 常被小写输入）。命中即返回该词条，供
        sql_context_search 注入 prompt；跨作用域词条不串入（平台数据源只看
        自己的口径，演示库按 workspace 隔离）。
        """
        if not question or not question.strip():
            return []

        q = question.lower()
        hits: list[dict] = []
        for rec in self._terms:
            if not self._in_scope(rec, datasource_id, workspace_id):
                continue
            candidates = [rec["term"], *rec["synonyms"]]
            if any(c and c.lower() in q for c in candidates):
                hits.append(dict(rec))
        return hits
