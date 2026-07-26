"""异步任务的 arq worker：任务注册 + WorkerSettings + 两个演示任务。

启动 worker：
    arq app.tasks.worker.WorkerSettings

Redis 连接从 settings.redis_* 组装（build_redis_settings），不引入 arq 专属配置。
每个任务从 arq 注入的 ctx 拿两样东西：
    ctx["job_id"]  —— 即入队时用 task_id 设的 arq job_id，任务据此写自己的状态/事件；
    ctx["redis"]   —— arq 连接池，构造 TaskService 直接复用（只写，不读）。
"""
from __future__ import annotations

from typing import Optional

from langchain_core.messages import HumanMessage
from loguru import logger

from app.core.llm import LLMFactory
from app.core.settings import settings
from app.skills.middleware import READ_SKILL_TOOL_NAME
from app.tasks.events import TaskEvent
from app.tasks.service import TaskService, build_redis_settings


def _svc(ctx) -> TaskService:
    """从 arq ctx 取 Redis 连接构造 TaskService（worker 侧只写事件/状态）。"""
    return TaskService(redis=ctx["redis"])


# ========== 演示任务 1：后台跑一轮对话 ==========


def _message_progress_events(msg) -> list[TaskEvent]:
    """从一条 agent 消息里提取进度事件：技能激活（read_skill）与普通工具调用。"""
    events: list[TaskEvent] = []
    for tc in getattr(msg, "tool_calls", None) or []:
        name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
        args = (tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)) or {}
        if name == READ_SKILL_TOOL_NAME:
            slug = str(args.get("slug", "")).strip()
            events.append(TaskEvent(type="progress", message=f"已激活技能: {slug}", payload={"skill": slug}))
        else:
            events.append(TaskEvent(type="progress", message=f"工具调用: {name}", payload={"tool": name}))
    return events


def _final_text(msg) -> str:
    """AIMessage 且无 tool_calls 时，其 content 即最终答案片段（兼容分段 content）。"""
    if msg.__class__.__name__ != "AIMessage" or getattr(msg, "tool_calls", None):
        return ""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    return "".join(
        p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
    )


async def run_chat_task(ctx, question: str, session_id: Optional[str] = None) -> dict:
    """后台跑一轮 ChatAgent 对话，逐步产出进度事件：已激活技能 / 每次工具调用 / 完成。"""
    task_id = ctx["job_id"]
    svc = _svc(ctx)
    await svc.mark_running(task_id)
    await svc.publish_event(task_id, TaskEvent(type="started", message=f"开始处理问题: {question[:50]}"))
    try:
        # 延迟导入：避免 worker 模块导入期就构建整个 Agent（含 LLM/技能加载）
        from app.core.dependencies import get_chat_agent

        agent = await get_chat_agent()
        answer = ""
        # stream_mode="updates"：每个节点产出的 state 增量，从中截获工具调用与最终回答
        async for update in agent.graph.astream(
            {"messages": [HumanMessage(content=question)]},
            stream_mode="updates",
        ):
            for delta in (update or {}).values():
                for msg in (delta or {}).get("messages", []) if isinstance(delta, dict) else []:
                    for event in _message_progress_events(msg):
                        await svc.publish_event(task_id, event)
                    text = _final_text(msg)
                    if text:
                        answer = text

        await svc.publish_event(
            task_id, TaskEvent(type="done", message="对话完成", progress=1.0, payload={"answer": answer})
        )
        await svc.mark_done(task_id, {"answer": answer, "session_id": session_id})
        return {"answer": answer}
    except Exception as e:  # noqa: BLE001 —— 任务级兜底：标失败并发 error 事件，再抛给 arq 记账
        logger.exception(f"run_chat_task 失败: {e}")
        await svc.publish_event(task_id, TaskEvent(type="error", message=f"任务失败: {e}"))
        await svc.mark_failed(task_id, str(e))
        raise


# ========== 演示任务 2：后台跑 Text-to-SQL 执行准确率评估 ==========


def _build_eval_context(model: Optional[str]):
    """构建评估所需的重资源：库路径 / schema / 技能正文 / M-Schema / LLM。

    单独抽出，是为了让离线测试整体 monkeypatch 掉（不连真库、不调真 LLM）。
    """
    from evals.text2sql import run_execution_eval as ev

    db = settings.sqlite_db_path
    schema = ev.fetch_schema(db)
    skill_body = ev.read_skill_body()
    m_schema = ev.generate_m_schema(db)
    llm = LLMFactory.create_llm(model=model, temperature=0.0, streaming=False)
    return db, schema, skill_body, m_schema, llm


async def run_eval_task(ctx, limit: Optional[int] = None, model: Optional[str] = None) -> dict:
    """后台跑 Text-to-SQL 执行准确率评估，逐例产出进度事件（第 n/N 例 ✓/✗）与最终准确率。"""
    task_id = ctx["job_id"]
    svc = _svc(ctx)
    await svc.mark_running(task_id)
    try:
        from evals.text2sql import run_execution_eval as ev

        dataset = ev.load_dataset()
        if limit is not None:
            dataset = dataset[:limit]
        total = len(dataset)
        await svc.publish_event(
            task_id, TaskEvent(type="started", message=f"开始评估：{total} 个用例", payload={"total": total})
        )

        db, schema, skill_body, m_schema, llm = _build_eval_context(model)
        results = []
        for i, case in enumerate(dataset, start=1):
            r = ev.evaluate_case(case, db, schema, skill_body, m_schema, llm)
            results.append(r)
            flag = "✓" if r.get("correct") else "✗"
            await svc.publish_event(
                task_id,
                TaskEvent(
                    type="progress",
                    message=f"第 {i}/{total} 例 {flag}",
                    progress=round(i / total, 4) if total else 1.0,
                    payload={"id": r.get("id"), "correct": bool(r.get("correct"))},
                ),
            )

        report = ev.build_report(results)
        summary = report["summary"]
        acc = summary["accuracy"]
        await svc.publish_event(
            task_id,
            TaskEvent(
                type="done",
                message=f"评估完成，执行准确率 {acc:.2%}",
                progress=1.0,
                payload={"accuracy": acc, "correct": summary["correct"], "total": summary["total"]},
            ),
        )
        await svc.mark_done(task_id, {"accuracy": acc, "summary": summary})
        return summary
    except Exception as e:  # noqa: BLE001 —— 同 run_chat_task：标失败 + error 事件后上抛
        logger.exception(f"run_eval_task 失败: {e}")
        await svc.publish_event(task_id, TaskEvent(type="error", message=f"评估失败: {e}"))
        await svc.mark_failed(task_id, str(e))
        raise


# ========== arq WorkerSettings ==========

WORKER_FUNCTIONS = [run_chat_task, run_eval_task]


async def _on_startup(ctx) -> None:
    logger.info("arq worker 启动：异步任务框架就绪")


async def _on_shutdown(ctx) -> None:
    logger.info("arq worker 关闭")


class WorkerSettings:
    """`arq app.tasks.worker.WorkerSettings` 读取此类启动 worker。"""

    functions = WORKER_FUNCTIONS
    redis_settings = build_redis_settings(settings)
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    max_tries = 2          # 瞬时故障重试一次
    job_timeout = 600      # 单任务上限 10 分钟
    keep_result = 3600     # arq 自身结果保留 1 小时（我们的状态另存 Hash）
