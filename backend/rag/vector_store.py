# backend/rag/vector_store.py
import logging
from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    VectorParams, Distance, PayloadSchemaType,
)

from backend.config import QDRANT_URL, QDRANT_COLLECTION, EMBEDDING_DIM

logger = logging.getLogger(__name__)

_client = None


def get_qdrant() -> QdrantClient:
    """Qdrant 客户端单例（fail-closed：挂了直接抛，让 Celery 重试）。"""
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL, timeout=10)
    return _client


def ensure_collection():
    """幂等：不存在则创建 collection，并补建 article_id 的 payload 索引。"""
    client = get_qdrant()
    if not client.collection_exists(QDRANT_COLLECTION):
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
        logger.info("Qdrant collection created: %s", QDRANT_COLLECTION)

    try:
        client.create_payload_index(
            collection_name=QDRANT_COLLECTION,
            field_name="article_id",
            field_schema=PayloadSchemaType.INTEGER,
        )
    except Exception as e:
        # 索引已存在会报错，忽略即可
        logger.debug("payload index article_id: %s", e)