# 知识图谱查询工具 —— knowledge-graph 技能声明的门控工具（激活后可见）。
# 把实体邻居子图文本化成 `主语 -[谓词]-> 宾语` 行，供模型做口径溯源与关联分析。
from __future__ import annotations

from langchain_core.tools import tool

from app.graph.service import GraphService


def create_graph_search_tool(graph_service: GraphService):
    """创建绑定到 GraphService 的图谱检索工具。

    输出面向模型可读：每条边一行 `A -[谓词]-> B`，箭头方向即三元组方向。
    实体名未精确命中时给出包含该子串的候选实体，模型可据此二次调用，
    避免一次失配就放弃图谱这条线索。
    """

    @tool
    def graph_search(entity: str, depth: int = 1) -> str:
        """查询业务知识图谱中某实体的邻居关系（指标口径溯源 / 实体关联分析）。

        返回以该实体为中心 depth 跳内的全部关系，每行一条 `主语 -[谓词]-> 宾语`，
        例如查 GMV 会得到 `GMV -[计算自]-> 订单项价格`。实体名需精确命中；
        未命中时会提示相近实体名，请换用提示的名称重查。

        参数:
            entity: 实体或指标名（如 GMV、订单、复购率）
            depth: 邻居深度，默认 1（直接相邻）；2 可看两跳间接关联
        """
        name = (entity or "").strip()
        if not name:
            return "实体名不能为空。"

        result = graph_service.query_entity(name, depth=depth)
        if result is None:
            candidates = graph_service.suggest_entities(name)
            if candidates:
                return f"图谱中不存在实体「{name}」。相近实体：{'、'.join(candidates)}（请用精确名称重查）"
            return f"图谱中不存在实体「{name}」，也没有相近实体（图谱可能未覆盖该概念）。"

        lines = [f"实体「{name}」的 {result['depth']} 跳邻居子图（{len(result['edges'])} 条关系）："]
        lines.extend(f"{e['subject']} -[{e['predicate']}]-> {e['object']}" for e in result["edges"])
        return "\n".join(lines)

    return graph_search


def create_graph_path_tool(graph_service: GraphService):
    """创建 Agent 直接调用的作用域路径查询工具。

    工具不接受 workspace/datasource 参数，GraphService 从请求级 GraphScope 读取；
    Embedding 解析失败时仍会回到精确名称、别名和子串候选。
    """

    @tool
    async def graph_path_search(from_entity: str, to_entity: str, max_hops: int = 3) -> str:
        """查询两个业务实体之间的关系路径。

        适用于“GMV 和客户有什么关系”“这个指标沿哪些表字段计算”等问题。
        如果实体名称存在歧义，返回候选列表；不要在候选不明确时自行猜测。
        """

        source = (from_entity or "").strip()
        target = (to_entity or "").strip()
        if not source or not target:
            return "起点和终点实体都不能为空。"
        result = await graph_service.find_path_resolved(source, target, max_hops=max(1, min(int(max_hops), 5)))
        if result.get("status") == "ambiguous":
            candidates = result.get("candidates", {})
            return (
                f"实体解析存在歧义：起点候选={candidates.get('from', [])}，"
                f"终点候选={candidates.get('to', [])}。请先向用户确认。"
            )
        if result.get("status") == "missing":
            return f"图谱中不存在实体：{'、'.join(result.get('missing', []))}。"
        if not result.get("found"):
            return (
                f"实体已解析，但 {source} 与 {target} 在 {result.get('scope', '')} 内没有不超过 {max_hops} 跳的路径。"
            )
        lines = [
            f"路径（{result['hops']} 跳，作用域 {result.get('scope', '')}）：{result['chain']}",
        ]
        resolution = result.get("resolution") or {}
        lines.append(f"实体解析：起点={resolution.get('from')}；终点={resolution.get('to')}")
        return "\n".join(lines)

    return graph_path_search
