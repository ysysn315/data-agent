# 使用 pydantic-settings 进行配置管理
# 支持自定义 base_url 和 api_key，兼容 OpenAI 接口规范
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 应用配置
    app_port: int = 9900
    upload_dir: str = "./uploads"
    save_dir: str = "./saves"          # 持久化目录（skills / mcp 配置等）
    debug: bool = False

    # CORS 配置
    CORS_ORIGINS: list[str] = ["*"]

    # LLM 配置（支持自定义 base_url，如美团 FRIDAY API）
    # api_key 允许为空以便离线测试，真正创建 LLM 时校验（显式失败）
    llm_model: str = "qwen3-max"
    llm_api_key: str = ""
    llm_base_url: Optional[str] = None  # 自定义 endpoint，如 "https://aigc.sankuai.com/v1/openai/native"
    llm_temperature: float = 0.1
    llm_streaming: bool = False

    # Embedding 配置（支持本地 Ollama 或 OpenAI 兼容接口）
    embedding_provider: str = "openai"  # openai / ollama / dashscope
    embedding_model: str = "text-embedding-v4"
    embedding_api_key: str = ""
    embedding_base_url: Optional[str] = None  # 如 "http://localhost:11434/v1"
    embedding_device: str = ""

    # 模型缓存目录（BGE reranker 等本地模型）
    MODEL_CACHE_DIR: str = "./models"

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "knowledge_base"

    # RAG 配置
    doc_chunk_max_size: int = 800
    doc_chunk_overlap: int = 100
    rag_top_k: int = 3

    # 知识库工具开关：Milvus 未部署时设为 False，chat 不再依赖 Milvus
    enable_kb_tool: bool = False

    # 演示数据源（Kaggle Brazilian E-Commerce 导入的 SQLite）
    sqlite_db_path: str = "./data/ecommerce.db"

    # Redis 配置
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    session_expire_seconds: int = 86400

    # 外部工具 API
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"

    # Langfuse 调用链追踪（可选，默认关闭）
    # enabled=False 或 key 为空时完全跳过，不 import langfuse，不影响主流程
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"  # 云端；自建填自己的实例地址

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()