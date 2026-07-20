# FastAPI 应用入口
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import routes_chat, routes_milvus, routes_session, routes_upload
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



