"""HTTP 请求的数据源归属与图谱作用域解析。"""

from __future__ import annotations

from fastapi import HTTPException, status

from app.core.settings import settings
from app.datasources.models import DataSourceNotFoundError
from app.datasources.service import DataSourceService, normalize_workspace_id
from app.graph.scope import GraphScope


async def resolve_graph_scope(
    datasource_id: int | None,
    user: dict | None,
    datasource_service: DataSourceService,
) -> GraphScope:
    """解析 workspace 并校验可选数据源归属，统一各路由的鉴权/404口径。"""

    workspace_id = normalize_workspace_id(user.get("workspace_id") if user else None)
    if datasource_id is not None:
        if settings.auth_enabled and user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="选择数据源时需要有效的 API Key")
        try:
            await datasource_service.get_source(datasource_id, workspace_id)
        except DataSourceNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return GraphScope.from_ids(workspace_id, datasource_id)
