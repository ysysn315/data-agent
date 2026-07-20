# 会话管理路由
# TODO: Phase 2 - 实现会话管理端点
# - DELETE /api/chat/clear/{session_id} - 清空指定会话
# - GET /api/chat/sessions - 列出所有活跃会话
#
# 依赖注入：
# - get_session_store() - 获取会话存储实例（单例模式）
#
# 注意事项：
# - SessionStore 应该是全局单例
# - 清空会话返回成功/失败状态
# - 列出会话返回会话 ID 列表和数量
from fastapi import APIRouter, Depends
from app.services.session_store import SessionStore
from loguru import logger
from app.core.settings import Settings, get_settings

router = APIRouter(tags=["session"])
_session_store_instance = None


def get_session_store() -> SessionStore:
    global _session_store_instance
    if _session_store_instance is None:
        settings=get_settings()
        _session_store_instance = SessionStore(settings=settings,max_history=6)
    return _session_store_instance


@router.delete("/chat/clear/{session_id}")
async def clear_session(session_id: str, session_store: SessionStore = Depends(get_session_store)):
    try:
        success = session_store.clear_session(session_id)
        if success:
            return {
                "status": "success",
                "message": f"会话{session_id}已清空"
            }
        else:
            return {
                "status": "not_found",
                "message": f"会话{session_id}不存在"
            }
    except Exception as e:
        logger.error(f"清空会话失败: {str(e)}")
        return {
            "status": "error",
            "message": f"清空失败: {str(e)}"
        }


@router.get("/chat/sessions")
async def list_session(session_store: SessionStore = Depends(get_session_store)):
    try:
        session_ids = session_store.get_all_session_ids()
        session_count = session_store.get_session_count()
        return {
            "status": "success",
            "count": session_count,
            "sessions": session_ids
        }
    except Exception as e:
        logger.error(f"获取会话列表失败: {str(e)}")
        return {
            "status": "error",
            "message": f"获取失败: {str(e)}"
        }
