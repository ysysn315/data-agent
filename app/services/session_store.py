from typing import List
import json

import redis
from loguru import logger

from app.core.settings import Settings


class SessionStore:
    """Redis-backed session store with sliding-window history support."""

    key_prefix = "session"
    key_history = ":history"
    key_summary = ":summary"
    key_sessions_set = "sessions"

    def __init__(self, settings: Settings, max_history: int = 6):
        self.max_history = max_history
        self.max_messages = 2 * max_history
        self.settings = settings
        self.redis_client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password or None,
            decode_responses=True,
        )
        logger.info(f"会话存储初始化完成，最大历史轮数: {max_history}")

    def _get_history_key(self, session_id: str) -> str:
        return f"{self.key_prefix}{session_id}{self.key_history}"

    def _get_summary_key(self, session_id: str) -> str:
        return f"{self.key_prefix}{session_id}{self.key_summary}"

    def add_message(self, session_id: str, role: str, content: str) -> None:
        """Append a message to the raw session history."""

        message = {"role": role, "content": content}
        history_key = self._get_history_key(session_id)
        self.redis_client.rpush(history_key, json.dumps(message, ensure_ascii=False))
        self.redis_client.expire(history_key, self.settings.session_expire_seconds)
        self.redis_client.sadd(self.key_sessions_set, session_id)
        logger.debug(f"会话 {session_id} 添加消息: {role} - {content[:50]}...")

    def get_history(self, session_id: str) -> List[dict]:
        """Get the current raw history window for a session."""

        history_key = self._get_history_key(session_id)
        messages = self.redis_client.lrange(history_key, 0, -1)
        history = [json.loads(msg) for msg in messages]
        logger.debug(f"获取会话 {session_id} 历史: {len(history)} 条消息")
        return history

    def get_summary(self, session_id: str) -> str:
        """Get the rolling summary of earlier turns."""

        summary_key = self._get_summary_key(session_id)
        return self.redis_client.get(summary_key) or ""

    def set_summary(self, session_id: str, summary: str) -> None:
        """Persist the rolling summary for a session."""

        summary_key = self._get_summary_key(session_id)
        if summary:
            self.redis_client.set(summary_key, summary)
            self.redis_client.expire(summary_key, self.settings.session_expire_seconds)
        else:
            self.redis_client.delete(summary_key)

    def get_messages_to_summarize(self, session_id: str) -> List[dict]:
        """
        Return the older messages that should be compacted into the rolling summary.

        The newest max_messages stay in the sliding window; older messages are eligible
        for summarization.
        """

        history = self.get_history(session_id)
        if len(history) <= self.max_messages:
            return []
        return history[:-self.max_messages]

    def apply_summary_and_trim(self, session_id: str, summary: str) -> None:
        """
        Save the updated summary, then trim the raw history back to the sliding window.

        This must be called only after the new summary has been generated successfully.
        """

        self.set_summary(session_id, summary)
        history_key = self._get_history_key(session_id)
        self.redis_client.ltrim(history_key, -self.max_messages, -1)
        self.redis_client.expire(history_key, self.settings.session_expire_seconds)

    def clear_session(self, session_id: str) -> bool:
        history_key = self._get_history_key(session_id)
        summary_key = self._get_summary_key(session_id)
        if self.redis_client.exists(history_key) or self.redis_client.exists(summary_key):
            self.redis_client.delete(history_key)
            self.redis_client.delete(summary_key)
            self.redis_client.srem(self.key_sessions_set, session_id)
            logger.debug(f"已清除对话 {session_id} 的历史和摘要")
            return True

        logger.warning(f"对话 {session_id} 不存在，无需清空")
        return False

    def get_session_count(self) -> int:
        return self.redis_client.scard(self.key_sessions_set)

    def get_all_session_ids(self) -> List[str]:
        return list(self.redis_client.smembers(self.key_sessions_set))
