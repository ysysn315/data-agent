# 文档切分模块
# TODO: 任务 11.1 - 实现 DocumentChunker 类
from typing import List, Dict,Optional
from langchain_text_splitters import RecursiveCharacterTextSplitter,MarkdownHeaderTextSplitter
from loguru import logger
from enum import Enum
class ChunkStrategy(Enum):
    """分块策略枚举"""
    RECURSIVE = "recursive"      # 通用文本
    MARKDOWN = "markdown"        # Markdown 文档
    PDF = "pdf"                  # PDF 文档
    DOCX = "docx"                # Word 文档
    HTML = "html"                # HTML 网页
    CSV = "csv"                  # CSV 表格
    JSON = "json"                # JSON 数据
class DocumentChunker:
    def __init__(self, strategy:ChunkStrategy,max_size: int = 800, overlap: int = 100):
        self.strategy=strategy
        self.max_size = max_size
        self.overlap = overlap
        self.splitter = self._create_splitter()
        
    def _create_splitter(self):
        if self.strategy==ChunkStrategy.MARKDOWN:
            return MarkdownHeaderTextSplitter(
                headers_to_split_on=[
                    ("#","header1"),
                    ("##","header2"),
                    ("###","header3"),
                ]
            )
        else:
            return RecursiveCharacterTextSplitter(
            chunk_size=self.max_size,
            chunk_overlap=self.overlap,
            separators=["\n\n", "\n", "。", "！", "？", " ", ""]
        )

    def chunk_text(self, text: str, source: str) -> List[Dict]:
        if text is None or not text.strip():
            logger.warning(f"切分的{source}文本为空")
            return []
        if self.strategy==ChunkStrategy.MARKDOWN:
            
            md_chunks=self.splitter.split_text(text)
            return[
                {"content":chunk.page_content,"metadata":{"source":source,"chunk_index":i,"total_chunks":len(md_chunks),"strategy":"markdown",**chunk.metadata}}
                for i,chunk in enumerate(md_chunks)
            ]
        else:
            response = self.splitter.split_text(text)
            chunks = []
            for i, chunk_text in enumerate(response):
                chunks.append(
                    {
                        "content": chunk_text,
                        "metadata": {"source": source, "chunk_index": i, "total_chunks": len(response),"strategy":"recursive"}
                    },
                )
            logger.info(f"文档 {source} 切分完成: {len(chunks)} 个 chunks")
            return chunks
def get_strategy_by_filename(filename: str) -> ChunkStrategy:
    """根据文件名自动选择分块策略"""
    ext = filename.lower().split('.')[-1]
    strategy_map = {
        'md': ChunkStrategy.MARKDOWN,
        'markdown': ChunkStrategy.MARKDOWN,
        'pdf': ChunkStrategy.PDF,
        'docx': ChunkStrategy.DOCX,
        'html': ChunkStrategy.HTML,
        'htm': ChunkStrategy.HTML,
        'csv': ChunkStrategy.CSV,
        'json': ChunkStrategy.JSON,
        'xlsx': ChunkStrategy.CSV,  # Excel 使用 CSV 策略（表格处理）
        'xls': ChunkStrategy.CSV,   # Excel 使用 CSV 策略（表格处理）
    }
    return strategy_map.get(ext, ChunkStrategy.RECURSIVE)

