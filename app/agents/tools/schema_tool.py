# Schema 目录工具 —— schema-retrieval 技能声明的门控工具（激活后可见）
# 返回请求所选平台数据源或兼容演示库的 M-Schema，供 sql-generation 对齐字段。
from pathlib import Path

from langchain_core.tools import tool
from loguru import logger

from app.datasources.context import current_selection
from app.text2sql.comments_ecommerce import ECOMMERCE_COMMENTS
from app.text2sql.m_schema import generate_m_schema


def create_schema_search_tool(db_path: str, datasource_runtime=None):
    """创建 schema 检索工具。

    请求选择平台数据源时读取其已审核目录；未选择时兼容原演示 SQLite。
    当前实现直接全量返回 M-Schema；表多时再引入按问题召回。
    二期演进：表多时应换成 schema embedding 召回（对齐 SQLBot
    backend/apps/datasource/crud/table.py 的 save_table_embedding + 余弦相似度），
    只返回与 question 最相关的 top-N 张表，避免 token 爆炸。
    """

    @tool
    def schema_search(question: str) -> str:
        """检索数据库表结构，返回 M-Schema 格式（表名、字段、类型、中文注释）。

        生成 SQL 前必须先调用本工具确认真实存在的表名与字段名，不要凭空臆测。

        参数:
            question: 用户的自然语言问题。当前对所选平台 Schema 或演示库均全量返回，
                      尚未据此过滤；后续用于 embedding/关键词召回相关表。
        """
        try:
            selection = current_selection()
            if selection is not None:
                if datasource_runtime is None:
                    return "当前 Agent 未配置平台数据源运行时"
                m_schema = datasource_runtime.get_m_schema(
                    selection.datasource_id,
                    selection.workspace_id,
                )
            else:
                if not Path(db_path).exists():
                    return f"数据库文件不存在: {db_path}（请先导入演示数据集，见 REQUIREMENTS §5）"
                m_schema = generate_m_schema(db_path, comments=ECOMMERCE_COMMENTS)
        except Exception as e:  # noqa: BLE001 —— 工具边界，任何异常都转成模型可读的提示
            logger.warning(f"M-Schema 生成失败: {e}")
            return f"读取表结构失败: {e}"

        if not m_schema.strip():
            return "当前数据源中没有可用的表结构"

        return m_schema

    return schema_search
