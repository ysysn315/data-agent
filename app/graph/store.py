"""知识图谱 - 三元组存储（SQLite 持久化 + NetworkX 内存镜像）

双层设计（对照 Yuxi 的 Neo4j+Milvus 双存储，取舍见 IMPLEMENTATION.md §3/§4）：
- 持久层：app/db 的 graph_triples 表，(s,p,o) 唯一约束 + 仓储判重保证入库幂等
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

from app.graph.extractor import Triple

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
    """三元组存储：graph_triples 表（幂等入库）+ NetworkX 内存镜像（惰性重建）。"""

    def __init__(self, repo, runner: Callable[[Coroutine], object], seed: bool = True):
        """
        Args:
            repo: app.db.repositories.GraphTripleRepository
            runner: sync→async 桥（运行时传 app.db.run_sync；测试注入独立事件循环 runner）
            seed: 首启表空时是否写入演示种子（与 TermStore 同策略：表非空即跳过，幂等）
        """
        self._repo = repo
        self._run = runner
        self._graph: nx.MultiDiGraph | None = None

        if seed and self._run(self._repo.count()) == 0:
            self.add_triples([{**t, "source": "seed"} for t in SEED_TRIPLES])
            logger.info(f"知识图谱首启灌种：{len(SEED_TRIPLES)} 条演示三元组")

    # ========== 写 ==========

    def add_triples(self, triples: Sequence[Triple | dict]) -> int:
        """三元组入库（(s,p,o) 幂等），返回实际新增条数；有新增才失效内存镜像。"""
        records = [t.to_dict() if isinstance(t, Triple) else dict(t) for t in triples]
        records = [r for r in records if r.get("subject") and r.get("predicate") and r.get("object")]
        if not records:
            return 0
        added = self._run(self._repo.add_many(records))
        if added:
            self._graph = None  # 写后失效：下次读图时从表全量重建
        return added

    # ========== 读 ==========

    def list_triples(self) -> list[dict]:
        return self._run(self._repo.list_all())

    def count(self) -> int:
        return self._run(self._repo.count())

    @property
    def graph(self) -> nx.MultiDiGraph:
        """NetworkX 内存镜像（惰性构建；节点=实体名，边带 predicate/source 属性）。"""
        if self._graph is None:
            g = nx.MultiDiGraph()
            for t in self.list_triples():
                # edge key 用谓词：同 (s,o) 的不同谓词共存，重复重建也不叠边
                g.add_edge(
                    t["subject"],
                    t["object"],
                    key=t["predicate"],
                    predicate=t["predicate"],
                    source=t["source"],
                )
            self._graph = g
        return self._graph
