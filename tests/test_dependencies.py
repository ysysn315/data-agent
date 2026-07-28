"""依赖注入单例测试

回归：get_chat_agent 曾在持有 _init_lock 时调用 get_skill_service（同一把锁），
asyncio.Lock 不可重入导致死锁——首次 chat 请求永久挂起。
"""

import asyncio

from app.core import dependencies
from app.core.settings import settings


async def test_get_chat_agent_no_deadlock(tmp_path, monkeypatch):
    """get_chat_agent 必须能在数秒内完成初始化（曾死锁挂起）"""
    monkeypatch.setattr(settings, "llm_api_key", "sk-dummy-for-test")
    monkeypatch.setattr(settings, "save_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_kb_tool", False)
    dependencies.reset_singletons()

    agent = await asyncio.wait_for(dependencies.get_chat_agent(), timeout=10)
    assert agent is not None
    # 单例：再次获取是同一实例
    assert await dependencies.get_chat_agent() is agent

    dependencies.reset_singletons()


async def test_skill_service_singleton_loads_builtins(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "save_dir", str(tmp_path))
    dependencies.reset_singletons()

    service = await dependencies.get_skill_service()
    skills = await service.list_skills(enabled_only=True)
    assert len(skills) >= 4  # 内置技能已加载

    dependencies.reset_singletons()
