# Milvus 向量数据库客户端
# TODO: 任务 6.1 - 实现 MilvusClient 类
# - __init__(self, settings: Settings)
# - async def connect(self) -> None
# - async def health_check(self) -> bool
# - async def ensure_collection(self) -> None
# Phase 1: 仅连接和健康检查
# Phase 2: 将添加 create_collection、insert、search 方法
from functools import lru_cache
import asyncio
from pymilvus import (
    connections,
    utility,
    Collection,           # 新增
    CollectionSchema,     # 新增
    FieldSchema,          # 新增
    DataType              # 新增
)
from app.core.settings import Settings
from loguru import logger


class MilvusClient:
    """Milvus 向量数据库客户端"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.collection_name = settings.milvus_collection
        self.collection = None

    async def connect(self) -> None:
        try:
            if connections.has_connection("default"):
                logger.info("milvus连接")
                return
            connections.connect(
                alias="default",
                host=self.settings.milvus_host,
                port=self.settings.milvus_port
            )
            logger.info("连接注册成功")
        except Exception as e:
            logger.error(f"连接milvus失败：{str(e)}")
            raise Exception(f"无法连接到 Milvus: {str(e)}")

    async def health_check(self) -> bool:
        try:
            if not connections.has_connection("default"):
                logger.warning("milvus连接不存在")
                return False
            utility.list_collections()
            logger.debug("milvus健康检查通过")
            return True
        except Exception as e:
            logger.error(f"Milvus 健康检查失败: {str(e)}")
            return False
    async def ensure_collection(self) -> None:
         try:
              if utility.has_collection(self.collection_name):
                   logger.info(f"Collection '{self.collection_name}' 已存在")
                   self.collection=Collection(self.collection_name)
                   self.collection.load()
              else:
                   logger.info(
                        f"Collection '{self.collection_name}' 不存在，" )
                   await self.create_collection()

         except Exception as e:
              logger.error(f"检查 collection 失败: {str(e)}")
              raise Exception(f"无法检查 collection: {str(e)}")
    async def create_collection(self)->None:
        try:
            if utility.has_collection(self.collection_name):
                logger.info(f"Collection '{self.collection_name}' 已存在,跳过创建")
                self.collection=Collection(self.collection_name)
                return
            fields=[
                FieldSchema(name="id",dtype=DataType.INT64,is_primary=True,auto_id=True),
                FieldSchema(name="vector",dtype=DataType.FLOAT_VECTOR,dim=1024),
                FieldSchema(name="content",dtype=DataType.VARCHAR,max_length=65535),
                FieldSchema(name="metadata",dtype=DataType.JSON)
            ]
            schema=CollectionSchema(
                fields=fields,
                description="知识库向量存储"
            )
            self.collection=Collection(
                name=self.collection_name,
                schema=schema
            )
            index_params = {
           "metric_type": "IP",
           "index_type": "IVF_FLAT",
           "params": {"nlist": 128}   }
            self.collection.create_index(
                field_name="vector",
                index_params=index_params
            )
            self.collection.load()
            logger.info(f"Collection '{self.collection_name}' 创建成功")
        except Exception as e:
            logger.error(f"创建 collection 失败: {str(e)}")
            raise Exception(f"创建 collection 失败: {str(e)}")


    async def close(self)->None:
         try:
              if connections.has_connection("default"):
                   connections.disconnect("default")
                   logger.info("Milvus 连接已关闭")
         except Exception as e:
             logger.error(f"关闭 Milvus 连接失败: {str(e)}")



