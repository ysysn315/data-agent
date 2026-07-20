# 向量索引服务
# TODO: 任务 13.2 - 实现 VectorIndexService 类
# 向量索引服务
# TODO: 任务 13.2 - 实现 VectorIndexService 类

from typing import Dict
from app.rag.chunking import DocumentChunker
from app.rag.vector_store import VectorStore
from loguru import logger
from app.rag.load import DocumentLoader
from app.rag.metadata_filters import build_base_metadata, merge_chunk_metadata


class VectorIndexService:
    def __init__(self, chunker: DocumentChunker, vector_store: VectorStore):
        self.chunker = chunker
        self.vector_store = vector_store
        logger.info("向量索引服务初始化完成")

    async def index_document(self, file_path: str, filename: str, title: str = "") -> Dict:
        try:
            records = DocumentLoader.load_records(file_path)
            if not records:
                logger.warning(f"文件{filename}为空")
                return {"filename": filename, "chunks": 0, "status": "failed"}

            base_metadata = build_base_metadata(filename, title=title or None)
            chunks = []
            for record in records:
                text = record.get("content", "")
                if not text or not str(text).strip():
                    continue

                extra_metadata = record.get("metadata") or {}
                for chunk in self.chunker.chunk_text(str(text), filename):
                    chunk_metadata = merge_chunk_metadata(
                        base_metadata=base_metadata,
                        extra_metadata=extra_metadata,
                        chunk_metadata=chunk.get("metadata") or {},
                    )
                    chunks.append(
                        {
                            "content": chunk["content"],
                            "metadata": chunk_metadata,
                        }
                    )

            if not chunks:
                logger.warning(f"文件{filename}切分后无内容")
                return {"filename": filename, "chunks": 0, "status": "failed"}

            await self.vector_store.insert(chunks)
            logger.info(f"文档 {filename} 索引完成，共 {len(chunks)} 个 chunks")
            return {
                "filename": filename,
                "chunks": len(chunks),
                "status": "success"
            }

        except Exception as e:
            logger.error(f"索引文档 {filename} 失败: {str(e)}")
            return {
                "filename": filename,
                "chunks": 0,
                "status": "failed"
            }
