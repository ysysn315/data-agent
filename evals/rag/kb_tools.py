import json
from pathlib import Path
from typing import Iterable, List

from loguru import logger
from pymilvus import utility

from app.clients.milvus_client import MilvusClient
from app.core.settings import Settings, get_settings
from app.rag.chunking import DocumentChunker, get_strategy_by_filename
from app.rag.embeddings import EmbeddingService
from app.rag.vector_store import VectorStore
from app.services.vector_index_service import VectorIndexService


# 统一从仓库根目录定位测试文档，避免依赖当前命令执行位置。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 与上传接口保持一致，避免批量导入时支持的格式不一致。
ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".html", ".htm", ".csv", ".json", ".xlsx", ".xls"}

# 这两批文档就是 embedding 对比测试时的固定样本。
DEFAULT_TEST_DOC_DIRS = [
    PROJECT_ROOT / "aiops-docs",
    PROJECT_ROOT / "aiops-docs-noise",
]


def load_settings() -> Settings:
    # 脚本场景下显式复用现有配置加载逻辑。
    return get_settings()


def collect_test_documents(doc_dirs: Iterable[Path] = DEFAULT_TEST_DOC_DIRS) -> List[Path]:
    files: List[Path] = []
    seen_names = set()

    for doc_dir in doc_dirs:
        if not doc_dir.exists():
            logger.warning(f"测试文档目录不存在，跳过: {doc_dir}")
            continue

        for path in sorted(doc_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue

            # 仍然沿用“文件名作为 source”的现有约定，所以这里提前拦截重名。
            if path.name in seen_names:
                raise ValueError(f"发现重复文件名，当前实现无法区分同名 source: {path.name}")

            seen_names.add(path.name)
            files.append(path)

    return files


async def clear_current_knowledge_base(settings: Settings) -> dict:
    milvus_client = MilvusClient(settings)
    await milvus_client.connect()

    dropped = False
    try:
        if utility.has_collection(settings.milvus_collection):
            utility.drop_collection(settings.milvus_collection)
            dropped = True
            logger.info(f"已删除 collection: {settings.milvus_collection}")

        # 清空后立即重建，保持后续导入和服务启动逻辑不变。
        await milvus_client.ensure_collection()
        logger.info(f"已重建 collection: {settings.milvus_collection}")
        return {
            "collection": settings.milvus_collection,
            "dropped": dropped,
            "status": "success",
        }
    finally:
        await milvus_client.close()


async def batch_index_documents(settings: Settings, doc_paths: Iterable[Path]) -> dict:
    milvus_client = MilvusClient(settings)
    await milvus_client.connect()
    await milvus_client.ensure_collection()

    embedding_service = EmbeddingService(settings)
    vector_store = VectorStore(milvus_client, embedding_service)

    results = []
    total_chunks = 0

    try:
        for path in doc_paths:
            # 每个文件按现有规则自动选择分块策略，避免和上传链路行为不一致。
            chunker = DocumentChunker(
                strategy=get_strategy_by_filename(path.name),
                max_size=settings.doc_chunk_max_size,
                overlap=settings.doc_chunk_overlap,
            )
            index_service = VectorIndexService(chunker, vector_store)
            result = await index_service.index_document(
                file_path=str(path),
                filename=path.name,
                title=path.stem,
            )
            results.append(result)
            total_chunks += result.get("chunks", 0)
    finally:
        await milvus_client.close()

    success_count = sum(1 for item in results if item.get("status") == "success")
    failed_count = len(results) - success_count

    return {
        "collection": settings.milvus_collection,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.embedding_model,
        "indexed_files": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
        "total_chunks": total_chunks,
        "results": results,
    }


def to_pretty_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
