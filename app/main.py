# FastAPI 应用入口
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import (
    routes_chat,
    routes_graph,
    routes_knowledge,
    routes_mcp,
    routes_milvus,
    routes_session,
    routes_skills,
    routes_upload,
)
from app.api import routes_tasks  # D 轮：异步任务
from app.api import routes_analysis  # E 轮：分析 Agent（P-O-R）
from app.core.settings import settings

app = FastAPI(
    title="Data Agent",
    description="智能数据分析 Agent 平台",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 启动事件
@app.on_event("startup")
async def startup_event():
    print("Data Agent starting...")
    # 持久化建表初始化（幂等；SQLite 首启会自动建 ./data 目录与库文件）
    from app.db import ensure_initialized
    ensure_initialized()

# 关闭事件
@app.on_event("shutdown")
async def shutdown_event():
    print("Data Agent shutting down...")

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}

# 注册路由
app.include_router(routes_chat.router, prefix="/api")
app.include_router(routes_milvus.router, prefix="/api/milvus")
app.include_router(routes_session.router, prefix="/api")
app.include_router(routes_upload.router, prefix="/api")
app.include_router(routes_skills.router, prefix="/api")
app.include_router(routes_mcp.router, prefix="/api")
app.include_router(routes_graph.router, prefix="/api")  # noqa: E402,E702 —— E 轮：知识图谱
app.include_router(routes_knowledge.router, prefix="/api")
app.include_router(routes_tasks.router, prefix="/api")
app.include_router(routes_analysis.router, prefix="/api")



