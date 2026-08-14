"""异步任务框架测试（离线，全程 fakeredis，不要求本机跑 Redis）。

覆盖：
1. enqueue -> 状态 queued，且 arq job 真的进了队列（ArqRedis over fakeredis）
2. 事件 XADD / 增量读取往返
3. 状态流转（手动调 service 的 mark_* 模拟 worker）
4. TaskService.enqueue 未知类型报错、get_status 不存在返回 None
5. API：POST /api/tasks、GET /api/tasks/{id}（含 400/404），走 httpx + dependency_overrides
6. SSE 端点：预置一个已完成任务，读到 done 后自动关闭
7. run_eval_task 逐例进度事件格式（monkeypatch 掉 eval，不调真 LLM/库）
8. 真实 arq worker 端到端一条（burst worker），本机 6379 连不上则跳过
"""

import socket

import fakeredis.aioredis
import httpx
import pytest
from arq import ArqRedis

from app.tasks.events import TaskEvent
from app.tasks.service import TaskService

# ========== fakeredis fixtures（共享 FakeServer，text 端 decode，arq 端 bytes）==========


@pytest.fixture
def fake_server():
    return fakeredis.aioredis.FakeServer()


@pytest.fixture
def text_redis(fake_server):
    # 读取侧：decode_responses=True，读回即字符串
    return fakeredis.aioredis.FakeRedis(server=fake_server, decode_responses=True)


@pytest.fixture
def arq_pool(fake_server):
    # 入队侧：arq 的 job 载荷是 bytes，用默认 bytes 模式，但同一个 FakeServer
    conn = fakeredis.aioredis.FakeRedis(server=fake_server)
    return ArqRedis(connection_pool=conn.connection_pool)


@pytest.fixture
def service(text_redis, arq_pool):
    return TaskService(redis=text_redis, arq_pool=arq_pool)


# ========== 1. enqueue ==========


async def test_enqueue_sets_queued_and_pushes_job(service, text_redis):
    task_id = await service.enqueue("eval", {"limit": 2, "model": None})
    assert task_id

    st = await service.get_status(task_id)
    assert st["status"] == "queued"
    assert st["type"] == "eval"
    assert st["params"] == {"limit": 2, "model": None}
    assert st["result"] is None and st["error"] is None

    # arq job 真的入队：task_id 即 job_id
    members = await text_redis.zrange("arq:queue", 0, -1)
    assert task_id in members
    assert await text_redis.exists(f"arq:job:{task_id}")


async def test_enqueue_unknown_type_raises(service):
    with pytest.raises(ValueError):
        await service.enqueue("nope", {})


async def test_enqueue_without_pool_raises(text_redis):
    svc = TaskService(redis=text_redis)  # 无 arq_pool
    with pytest.raises(RuntimeError):
        await svc.enqueue("eval", {})


# ========== 2. 事件流往返 ==========


async def test_event_xadd_and_incremental_read(service):
    task_id = "t-events"
    await service.publish_event(
        task_id, TaskEvent(type="progress", message="第 1/2 例 ✓", progress=0.5, payload={"id": "q1"})
    )
    await service.publish_event(
        task_id, TaskEvent(type="done", message="完成", progress=1.0, payload={"accuracy": 0.5})
    )

    events = await service.read_events(task_id)
    assert [e["event"]["type"] for e in events] == ["progress", "done"]
    first = events[0]["event"]
    assert first["message"] == "第 1/2 例 ✓"
    assert first["progress"] == 0.5
    assert first["payload"] == {"id": "q1"}

    # 游标增量读：从第一条之后只应读到第二条
    after = await service.read_events(task_id, after_seq=events[0]["seq"])
    assert [e["event"]["type"] for e in after] == ["done"]


# ========== 3. 状态流转 ==========


async def test_status_transitions(service):
    task_id = await service.enqueue("eval", {"limit": 1})
    assert (await service.get_status(task_id))["status"] == "queued"

    await service.mark_running(task_id)
    assert (await service.get_status(task_id))["status"] == "running"

    await service.mark_done(task_id, {"accuracy": 1.0})
    st = await service.get_status(task_id)
    assert st["status"] == "done"
    assert st["result"] == {"accuracy": 1.0}


async def test_mark_failed_records_error(service):
    task_id = await service.enqueue("eval", {})
    await service.mark_failed(task_id, "boom")
    st = await service.get_status(task_id)
    assert st["status"] == "failed"
    assert st["error"] == "boom"


async def test_get_status_missing_returns_none(service):
    assert await service.get_status("does-not-exist") is None


# ========== 5. API（httpx + dependency_overrides）==========


def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_api_create_get_and_errors(fake_server):
    from app.core.dependencies import get_task_service
    from app.main import app

    text = fakeredis.aioredis.FakeRedis(server=fake_server, decode_responses=True)
    pool = ArqRedis(connection_pool=fakeredis.aioredis.FakeRedis(server=fake_server).connection_pool)
    svc = TaskService(redis=text, arq_pool=pool)
    app.dependency_overrides[get_task_service] = lambda: svc
    try:
        async with _client(app) as client:
            # 提交
            r = await client.post("/api/tasks", json={"type": "eval", "params": {"limit": 1}})
            assert r.status_code == 201
            task_id = r.json()["task_id"]

            # 查询
            r2 = await client.get(f"/api/tasks/{task_id}")
            assert r2.status_code == 200
            assert r2.json()["status"] == "queued"

            # 未知类型 -> 400
            assert (await client.post("/api/tasks", json={"type": "nope", "params": {}})).status_code == 400
            # 不存在的任务 -> 404
            assert (await client.get("/api/tasks/ghost")).status_code == 404
    finally:
        app.dependency_overrides.clear()


async def test_api_scoped_task_overwrites_workspace_and_validates_datasource(fake_server, monkeypatch):
    """异步 Chat/Analysis 任务把服务端 workspace 传给 worker，不信任客户端字段。"""
    from app.core.dependencies import get_datasource_service, get_task_service
    from app.core.settings import settings
    from app.main import app

    class FakeDataSourceService:
        def __init__(self):
            self.seen = None

        async def get_source(self, datasource_id, workspace_id):
            self.seen = (datasource_id, workspace_id)
            return {"id": datasource_id, "workspace_id": workspace_id}

    text = fakeredis.aioredis.FakeRedis(server=fake_server, decode_responses=True)
    pool = ArqRedis(connection_pool=fakeredis.aioredis.FakeRedis(server=fake_server).connection_pool)
    svc = TaskService(redis=text, arq_pool=pool)
    datasource_service = FakeDataSourceService()
    monkeypatch.setattr(settings, "auth_enabled", False)
    app.dependency_overrides[get_task_service] = lambda: svc
    app.dependency_overrides[get_datasource_service] = lambda: datasource_service
    try:
        async with _client(app) as client:
            response = await client.post(
                "/api/tasks",
                json={
                    "type": "chat",
                    "params": {"question": "q", "datasource_id": 9, "workspace_id": 999},
                },
            )
            assert response.status_code == 201
            task = await svc.get_status(response.json()["task_id"])
            assert task["params"]["workspace_id"] == 0
            assert task["params"]["datasource_id"] == 9
            assert datasource_service.seen == (9, 0)

            rejected = await client.post(
                "/api/tasks",
                json={"type": "eval", "params": {"datasource_id": 9}},
            )
            assert rejected.status_code == 400
    finally:
        app.dependency_overrides.clear()


# ========== 6. SSE 端点 ==========


async def test_sse_streams_events_then_closes(fake_server):
    from app.core.dependencies import get_task_service
    from app.main import app

    text = fakeredis.aioredis.FakeRedis(server=fake_server, decode_responses=True)
    svc = TaskService(redis=text)  # SSE 只读，不需要 arq_pool
    task_id = "sse-1"

    # 预置一个已完成任务：running -> 一条进度 -> done（元数据 + 终结事件）
    await svc.mark_running(task_id)
    await svc.publish_event(task_id, TaskEvent(type="progress", message="第 1/1 例 ✓", progress=1.0))
    await svc.mark_done(task_id, {"accuracy": 1.0})
    await svc.publish_event(task_id, TaskEvent(type="done", message="完成", progress=1.0, payload={"accuracy": 1.0}))

    app.dependency_overrides[get_task_service] = lambda: svc
    try:
        async with _client(app) as client:
            resp = await client.get(f"/api/tasks/{task_id}/events")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = resp.text
    finally:
        app.dependency_overrides.clear()

    # SSE 帧：进度在前、done 在后，格式为 data: {...}
    assert "data: " in body
    assert "第 1/1 例 ✓" in body
    assert '"type": "done"' in body
    assert body.index("第 1/1 例") < body.index('"type": "done"')


# ========== 7. run_eval_task 进度事件 ==========


async def test_run_eval_task_emits_progress_events(fake_server, monkeypatch):
    from app.tasks import worker
    from evals.text2sql import run_execution_eval as ev

    text = fakeredis.aioredis.FakeRedis(server=fake_server, decode_responses=True)
    svc = TaskService(redis=text)
    task_id = "eval-1"

    # 固定 3 例数据集；重资源与逐例评估整体 monkeypatch，绝不碰真 LLM/库
    monkeypatch.setattr(
        ev,
        "load_dataset",
        lambda: [{"id": "q1", "tags": ["agg"]}, {"id": "q2", "tags": []}, {"id": "q3", "tags": ["join"]}],
    )
    monkeypatch.setattr(worker, "_build_eval_context", lambda model: ("db", {}, "body", "M", object()))
    seq = iter([True, False, True])
    monkeypatch.setattr(
        ev,
        "evaluate_case",
        lambda case, *a, **k: {"id": case["id"], "tags": case.get("tags", []), "correct": next(seq)},
    )

    ctx = {"job_id": task_id, "redis": text}
    await worker.run_eval_task(ctx, limit=3, model=None)

    events = [e["event"] for e in await svc.read_events(task_id)]
    types = [e["type"] for e in events]
    assert types[0] == "started"
    assert types[-1] == "done"

    progress_msgs = [e["message"] for e in events if e["type"] == "progress"]
    assert progress_msgs == ["第 1/3 例 ✓", "第 2/3 例 ✗", "第 3/3 例 ✓"]
    # 逐例进度带比例与命中标记
    prog_events = [e for e in events if e["type"] == "progress"]
    assert prog_events[0]["progress"] == round(1 / 3, 4)
    assert prog_events[0]["payload"] == {"id": "q1", "correct": True}

    # 终结事件带最终准确率
    done = events[-1]
    assert done["payload"]["total"] == 3
    assert done["payload"]["correct"] == 2
    assert done["payload"]["accuracy"] == round(2 / 3, 4)

    assert (await svc.get_status(task_id))["status"] == "done"


# ========== 8. 真实 arq worker 端到端（本机 6379 连不上则跳过）==========


def _redis_reachable(host: str = "localhost", port: int = 6379) -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


@pytest.mark.skipif(not _redis_reachable(), reason="本机 6379 无 Redis，跳过真实 arq worker 端到端")
async def test_real_arq_worker_end_to_end(monkeypatch):
    from arq.worker import Worker

    from app.core.settings import settings
    from app.tasks import worker as wk
    from app.tasks.service import build_redis_settings, create_arq_pool, create_task_redis
    from evals.text2sql import run_execution_eval as ev

    # 同进程 burst worker，monkeypatch 生效；避免真 LLM/库
    monkeypatch.setattr(ev, "load_dataset", lambda: [{"id": "q1", "tags": []}, {"id": "q2", "tags": []}])
    monkeypatch.setattr(wk, "_build_eval_context", lambda model: ("db", {}, "body", "M", object()))
    monkeypatch.setattr(ev, "evaluate_case", lambda case, *a, **k: {"id": case["id"], "tags": [], "correct": True})

    text = create_task_redis(settings)
    pool = await create_arq_pool(settings)
    svc = TaskService(redis=text, arq_pool=pool)
    task_id = await svc.enqueue("eval", {"limit": 2, "model": None})

    worker_obj = Worker(
        functions=wk.WORKER_FUNCTIONS,
        redis_settings=build_redis_settings(settings),
        burst=True,
        poll_delay=0.05,
        handle_signals=False,
    )
    try:
        await worker_obj.async_run()  # 处理完队列即退出（burst）

        st = await svc.get_status(task_id)
        assert st["status"] == "done"
        assert st["result"]["summary"]["total"] == 2
        types = [e["event"]["type"] for e in await svc.read_events(task_id)]
        assert types[0] == "started" and types[-1] == "done"
    finally:
        await text.delete(TaskService._meta_key(task_id), TaskService._events_key(task_id))
        await worker_obj.close()
        await pool.aclose()
        await text.aclose()
