from pathlib import Path
from typing import Any, Dict, Optional
import time


DOC_TYPE_MAP = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".pdf": "pdf",
    ".docx": "docx",
    ".html": "html",
    ".htm": "html",
    ".csv": "csv",
    ".json": "json",
    ".xlsx": "excel",
    ".xls": "excel",
}


def infer_doc_type(filename: str) -> str:
    return DOC_TYPE_MAP.get(Path(filename).suffix.lower(), "text")


def infer_title(filename: str) -> str:
    return Path(filename).stem


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_base_metadata(
    filename: str,
    title: Optional[str] = None,
    ingested_at: Optional[int] = None,
) -> Dict[str, Any]:
    timestamp = _coerce_int(ingested_at) if ingested_at is not None else None
    if timestamp is None:
        timestamp = int(time.time())

    return {
        "source": filename,
        "title": (title or infer_title(filename)).strip(),
        "doc_type": infer_doc_type(filename),
        "sheet_name": None,
        "section_path": None,
        "ingested_at": timestamp,
        "timestamp": timestamp,
    }


def extract_section_path(metadata: Dict[str, Any]) -> Optional[str]:
    parts = [
        str(metadata.get("header1", "")).strip(),
        str(metadata.get("header2", "")).strip(),
        str(metadata.get("header3", "")).strip(),
    ]
    path = "/".join(part for part in parts if part)
    return path or None


def merge_chunk_metadata(
    base_metadata: Dict[str, Any],
    extra_metadata: Optional[Dict[str, Any]] = None,
    chunk_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    metadata = dict(base_metadata)
    metadata.update(extra_metadata or {})
    metadata.update(chunk_metadata or {})
    metadata["source"] = base_metadata["source"]
    metadata["section_path"] = extract_section_path(metadata)
    return metadata


def normalize_metadata_filters(
    metadata_filters: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not metadata_filters:
        return {}

    normalized: Dict[str, Any] = {}
    aliases = {
        "timestamp_from": "ingested_at_from",
        "timestamp_to": "ingested_at_to",
    }
    allowed_fields = {
        "source",
        "title",
        "title_contains",
        "doc_type",
        "sheet_name",
        "section_path",
        "section_path_contains",
        "timestamp",
        "ingested_at_from",
        "ingested_at_to",
    }

    for raw_key, raw_value in metadata_filters.items():
        key = aliases.get(raw_key, raw_key)
        if key not in allowed_fields or raw_value is None:
            continue

        if key in {"ingested_at_from", "ingested_at_to", "timestamp"}:
            value = _coerce_int(raw_value)
            if value is not None:
                normalized[key] = value
            continue

        value = str(raw_value).strip()
        if value:
            normalized[key] = value

    return normalized


def matches_metadata_filters(
    metadata: Optional[Dict[str, Any]],
    metadata_filters: Optional[Dict[str, Any]],
) -> bool:
    filters = normalize_metadata_filters(metadata_filters)
    if not filters:
        return True

    current = metadata or {}

    exact_fields = ("source", "title", "doc_type", "sheet_name", "section_path", "timestamp")
    for field in exact_fields:
        expected = filters.get(field)
        if expected is None:
            continue

        if field == "timestamp":
            actual = _coerce_int(current.get(field))
            if actual is None or actual != expected:
                return False
            continue

        actual = str(current.get(field, "")).strip()
        if actual != expected:
            return False

    title_contains = filters.get("title_contains")
    if title_contains:
        title = str(current.get("title", "")).lower()
        if title_contains.lower() not in title:
            return False

    section_path_contains = filters.get("section_path_contains")
    if section_path_contains:
        section_path = str(current.get("section_path", "")).lower()
        if section_path_contains.lower() not in section_path:
            return False

    ingested_at = _coerce_int(current.get("ingested_at"))
    if "ingested_at_from" in filters:
        if ingested_at is None or ingested_at < filters["ingested_at_from"]:
            return False
    if "ingested_at_to" in filters:
        if ingested_at is None or ingested_at > filters["ingested_at_to"]:
            return False

    return True
