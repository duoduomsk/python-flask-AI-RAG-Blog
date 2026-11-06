# backend/rag/vector_store.py
import logging
from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    VectorParams, Distance, PayloadSchemaType, SparseVectorParams
)

from backend.config import QDRANT_URL, QDRANT_COLLECTION, EMBEDDING_DIM

logger = logging.getLogger(__name__)

_client = None

def get_qdrant() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL, timeout=10)
    return _client

def ensure_collection():
    """幂等：不存在则创建含 dense + sparse 的 collection，并补建索引。"""
    client = get_qdrant()
    if not client.collection_exists(QDRANT_COLLECTION):
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config={
                "dense": VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE)
            },
            sparse_vectors_config={
                "sparse": SparseVectorParams()
            }
        )
        logger.info("Qdrant collection created with hybrid vectors: %s", QDRANT_COLLECTION)

    # 建立 payload 索引，供混合检索时过滤用（如果已存在会报错，忽略即可）
    for field, schema in [
        ("article_id", PayloadSchemaType.INTEGER),
        ("category", PayloadSchemaType.KEYWORD),
        ("tags", PayloadSchemaType.KEYWORD),
        ("chunk_type", PayloadSchemaType.KEYWORD),
    ]:
        try:
            client.create_payload_index(
                collection_name=QDRANT_COLLECTION,
                field_name=field,
                field_schema=schema,
            )
        except Exception as e:
            logger.debug(f"payload index {field}: {e}")