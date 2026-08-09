"""把持久化目录渲染为供 Agent 使用的 M-Schema。"""

from __future__ import annotations

from app.datasources.models import ReviewStatus
from app.text2sql.m_schema import build_table_m_schema


def _effective_comment(item: dict, include_pending: bool) -> str:
    status = item.get("review_status")
    if status == ReviewStatus.APPROVED.value:
        value = item.get("reviewed_comment") or ""
        return " ".join(str(value).split())[:1000]
    if include_pending and status == ReviewStatus.PENDING.value:
        value = item.get("ai_comment") or item.get("physical_comment") or ""
        return " ".join(str(value).split())[:1000]
    return " ".join(str(item.get("physical_comment") or "").split())[:1000]


def render_m_schema(catalog: dict, include_pending: bool = False) -> str:
    """渲染目录。

    默认只使用 approved + 物理注释；include_pending 仅供审核预览，不能用于 Agent。
    """
    blocks: list[str] = []
    for table in catalog.get("tables") or []:
        field_comments: dict[str, str] = {}
        columns: list[tuple[str, str]] = []
        for column in table.get("columns") or []:
            name = str(column["column_name"])
            data_type = " ".join(str(column.get("data_type") or "UNKNOWN").split())[:256]
            columns.append((name, data_type))

            parts: list[str] = []
            comment = _effective_comment(column, include_pending)
            if comment:
                parts.append(comment)
            if column.get("primary_key"):
                parts.append("主键")
            references = column.get("references") or {}
            if references.get("table") and references.get("column"):
                ref_table = " ".join(str(references["table"]).split())[:256]
                ref_column = " ".join(str(references["column"]).split())[:256]
                parts.append(f"关联 {ref_table}.{ref_column}")

            synonyms: list[str] = []
            status = column.get("review_status")
            if status == ReviewStatus.APPROVED.value:
                synonyms = list(column.get("reviewed_synonyms") or [])
            elif include_pending and status == ReviewStatus.PENDING.value:
                synonyms = list(column.get("ai_synonyms") or [])
            if synonyms:
                parts.append(f"同义词：{'、'.join(synonyms)}")
            if parts:
                field_comments[name] = "；".join(parts)

        if not columns:
            continue
        blocks.append(
            build_table_m_schema(
                table_name=str(table["table_name"]),
                columns=columns,
                table_comment=_effective_comment(table, include_pending),
                field_comments=field_comments,
            )
        )
    return "\n\n".join(blocks)


def schema_map(catalog: dict) -> dict[str, list[str]]:
    return {
        str(table["table_name"]): [str(column["column_name"]) for column in table.get("columns") or []]
        for table in catalog.get("tables") or []
    }
