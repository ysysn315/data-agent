"""异步任务框架的核心服务。

三块职责，都落在 Redis 上（与 D1 持久化层的边界见 IMPLEMENTATION.md「取舍」一节）：
1. 入队：把 {task_type, params} 投递到 arq 队列，返回 task_id（复用为 arq job_id）；
2. 状态：queued/running/done/failed + 结果，存 Redis Hash（易失、带 TTL）；
3. 事件流：每条进度用 XADD 追加到 Redis Stream，XRANGE 增量读出，SSE 透传。

连接分工：元数据/事件流走一个 decode_responses=True 的异步 Redis（读回即字符串）；
入队走 arq 自己的连接池（ArqRedis，仅用 enqueue_job）。worker 侧写事件时直接用
arq 注入的 ctx["redis"]（bytes 模式）——写入与 decode 模式无关，读取统一在 text 端。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from redis.asyncio import Redis

from app.core.settings import Settings
from app.tasks.events import TERMINAL_EVENT_TYPES, TaskEvent

# task_type -> arq 任务函数名（worker.py 里注册的协程名）。新增任务在此登记即可。
TASK_REGISTRY: dict[str, str] = {
    "chat": "run_chat_task",
    "eval": "run_eval_task",
    "run_analysis_task": "run_analysis_task",  # E 轮：P-O-R 分析工作流（type 即函数名）
}

# 任务元数据/事件流的 TTL（秒）：24h，避免历史任务无限堆积。arq 专属配置走默认，不进 settings。
DEFAULT_TASK_TTL_SECONDS = 24 * 3600

# 元数据里的终态
TERMINAL_STATUSES = {"done", "failed"}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _loads(raw: Optional[str]):
    """Hash 里存的 JSON 字符串安全反序列化；空串/None 返回 None。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _sse(data: dict) -> str:
    """复用 routes_chat 的 SSE 帧格式：data: {...}\\n\\n。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _fields_to_event(fields: dict) -> dict:
    """Stream 里的扁平字段还原成事件字典（供 SSE 透传）。"""
    progress = fields.get("progress")
    return {
        "type": fields.get("type"),
        "message": fields.get("message", ""),
        "progress": float(progress) if progress not in (None, "") else None,
        "payload": _loads(fields.get("payload")) or {},
        "ts": fields.get("ts"),
    }


# ========== 连接构造（供 dependencies / worker 复用，全部从现有 settings.redis_* 组装）==========


def build_redis_settings(settings: Settings):
    """用现有 settings.redis_* 组装 arq 的 RedisSettings（不新增任何配置项）。"""
    from arq.connections import RedisSettings

    return RedisSettings(
        host=settings.redis_host,
        port=settings.redis_port,
        database=settings.redis_db,
        password=settings.redis_password or None,
    )


def create_task_redis(settings: Settings) -> Redis:
    """事件流/元数据用的异步 Redis 客户端（decode_responses=True）。惰性连接。"""
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password or None,
        decode_responses=True,
    )


async def create_arq_pool(settings: Settings):
    """arq 入队用的连接池（ArqRedis，仅供 enqueue_job）。会 ping Redis。"""
    from arq import create_pool

    return await create_pool(build_redis_settings(settings))


class TaskService:
    """异步任务的入队、状态查询与事件流。

    Args:
        redis: 元数据 + 事件流用的异步 Redis（读取侧务必 decode_responses=True）。
        arq_pool: 入队用的 ArqRedis；仅 enqueue 需要，worker 侧写事件可不传。
        ttl_seconds: 元数据/事件流的过期时间。
    """

    def __init__(self, redis: Redis, arq_pool=None, ttl_seconds: int = DEFAULT_TASK_TTL_SECONDS):
        self.redis = redis
        self.arq_pool = arq_pool
        self.ttl = ttl_seconds

    # ---- key 布局 ----

    @staticmethod
    def _meta_key(task_id: str) -> str:
        return f"task:{task_id}:meta"

    @staticmethod
    def _events_key(task_id: str) -> str:
        return f"task:{task_id}:events"

    # ---- 入队 ----

    async def enqueue(self, task_type: str, params: Optional[dict] = None) -> str:
        """登记任务元数据（status=queued）并投递到 arq 队列，返回 task_id。"""
        params = params or {}
        func_name = TASK_REGISTRY.get(task_type)
        if func_name is None:
            raise ValueError(f"未知任务类型: {task_type}（可选: {sorted(TASK_REGISTRY)}）")
        if self.arq_pool is None:
            raise RuntimeError("TaskService 未注入 arq_pool，无法入队")

        task_id = uuid.uuid4().hex
        meta_key = self._meta_key(task_id)
        now = _now_iso()
        await self.redis.hset(
            meta_key,
            mapping={
                "task_id": task_id,
                "type": task_type,
                "params": json.dumps(params, ensure_ascii=False),
                "status": "queued",
                "result": "",
                "error": "",
                "created_at": now,
                "updated_at": now,
            },
        )
        await self.redis.expire(meta_key, self.ttl)
        # 用 task_id 作为 arq job_id：worker 里 ctx["job_id"] 即 task_id，无需二次传递
        await self.arq_pool.enqueue_job(func_name, _job_id=task_id, **params)
        return task_id

    # ---- 状态 ----

    async def get_status(self, task_id: str) -> Optional[dict]:
        """读取任务状态 + 结果；任务不存在返回 None。"""
        meta = await self.redis.hgetall(self._meta_key(task_id))
        if not meta:
            return None
        return {
            "task_id": meta.get("task_id", task_id),
            "type": meta.get("type"),
            "status": meta.get("status"),
            "params": _loads(meta.get("params")),
            "result": _loads(meta.get("result")),
            "error": meta.get("error") or None,
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
        }

    async def _update_meta(self, task_id: str, **fields) -> None:
        fields["updated_at"] = _now_iso()
        await self.redis.hset(self._meta_key(task_id), mapping=fields)
        await self.redis.expire(self._meta_key(task_id), self.ttl)

    async def mark_running(self, task_id: str) -> None:
        await self._update_meta(task_id, status="running")

    async def mark_done(self, task_id: str, result: Optional[dict] = None) -> None:
        await self._update_meta(task_id, status="done", result=json.dumps(result or {}, ensure_ascii=False))

    async def mark_failed(self, task_id: str, error: str) -> None:
        await self._update_meta(task_id, status="failed", error=str(error))

    # ---- 事件流（Redis Streams）----

    async def publish_event(self, task_id: str, event: TaskEvent) -> str:
        """把一条进度事件 XADD 进 Stream，返回自动生成的 stream seq。"""
        fields = {
            "type": event.type,
            "message": event.message,
            "progress": "" if event.progress is None else str(event.progress),
            "payload": json.dumps(event.payload, ensure_ascii=False),
            "ts": _now_iso(),
        }
        seq = await self.redis.xadd(self._events_key(task_id), fields)
        await self.redis.expire(self._events_key(task_id), self.ttl)
        return seq

    async def read_events(self, task_id: str, after_seq: str = "0-0", count: int = 200) -> list[dict]:
        """按游标增量读事件；after_seq 为上次读到的 seq（独占），返回 [{seq, event}]。"""
        start = "-" if after_seq in ("", "0-0") else f"({after_seq}"
        rows = await self.redis.xrange(self._events_key(task_id), min=start, max="+", count=count)
        return [{"seq": seq, "event": _fields_to_event(fields)} for seq, fields in rows]

    async def stream_sse(
        self,
        task_id: str,
        poll_interval: float = 0.25,
        max_seconds: float = 300.0,
    ) -> AsyncIterator[str]:
        """把事件流按 SSE 格式吐给前端：逐条 data: {...}，任务结束发 done 后关闭。

        终结条件（任一）：读到终结事件（done/error）；或任务已落终态但 worker 没来得及
        发终结事件（如崩溃）——此时补发一条 done 兜底。看门狗 max_seconds 防止悬挂。
        """
        if await self.get_status(task_id) is None:
            yield _sse({"type": "error", "message": f"任务不存在: {task_id}", "payload": {}})
            return

        last_seq = "0-0"
        waited = 0.0
        while True:
            events = await self.read_events(task_id, after_seq=last_seq)
            for item in events:
                last_seq = item["seq"]
                yield _sse(item["event"])
                if item["event"]["type"] in TERMINAL_EVENT_TYPES:
                    return

            if not events:
                status_doc = await self.get_status(task_id)
                status = status_doc["status"] if status_doc else None
                if status in TERMINAL_STATUSES:
                    yield _sse({
                        "type": "done",
                        "message": f"任务已{status}",
                        "progress": 1.0 if status == "done" else None,
                        "payload": {"status": status},
                    })
                    return
                await asyncio.sleep(poll_interval)
                waited += poll_interval
                if waited >= max_seconds:
                    yield _sse({"type": "error", "message": "事件流超时", "payload": {}})
                    return
