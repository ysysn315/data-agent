"""分析 Agent（Plan-Operation-Reflection 工作流）

把一个宽泛的分析请求，拆成「规划 → 逐步执行 → 反思 → 报告」四阶段的
LangGraph StateGraph，产出一份结构化 Markdown 报告。

设计要点（详见 app/agents/IMPLEMENTATION-analysis.md）：
- Planner：LLM 把请求拆成 2~N 步 JSON 计划（每步：目标 goal + 技能/工具提示 tool_hint）；
  解析失败重试一次，再失败显式报错（RuntimeError）。
- Operation：逐步执行，**每一步就是复用 ChatAgent 跑完整一轮**——技能三段式（渐进式
  披露 + 门控 + 激活）原样生效，收集该步的回答文本、SQL 与工具轨迹。
- Reflection：LLM 审查全部步骤结果（是否回答了原问题、有无矛盾/缺口）；发现缺口
  允许**至多一次**补充步骤（supplement_used 标志防循环）。
- Report：在**代码里**把状态拼成固定结构的 Markdown（概述 / 各步发现 / 结论与建议 /
  附：执行的 SQL 清单），不让 LLM 一次性生成整篇文档。

与 my-agent 原版（app/agents/aiops_agent.py 的 AIOpsAgent，283 行）的关系见 IMPLEMENTATION。
构造期注入 llm 与 chat_agent（测试可全部换成假模型）；每阶段通过回调 on_event 上报
TaskEvent 进度（复用 app/tasks/events.py 的事件模型，异步任务侧直接透传给 SSE）。
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Awaitable, Callable, List, Optional, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from loguru import logger

from app.core.tracing import get_langfuse_callbacks
from app.tasks.events import TaskEvent

# 计划步数的下限/上限：低于 2 步不算「分析」，高于 5 步同步跑不划算（长任务走异步）
MIN_STEPS = 2
MAX_STEPS = 5
# 各步回答在事件 payload / 报告里的预览长度
ANSWER_PREVIEW_CHARS = 200

OnEvent = Callable[[TaskEvent], Awaitable[None]]


class AnalysisState(TypedDict, total=False):
    """P-O-R 工作流的状态。

    plan / step_results 用「整表替换」语义（每个节点读旧值、算出新全量返回），
    因此不需要 reducer——补充步骤时 operation 只补跑 len(step_results)..len(plan) 的差集。
    """

    question: str
    max_steps: int
    plan: List[dict]  # [{goal, tool_hint}]
    step_results: List[dict]  # [{index, goal, answer, sql_list, tools}]
    reflection: str  # 反思给出的「结论与建议」正文
    assessment: str  # 反思对「是否回答了原问题」的一句话判断
    supplement_used: bool  # 是否已用掉那一次补充机会（防循环）
    report: str  # 最终 Markdown 报告


class AnalysisAgent:
    """P-O-R 分析工作流。

    Args:
        llm: 规划与反思用的 LLM（BaseChatModel；测试传假模型）。Operation 步骤**不**用它，
            而是复用 chat_agent。
        chat_agent: 已装配技能中间件的 ChatAgent；每个 Operation 步骤跑它的图一轮。
        on_event: 进度回调，异步任务侧把它接到 TaskService.publish_event；None 时不上报。
        max_steps: 计划步数上限（clamp 到 [2,5]）。可在 analyze() 时按次覆盖（同步小模式传 2）。
    """

    def __init__(
        self,
        llm: BaseChatModel,
        chat_agent: Any,
        on_event: Optional[OnEvent] = None,
        max_steps: int = MAX_STEPS,
    ):
        self.llm = llm
        self.chat_agent = chat_agent
        self.on_event = on_event
        self.max_steps = _clamp_steps(max_steps)
        self.graph = self._build_graph()

    # ========== 图装配 ==========

    def _build_graph(self):
        workflow = StateGraph(AnalysisState)
        workflow.add_node("planner", self.planner_node)
        workflow.add_node("operation", self.operation_node)
        workflow.add_node("reflection", self.reflection_node)
        workflow.add_node("report", self.report_node)
        workflow.set_entry_point("planner")
        workflow.add_edge("planner", "operation")
        workflow.add_edge("operation", "reflection")
        # 反思后：还有没跑的（补充）步骤就回 operation，否则去 report
        workflow.add_conditional_edges(
            "reflection",
            self._route_after_reflection,
            {"operation": "operation", "report": "report"},
        )
        workflow.add_edge("report", END)
        return workflow.compile()

    @staticmethod
    def _route_after_reflection(state: AnalysisState) -> str:
        plan = state.get("plan", [])
        done = state.get("step_results", [])
        return "operation" if len(plan) > len(done) else "report"

    # ========== 进度上报 ==========

    async def _emit(
        self,
        phase: str,
        message: str,
        *,
        progress: Optional[float] = None,
        event_type: str = "progress",
        **payload: Any,
    ) -> None:
        """构造 TaskEvent 并回调上报（phase 塞进 payload，供进度序列断言与前端渲染）。"""
        if self.on_event is None:
            return
        payload = {"phase": phase, **payload}
        event = TaskEvent(type=event_type, message=message, progress=progress, payload=payload)
        # 兼容同步/异步回调；事件上报是辅助路径，失败只告警不打断分析主流程
        try:
            result = self.on_event(event)
            if inspect.isawaitable(result):
                await result
        except Exception as e:  # noqa: BLE001
            logger.warning(f"分析进度事件上报失败（忽略继续）: {e}")

    # ========== 阶段 1：Planner ==========

    async def planner_node(self, state: AnalysisState) -> dict:
        question = state["question"]
        max_steps = _clamp_steps(state.get("max_steps", self.max_steps))
        plan = await self._plan_with_retry(question, max_steps)
        logger.info(f"Planner 生成 {len(plan)} 步计划")
        await self._emit(
            "planning",
            f"已把请求拆解为 {len(plan)} 个分析步骤",
            progress=0.1,
            steps=[s["goal"] for s in plan],
        )
        return {"plan": plan}

    async def _plan_with_retry(self, question: str, max_steps: int) -> List[dict]:
        """要 LLM 产出 JSON 计划；解析失败重试一次，再失败显式报错。"""
        base = [
            SystemMessage(content=_PLANNER_SYSTEM),
            HumanMessage(content=_planner_user(question, max_steps)),
        ]
        first = await self.llm.ainvoke(base)
        try:
            return self._parse_plan(first.content, max_steps)
        except ValueError as e1:
            logger.warning(f"Planner 首次输出解析失败，重试一次：{e1}")
            retry = base + [first, HumanMessage(content=_PLANNER_RETRY)]
            second = await self.llm.ainvoke(retry)
            try:
                return self._parse_plan(second.content, max_steps)
            except ValueError as e2:
                raise RuntimeError(f"Planner 无法生成合法 JSON 计划（已重试一次）：{e2}") from e2

    @staticmethod
    def _parse_plan(content: Any, max_steps: int) -> List[dict]:
        """从模型输出里抠出 JSON 数组并规整为 [{goal, tool_hint}]；不合法抛 ValueError。"""
        data = _extract_json(content)
        if not isinstance(data, list) or not data:
            raise ValueError("计划不是非空 JSON 数组")
        plan: List[dict] = []
        for i, raw in enumerate(data[:max_steps]):
            if not isinstance(raw, dict):
                raise ValueError(f"第 {i + 1} 步不是对象")
            goal = str(raw.get("goal", "")).strip()
            if not goal:
                raise ValueError(f"第 {i + 1} 步缺少 goal")
            plan.append({"goal": goal, "tool_hint": str(raw.get("tool_hint", "")).strip()})
        if len(plan) < MIN_STEPS:
            raise ValueError(f"计划至少需要 {MIN_STEPS} 步，实际 {len(plan)} 步")
        return plan

    # ========== 阶段 2：Operation（逐步复用 ChatAgent） ==========

    async def operation_node(self, state: AnalysisState) -> dict:
        plan = state["plan"]
        results = list(state.get("step_results", []))
        total = len(plan)
        # 只补跑还没执行的步骤（首轮是全部；补充轮只跑新增的那一步）
        for idx in range(len(results), total):
            step = plan[idx]
            human_no = idx + 1
            answer, sql_list, tools = await self._run_step(step, results, state["question"])
            record = {
                "index": human_no,
                "goal": step["goal"],
                "answer": answer,
                "sql_list": sql_list,
                "tools": tools,
            }
            results.append(record)
            await self._emit(
                "step",
                f"第 {human_no}/{total} 步：{step['goal']}",
                progress=round(0.1 + 0.6 * human_no / total, 4),
                index=human_no,
                total=total,
                goal=step["goal"],
                answer_preview=_preview(answer),
                sql=sql_list,
                tools=tools,
            )
        return {"step_results": results}

    async def _run_step(self, step: dict, prior: List[dict], question: str) -> tuple[str, List[str], List[str]]:
        """跑 ChatAgent 一轮执行单步，返回（回答文本, SQL 列表, 工具轨迹）。"""
        prompt = _operation_prompt(question, step, prior)
        result = await self.chat_agent.graph.ainvoke(
            {"messages": [HumanMessage(content=prompt)]},
            config={"callbacks": get_langfuse_callbacks()},
        )
        messages = result.get("messages", []) if isinstance(result, dict) else []
        return _extract_step_output(messages)

    # ========== 阶段 3：Reflection（至多一次补充） ==========

    async def reflection_node(self, state: AnalysisState) -> dict:
        results = state.get("step_results", [])
        supplement_used = bool(state.get("supplement_used", False))
        review = await self._reflect(state["question"], results)

        update: dict = {
            "reflection": review.get("conclusion", ""),
            "assessment": review.get("assessment", ""),
        }
        gap = bool(review.get("need_more")) and isinstance(review.get("supplement"), dict)
        supplement = review.get("supplement") or {}
        can_supplement = gap and not supplement_used and str(supplement.get("goal", "")).strip()

        if can_supplement:
            new_plan = list(state["plan"]) + [
                {
                    "goal": str(supplement["goal"]).strip(),
                    "tool_hint": str(supplement.get("tool_hint", "")).strip(),
                }
            ]
            update["plan"] = new_plan
            update["supplement_used"] = True
            await self._emit(
                "reflecting",
                f"发现缺口，补充 1 个步骤：{supplement['goal']}",
                progress=0.8,
                need_more=True,
                supplement=str(supplement["goal"]).strip(),
            )
        else:
            # 已补充过 / 无缺口：都不再加步（防循环）
            update["supplement_used"] = supplement_used
            await self._emit(
                "reflecting",
                "已审查各步结果，证据充分，准备汇总报告",
                progress=0.8,
                need_more=False,
            )
        return update

    async def _reflect(self, question: str, results: List[dict]) -> dict:
        """LLM 审查全部步骤结果，返回 {assessment, conclusion, need_more, supplement}。

        反思是尽力而为的：JSON 解析失败不报错（不同于 Planner），退化为「用原文当结论、
        不补充」——避免反思输出的格式抖动把整条分析链搞崩。
        """
        messages = [
            SystemMessage(content=_REFLECTION_SYSTEM),
            HumanMessage(content=_reflection_user(question, results)),
        ]
        resp = await self.llm.ainvoke(messages)
        try:
            data = _extract_json(resp.content)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        text = _as_text(resp.content).strip()
        logger.warning("Reflection 输出非 JSON，退化为纯文本结论、不触发补充")
        return {"assessment": "", "conclusion": text, "need_more": False, "supplement": {}}

    # ========== 阶段 4：Report（代码拼接） ==========

    async def report_node(self, state: AnalysisState) -> dict:
        await self._emit("reporting", "正在汇总结构化报告", progress=0.9)
        report = _render_report(state)
        summaries = _step_summaries(state.get("step_results", []))
        await self._emit(
            "done",
            "分析完成",
            progress=1.0,
            event_type="done",
            steps=summaries,
        )
        return {"report": report}

    # ========== 对外入口 ==========

    async def analyze(self, question: str, max_steps: Optional[int] = None) -> dict:
        """跑完整 P-O-R 工作流，返回 {report, plan, step_results, reflection, step_summaries}。"""
        logger.info(f"AnalysisAgent 开始分析：{question[:60]}")
        init: AnalysisState = {
            "question": question,
            "max_steps": _clamp_steps(max_steps if max_steps is not None else self.max_steps),
            "plan": [],
            "step_results": [],
            "reflection": "",
            "assessment": "",
            "supplement_used": False,
            "report": "",
        }
        final = await self.graph.ainvoke(init)
        return {
            "report": final.get("report", ""),
            "plan": final.get("plan", []),
            "step_results": final.get("step_results", []),
            "reflection": final.get("reflection", ""),
            "step_summaries": _step_summaries(final.get("step_results", [])),
        }


# ========== 纯函数辅助（无状态，便于单测与复用） ==========


def _clamp_steps(n: Any) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return MAX_STEPS
    return max(MIN_STEPS, min(MAX_STEPS, n))


def _as_text(content: Any) -> str:
    """把 LLM 的 content（str 或分段 list）统一成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content)


def _extract_json(content: Any) -> Any:
    """从模型输出里提取 JSON：先整体 parse，失败再抠首个 {...} 或 [...] 片段。"""
    text = _as_text(content).strip()
    if not text:
        raise ValueError("空输出")
    # 去掉 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    # 兜底：抠第一个数组或对象
    match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except (TypeError, ValueError) as e:
            raise ValueError(f"JSON 解析失败：{e}") from e
    raise ValueError("未找到 JSON 片段")


def _extract_step_output(messages: List) -> tuple[str, List[str], List[str]]:
    """从 ChatAgent 一轮的消息流里抽取：最终回答、SQL 列表、工具轨迹。"""
    answer = ""
    sql_list: List[str] = []
    tools: List[str] = []
    for msg in messages:
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            args = (tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)) or {}
            if name:
                tools.append(name)
            if name == "execute_sql":
                sql = str(args.get("sql", "")).strip()
                if sql:
                    sql_list.append(sql)
        # 最后一条「无 tool_calls 的 AIMessage」即该步答案
        if msg.__class__.__name__ == "AIMessage" and not getattr(msg, "tool_calls", None):
            text = _as_text(getattr(msg, "content", "")).strip()
            if text:
                answer = text
    return answer, sql_list, tools


def _preview(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= ANSWER_PREVIEW_CHARS else text[:ANSWER_PREVIEW_CHARS] + "…"


def _step_summaries(results: List[dict]) -> List[dict]:
    """给事件 payload / 任务结果用的精简步骤摘要（不塞大对象）。"""
    return [
        {
            "index": r.get("index"),
            "goal": r.get("goal"),
            "answer_preview": _preview(r.get("answer", "")),
            "sql_count": len(r.get("sql_list", [])),
        }
        for r in results
    ]


def _render_report(state: AnalysisState) -> str:
    """把状态拼成固定结构的 Markdown 报告（结构由代码保证，内容来自各阶段产出）。"""
    question = state.get("question", "")
    results = state.get("step_results", [])
    assessment = (state.get("assessment") or "").strip()
    conclusion = (state.get("reflection") or "").strip()

    lines: List[str] = ["# 数据分析报告", ""]

    # 概述
    lines += ["## 概述", ""]
    lines.append(f"- 原始问题：{question}")
    lines.append(f"- 执行步骤：{len(results)} 步")
    if assessment:
        lines.append(f"- 数据评估：{assessment}")
    lines.append("")

    # 各步发现
    lines += ["## 各步发现", ""]
    for r in results:
        lines.append(f"### 步骤 {r.get('index')}：{r.get('goal', '')}")
        answer = (r.get("answer") or "").strip() or "（本步未产生文本结论）"
        lines.append(answer)
        n_sql = len(r.get("sql_list", []))
        if n_sql:
            lines.append(f"\n> 本步执行了 {n_sql} 条 SQL。")
        lines.append("")

    # 结论与建议
    lines += ["## 结论与建议", ""]
    lines.append(conclusion or "（反思阶段未给出额外结论）")
    lines.append("")

    # 附：执行的 SQL 清单
    lines += ["## 附：执行的 SQL 清单", ""]
    all_sql = [sql for r in results for sql in r.get("sql_list", [])]
    if all_sql:
        for i, sql in enumerate(all_sql, start=1):
            lines.append(f"{i}. `{sql}`")
    else:
        lines.append("（本次分析未执行 SQL）")
    lines.append("")

    return "\n".join(lines).strip() + "\n"


# ========== Prompt 文案 ==========

_PLANNER_SYSTEM = (
    "你是一名严谨的数据分析规划助手。你的职责是把用户的分析请求拆解为一份可执行的分步计划，"
    "只输出 JSON，不要任何解释或 Markdown 围栏。"
)


def _planner_user(question: str, max_steps: int) -> str:
    return (
        f"请把下面的分析请求拆解为 {MIN_STEPS} 到 {max_steps} 个步骤，"
        "每一步聚焦一个可独立执行的子目标。\n\n"
        f"分析请求：{question}\n\n"
        "输出要求：一个 JSON 数组，每个元素是对象，字段：\n"
        "  - goal：这一步要达成的具体目标（中文，一句话）\n"
        '  - tool_hint：建议使用的技能或工具提示（如 "sqlite-query 技能查询订单表"、"检索内部文档"）\n'
        '示例：[{"goal": "统计各州订单量", "tool_hint": "sqlite-query 技能对 orders 表分组"}, ...]\n'
        "只输出 JSON 数组本身。"
    )


_PLANNER_RETRY = (
    "上一次的输出无法被解析为 JSON。请严格只输出一个 JSON 数组，不要包含任何解释、前后缀或 Markdown 代码围栏。"
)

_REFLECTION_SYSTEM = (
    "你是一名数据分析复盘助手。请审查已完成的各步骤结果，判断是否已经回答了用户的原始问题、"
    "步骤之间有无矛盾或明显缺口。只输出 JSON。"
)


def _reflection_user(question: str, results: List[dict]) -> str:
    blocks = []
    for r in results:
        sql = "；".join(r.get("sql_list", [])) or "无"
        blocks.append(
            f"步骤 {r.get('index')}（目标：{r.get('goal')}）\n回答：{_preview(r.get('answer', '')) or '无'}\nSQL：{sql}"
        )
    joined = "\n\n".join(blocks) or "（无已完成步骤）"
    return (
        f"用户原始问题：{question}\n\n"
        f"各步骤结果：\n{joined}\n\n"
        "请输出一个 JSON 对象，字段：\n"
        "  - assessment：一句话判断数据是否回答了原问题\n"
        "  - conclusion：给用户的「结论与建议」正文（可多句，综合各步发现）\n"
        "  - need_more：布尔，是否还缺一个关键步骤才能回答问题\n"
        '  - supplement：当 need_more 为 true 时，给出补充步骤对象 {"goal":..., "tool_hint":...}；否则给 {}\n'
        "只输出 JSON 对象本身。"
    )


def _operation_prompt(question: str, step: dict, prior: List[dict]) -> str:
    context = ""
    if prior:
        prev = "\n".join(f"- 步骤{r.get('index')}结论：{_preview(r.get('answer', ''))}" for r in prior)
        context = f"\n\n已完成步骤的结论（供参考，不要重复执行）：\n{prev}"
    hint = f"\n建议使用：{step['tool_hint']}" if step.get("tool_hint") else ""
    return (
        f"这是一个更大分析任务中的一步。整体分析目标：{question}\n\n"
        f"本步目标：{step['goal']}{hint}{context}\n\n"
        "请使用可用的技能与工具完成本步目标；涉及数据查询时先了解表结构再生成并执行 SQL，"
        "最后用简洁的中文给出这一步的结论。"
    )
