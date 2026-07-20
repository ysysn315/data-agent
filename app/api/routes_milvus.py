# Milvus 健康检查路由
# TODO: 任务 6.2 - 实现 Milvus 健康检查端点
# - GET /milvus/health - 检查 Milvus 连接状态
from fastapi import APIRouter, Depends
from app.clients.milvus_client import MilvusClient
from app.core.settings import Settings, get_settings
from loguru import logger

# 创建路由器
router = APIRouter(tags=["milvus"])


def get_milvus_client(settings: Settings = Depends(get_settings)):
    return MilvusClient(settings)
@router.get("/health")
async def milvus_health(milvus_client:MilvusClient=Depends(get_milvus_client)):
    try:
        await milvus_client.connect()
        is_health=await milvus_client.health_check()
        if is_health:
            return {
                "status":"healthy",
                "message":"Milvus连接正常"
            }
        else:
            return {
                "status":"unhealthy",
                "message":"Milvus连接失败"
            }
    except Exception as e:
        logger.error(f"健康检查失败：{e}")
        return {
            "status":"error",
            "message": f"健康检查异常: {str(e)}"
        }
