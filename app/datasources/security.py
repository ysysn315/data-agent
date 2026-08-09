"""数据源凭证加密、路径约束和异常脱敏。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.datasources.models import DataSourceConfigError

_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@")


class CredentialCipher:
    """使用部署侧 Fernet key 加解密远程数据库凭证。

    key 为空时仍可承载无凭证 SQLite，但任何 encrypt/decrypt 都显式失败，绝不
    静默降级成明文。
    """

    def __init__(self, key: str):
        self._key = (key or "").strip()

    def _fernet(self) -> Fernet:
        if not self._key:
            raise DataSourceConfigError(
                "远程数据源需要配置 DATASOURCE_SECRET_KEY（Fernet key），不允许明文保存凭证"
            )
        try:
            return Fernet(self._key.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise DataSourceConfigError("DATASOURCE_SECRET_KEY 不是合法 Fernet key") from exc

    def encrypt(self, credentials: dict[str, str]) -> str | None:
        cleaned = {str(k): str(v) for k, v in credentials.items() if v is not None}
        if not cleaned:
            return None
        payload = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet().encrypt(payload).decode("ascii")

    def decrypt(self, token: str | None) -> dict[str, str]:
        if not token:
            return {}
        try:
            payload = self._fernet().decrypt(token.encode("ascii"))
            decoded = json.loads(payload.decode("utf-8"))
        except (InvalidToken, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise DataSourceConfigError("数据源凭证无法解密，请检查 DATASOURCE_SECRET_KEY") from exc
        if not isinstance(decoded, dict):
            raise DataSourceConfigError("数据源凭证格式无效")
        return {str(k): str(v) for k, v in decoded.items()}


def resolve_sqlite_path(raw_path: str, allowed_root: str) -> Path:
    """把 SQLite 路径限制在部署配置的根目录下，并要求目标为现有普通文件。"""
    if not raw_path or not raw_path.strip():
        raise DataSourceConfigError("SQLite path 不能为空")

    root = Path(allowed_root).expanduser().resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise DataSourceConfigError(f"SQLite 文件必须位于允许目录内: {root}") from exc

    if not candidate.exists() or not candidate.is_file():
        raise DataSourceConfigError(f"SQLite 文件不存在: {candidate}")
    return candidate


def sanitize_error(exc: BaseException, secrets: dict[str, Any] | None = None) -> str:
    """生成可落库/返回的短错误文本，移除 URL 用户信息和已知凭证。"""
    message = _URL_CREDENTIALS.sub(r"\g<scheme>***@", str(exc))
    for value in (secrets or {}).values():
        text = str(value or "")
        if text:
            message = message.replace(text, "***")
    message = " ".join(message.split())
    return message[:1000] or type(exc).__name__
