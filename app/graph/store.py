"""知识图谱 - 三元组存储（SQLite 持久化 + NetworkX 内存镜像）

双层设计（对照 Yuxi 的 Neo4j+Milvus 双存储，取舍见 IMPLEMENTATION.md §3/§4）：
- 持久层：app/db 的 graph_triples 表，作用域内唯一约束 + 仓储判重保证入库幂等
- 查询层：NetworkX MultiDiGraph 内存镜像 —— **惰性构建、写后失效**：
  首次读图时从表全量重建；任何有效写入把镜像置 None，下次读时再重建。
  规模假设是演示级图谱（千级三元组），全量重建 O(E) 毫秒级，简单正确优先，
  不做增量维护（增量在删除/并发下极易出现镜像与表不一致的隐性 bug）。

选 MultiDiGraph 而非 DiGraph：同一对实体允许多条不同谓词的边
（如 客单价-[计算自]->订单数 与未来可能的 客单价-[核算于]->订单数），
edge key 直接用谓词，重建天然幂等。
"""

from __future__ import annotations

from typing import Callable, Coroutine, Sequence

import networkx as nx
from loguru import logger

from app.graph.entities import merge_attributes, normalize_entity_name
from app.graph.extractor import Triple
from app.graph.scope import GraphScope, current_graph_scope

# 首启种子：演示库（Kaggle Brazilian E-Commerce）业务图谱。
# 指标口径与 app/text2sql/terminology.SEED_TERMS 对齐（GMV / 复购率 / 客单价）：
# 术语库回答"指标怎么算"，图谱回答"指标沿什么链路算出来"（口径溯源）。
SEED_TRIPLES: list[dict] = [
    # 实体归属（外键方向：子表 -[属于]-> 主表）
    {"subject": "订单", "predicate": "属于", "object": "客户"},
    {"subject": "订单项", "predicate": "属于", "object": "订单"},
    {"subject": "支付记录", "predicate": "属于", "object": "订单"},
    {"subject": "评价", "predicate": "属于", "object": "订单"},
    {"subject": "订单项", "predicate": "关联", "object": "商品"},
    {"subject": "订单项", "predicate": "关联", "object": "卖家"},
    {"subject": "商品", "predicate": "属于", "object": "品类"},
    # GMV：SUM(order_items.price)，按月用 order_purchase_timestamp（对齐术语库 sql_hint）
    {"subject": "GMV", "predicate": "计算自", "object": "订单项价格"},
    {"subject": "订单项价格", "predicate": "属于", "object": "订单项"},
    {"subject": "GMV", "predicate": "按月分组于", "object": "下单时间"},
    {"subject": "下单时间", "predicate": "属于", "object": "订单"},
    # 复购率：以 customer_unique_id 去重统计（对齐术语库 sql_hint）
    {"subject": "复购率", "predicate": "统计自", "object": "客户唯一标识"},
    {"subject": "客户唯一标识", "predicate": "属于", "object": "客户"},
    # 客单价：SUM(payment_value) / COUNT(DISTINCT order_id)（对齐术语库 sql_hint）
    {"subject": "客单价", "predicate": "计算自", "object": "支付金额"},
    {"subject": "客单价", "predicate": "计算自", "object": "订单数"},
    {"subject": "支付金额", "predicate": "属于", "object": "支付记录"},
    {"subject": "订单数", "predicate": "统计自", "object": "订单"},
]


class GraphStore:
    """作用域三元组存储：SQLite 持久化 + NetworkX 惰性镜像。"""

    def __init__(
        self,
        repo,
        runner: Callable[[Coroutine], object],
        seed: bool = True,
        entity_repo=None,
        scope: GraphScope | None = None,
    ):
        """
        Args:
            repo: app.db.repositories.GraphTripleRepository
            runner: sync→async 桥（运行时传 app.db.run_sync；测试注入独立事件循环 runner）
            seed: 仅在演示 workspace:0 作用域为空时写入种子
        """
        self._repo = repo
        self._run = runner
        self._entity_repo = entity_repo
        if self._entity_repo is None and hasattr(repo, "_sm"):
            from app.db.repositories import GraphEntityRepository

            self._entity_repo = GraphEntityRepository(repo._sm)
        self._default_scope = scope or GraphScope()
        self._graph: nx.MultiDiGraph | None = None
        self._graph_scope_key: str | None = None

        if seed and self._run(self._repo.count(self._default_scope.key)) == 0:
            self.add_triples([{**t, "source": "seed"} for t in SEED_TRIPLES], scope=self._default_scope)
            logger.info(f"知识图谱首启灌种：{len(SEED_TRIPLES)} 条演示三元组")

    # ========== 写 ==========

    def _scope(self, scope: GraphScope | None) -> GraphScope:
        # 直接使用 GraphStore 的调用（例如导入任务或测试）也应遵循请求
        # 上下文；显式传入 scope 仍然拥有更高优先级。
        return scope or current_graph_scope() or self._default_scope

    def _entity_records(self, records: list[dict], scope: GraphScope) -> list[dict]:
        names: dict[str, dict] = {}
        for record in records:
            for field in ("subject", "object"):
                name = str(record.get(field) or "").strip()
                if not name:
                    continue
                key = normalize_entity_name(name)
                source = record.get("source") or "manual"
                incoming_aliases = record.get(f"{field}_aliases") or []
                if not isinstance(incoming_aliases, (list, tuple, set)):
                    incoming_aliases = [incoming_aliases]
                incoming_attributes = record.get(f"{field}_attributes") or {}
                if not isinstance(incoming_attributes, dict):
                    incoming_attributes = {}
                entry = names.get(key)
                if entry is None:
                    names[key] = {
                        "canonical_name": name,
                        "normalized_name": key,
                        "scope_key": scope.key,
                        "workspace_id": scope.workspace_id,
                        "datasource_id": scope.datasource_id,
                        "entity_type": record.get(f"{field}_type") or "unknown",
                        "aliases": list(dict.fromkeys(incoming_aliases)),
                        "attributes": dict(incoming_attributes),
                        "source": source,
                    }
                else:
                    entry["aliases"] = list(dict.fromkeys([*entry["aliases"], *incoming_aliases]))
                    entry["attributes"] = merge_attributes(entry["attributes"], incoming_attributes, source)
                    if entry["entity_type"] == "unknown" and record.get(f"{field}_type"):
                        entry["entity_type"] = record[f"{field}_type"]
        return list(names.values())

    def add_triples(self, triples: Sequence[Triple | dict], scope: GraphScope | None = None) -> int:
        """三元组入库（作用域内幂等），同时确保端点实体存在。"""
        scope = self._scope(scope)
        records = [t.to_dict() if isinstance(t, Triple) else dict(t) for t in triples]
        records = [r for r in records if r.get("subject") and r.get("predicate") and r.get("object")]
        if not records:
            return 0
        entity_rows = self._entity_records(records, scope)
        entities = self._run(self._entity_repo.upsert_many(entity_rows)) if self._entity_repo else []
        entity_by_name = {e["normalized_name"]: e for e in entities}
        for record in records:
            record.update(
                scope_key=scope.key,
                workspace_id=scope.workspace_id,
                datasource_id=scope.datasource_id,
                subject_entity_id=entity_by_name.get(normalize_entity_name(record["subject"]), {}).get("id"),
                object_entity_id=entity_by_name.get(normalize_entity_name(record["object"]), {}).get("id"),
            )
        added = self._run(self._repo.add_many(records, scope_key=scope.key))
        if added:
            self._graph = None  # 写后失效：下次读该作用域时从表全量重建
            self._graph_scope_key = None
        return added

    # ========== 读 ==========

    def list_triples(self, scope: GraphScope | None = None) -> list[dict]:
        return self._run(self._repo.list_all(self._scope(scope).key))

    def count(self, scope: GraphScope | None = None) -> int:
        return self._run(self._repo.count(self._scope(scope).key))

    def merge_entities(self, survivor_id: int, duplicate_id: int, scope: GraphScope | None = None) -> dict:
        scope = self._scope(scope)
        if not self._entity_repo:
            raise ValueError("图谱未配置实体仓储")
        result = self._run(self._entity_repo.merge_entities(scope.key, survivor_id, duplicate_id))
        self._graph = None
        self._graph_scope_key = None
        return result

    def graph_for(self, scope: GraphScope | None = None) -> nx.MultiDiGraph:
        """返回作用域内 NetworkX 镜像；不同作用域不共享节点和边。"""
        scope = self._scope(scope)
        if self._graph is None or self._graph_scope_key != scope.key:
            g = nx.MultiDiGraph()
            for t in self.list_triples(scope):
                # edge key 用谓词：同 (s,o) 的不同谓词共存，重复重建也不叠边
                g.add_edge(
                    t["subject"],
                    t["object"],
                    key=t["predicate"],
                    predicate=t["predicate"],
                    source=t["source"],
                    source_ref=t.get("source_ref"),
                    provenance=t.get("provenance") or {},
                )
            self._graph = g
            self._graph_scope_key = scope.key
        return self._graph

    @property
    def graph(self) -> nx.MultiDiGraph:
        """兼容旧调用：返回默认演示作用域镜像。"""
        return self.graph_for(self._default_scope)

    @property
    def entity_repo(self):
        return self._entity_repo
