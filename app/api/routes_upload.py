# 文件上传路由
# TODO: 任务 13.3 - 实现文件上传 API
# 文件上传路由
# TODO: 任务 13.3 - 实现文件上传 API

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from loguru import logger

from app.core.dependencies import get_vector_store
from app.core.settings import Settings, get_settings
from app.rag.chunking import DocumentChunker, get_strategy_by_filename
from app.schemas.upload import DocumentResponse, UploadResponse
from app.services.vector_index_service import VectorIndexService

router = APIRouter()


@router.get("/documents", response_model=list[DocumentResponse])
async def list_documents(vector_store=Depends(get_vector_store)):
    """列出 Milvus 中真实存在的文档，按 source 聚合 chunk。"""
    try:
        return await vector_store.list_documents()
    except Exception as exc:
        logger.error(f"读取文档列表失败: {exc}")
        raise HTTPException(status_code=503, detail="知识库暂不可用，请确认 Milvus 已启动") from exc


@router.post("/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...), title: Optional[str] = Form(None), settings: Settings = Depends(get_settings)
):
    try:
        allowed_extensions = [".txt", ".md", ".pdf", ".docx", ".html", ".htm", ".csv", ".json", ".xlsx", ".xls"]
        safe_filename = Path(file.filename or "").name
        if not safe_filename or safe_filename in {".", ".."}:
            raise HTTPException(status_code=400, detail="文件名不能为空")
        file_ext = os.path.splitext(safe_filename)[1].lower()
        if file_ext not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式: {file_ext}，支持 .txt, .md, .pdf, .docx, .html, .csv, .json, .xlsx, .xls",
            )
        logger.info(f"接收到文件上传请求: {safe_filename}")

        # 2. 确保 uploads 目录存在
        os.makedirs(settings.upload_dir, exist_ok=True)

        file_path = os.path.join(settings.upload_dir, safe_filename)
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        logger.info(f"文件{safe_filename}保存成功:{file_path}")
        strategy = get_strategy_by_filename(safe_filename)
        logger.info(f"文件 {safe_filename} 使用分块策略: {strategy.value}")

        vector_store = await get_vector_store()
        # 同名文件视为重新索引，避免 Milvus 与列表出现重复文档。
        await vector_store.delete_by_source(safe_filename)
        chunker = DocumentChunker(
            strategy=strategy, max_size=settings.doc_chunk_max_size, overlap=settings.doc_chunk_overlap
        )
        index_service = VectorIndexService(chunker, vector_store)
        result = await index_service.index_document(file_path, safe_filename, title=title or "")
        return UploadResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"上传文件失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")
