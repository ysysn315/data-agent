# 文件上传路由
# TODO: 任务 13.3 - 实现文件上传 API
# 文件上传路由
# TODO: 任务 13.3 - 实现文件上传 API

from typing import Optional
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from app.schemas.upload import UploadResponse
from app.services.vector_index_service import VectorIndexService
from app.core.settings import Settings, get_settings
from app.clients.milvus_client import MilvusClient
from app.rag.embeddings import EmbeddingService
from app.rag.chunking import DocumentChunker,get_strategy_by_filename
from app.rag.vector_store import VectorStore
from loguru import logger
import os
router = APIRouter()


@router.post("/upload", response_model=UploadResponse)
async def upload_file(
        file: UploadFile = File(...),
        title: Optional[str] = Form(None),
        settings: Settings = Depends(get_settings)
):
    try:
        allowed_extensions= [".txt", ".md", ".pdf", ".docx", ".html", ".htm", ".csv", ".json", ".xlsx", ".xls"]
        file_ext=os.path.splitext(file.filename)[1].lower()
        if file_ext not in allowed_extensions:
            raise HTTPException(
                status_code=400,
               detail=f"不支持的文件格式: {file_ext}，支持 .txt, .md, .pdf, .docx, .html, .csv, .json, .xlsx, .xls"
            )
        logger.info(f"接收到文件上传请求: {file.filename}")

        # 2. 确保 uploads 目录存在
        os.makedirs(settings.upload_dir, exist_ok=True)

        file_path=os.path.join(settings.upload_dir,file.filename)
        with open(file_path,"wb") as f:
            content=await file.read()
            f.write(content)
        logger.info(f"文件{file.filename}保存成功:{file_path}")
        strategy=get_strategy_by_filename(file.filename)
        logger.info(f"文件 {file.filename} 使用分块策略: {strategy.value}")

        milvus_client = MilvusClient(settings)
        await milvus_client.connect()
        await milvus_client.ensure_collection()

        embedding_service=EmbeddingService(settings)
        vector_store=VectorStore(milvus_client,embedding_service)
        chunker=DocumentChunker(
            strategy=strategy, max_size=settings.doc_chunk_max_size,
            overlap=settings.doc_chunk_overlap
        )
        index_service=VectorIndexService(chunker,vector_store)
        result=await index_service.index_document(file_path,file.filename,title=title or "")
        return UploadResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"上传文件失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")

















