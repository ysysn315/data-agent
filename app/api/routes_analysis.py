"""分析 Agent - API 路由（P-O-R 工作流）

    POST /api/analysis   同步小模式：跑完整 Plan-Operation-Reflection，直接返回 Markdown 报告

同步模式限制在 **步数 ≤ 2** 的小分析（max_steps 会被夹到 2），适合秒级返回的场景。
需要更多步骤 / 长耗时的分析，请走异步任务框架，进度用 SSE 订阅：

    POST /api/tasks            {"type": "run_analysis_task",
                                "params": {"question": "...", "datasource_id": 1}}  -> {task_id}
    GET  /api/tasks/{id}       查询状态，done 后 result.report 即完整报告
    GET  /api/tasks/{id}/events SSE 事件流：started → planning → step i/N → reflecting → reporting → done

路由风格对齐 routes_tasks（APIRouter(prefix=...) + Depends 注入），依赖 get_analysis_agent
可被 dependency_overrides 换成假 agent（离线测试不触碰真 LLM）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.analysis_agent import AnalysisAgent
from app.core.dependencies import get_current_user_optional, get_datasource_service
from app.core.settings import settings
from app.datasources.context import use_datasource
from app.datasources.models import DataSourceNotFoundError
from app.datasources.service import DataSourceService, normalize_workspace_id
from app.graph.scope import GraphScope, use_graph_scope

router = APIRouter(prefix="/analysis", tags=["analysis"])

# 同步小模式的步数上限：控制单请求时延，超出请走异步任务
SYNC_MAX_STEPS = 2


class AnalysisRequest(BaseModel):
    question: str = Field(description="分析请求，如：分析各州的销售额分布并给出建议")
    datasource_id: int | None = Field(default=None, ge=1, description="可选的平台数据源")
    max_steps: int = Field(
        default=SYNC_MAX_STEPS,
        ge=2,
        le=SYNC_MAX_STEPS,
        description="计划步数上限（同步模式最多 2 步；更多步请走 POST /api/tasks）",
    )


class AnalysisResponse(BaseModel):
    report: str = Field(description="结构化 Markdown 报告")
    plan: list = Field(description="规划出的步骤列表 [{goal, tool_hint}]")
    steps: list = Field(description="各步摘要 [{index, goal, answer_preview, sql_count}]")


async def get_analysis_agent() -> AnalysisAgent:
    """AnalysisAgent 依赖：复用 ChatAgent 单例做 Operation，另建一个 LLM 做规划/反思。

    每次请求新建 AnalysisAgent（轻量：仅包 llm + chat_agent + 图），不进 dependencies 单例。
    测试通过 dependency_overrides 注入假 agent，不走这里、不连真 LLM。
    """
    from app.core.dependencies import get_chat_agent
    from app.core.llm import LLMFactory

    chat_agent = await get_chat_agent()
    llm = LLMFactory.create_llm(temperature=0.0, streaming=False)
    return AnalysisAgent(llm=llm, chat_agent=chat_agent, max_steps=SYNC_MAX_STEPS)


@router.post("", response_model=AnalysisResponse)
async def analyze(
    req: AnalysisRequest,
    agent: AnalysisAgent = Depends(get_analysis_agent),
    user: dict | None = Depends(get_current_user_optional),
    datasource_service: DataSourceService = Depends(get_datasource_service),
):
    """同步跑一次小规模分析，返回结构化 Markdown 报告。长任务请走 POST /api/tasks。"""
    workspace_id = normalize_workspace_id(user.get("workspace_id") if user else None)
    if req.datasource_id is not None:
        if settings.auth_enabled and user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="选择数据源时需要有效的 API Key")
        try:
            await datasource_service.get_source(req.datasource_id, workspace_id)
        except DataSourceNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    try:
        with (
            use_datasource(req.datasource_id, workspace_id),
            use_graph_scope(GraphScope.from_ids(workspace_id, req.datasource_id)),
        ):
            result = await agent.analyze(req.question, max_steps=min(req.max_steps, SYNC_MAX_STEPS))
    except RuntimeError as e:
        # Planner 重试后仍无法产出合法计划：显式 400，交由调用方重述问题
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return AnalysisResponse(
        report=result["report"],
        plan=result["plan"],
        steps=result["step_summaries"],
    )
