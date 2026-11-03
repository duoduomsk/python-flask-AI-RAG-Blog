# backend/rag/embedder.py
import logging
import time
from openai import OpenAI

from backend.config import (
    EMBEDDING_BASE_URL, EMBEDDING_API_KEY, EMBEDDING_MODEL,
)

logger = logging.getLogger(__name__)

_client = None


def get_embed_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=EMBEDDING_BASE_URL, api_key=EMBEDDING_API_KEY)
    return _client


def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    批量 embedding。DashScope 单批上限 10 条；空串会被过滤，但保留下标对齐。
    """
    if not texts:
        return []

    indexed = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
    result: list[list[float]] = [[] for _ in texts]
    if not indexed:
        return result

    BATCH = 10
    for start in range(0, len(indexed), BATCH):
        batch = indexed[start:start + BATCH]
        batch_texts = [t for _, t in batch]
        resp = get_embed_client().embeddings.create(
            model=EMBEDDING_MODEL, input=batch_texts,
        )
        # 返回顺序不保证与输入一致 → 必须按 index 排序
        items = sorted(resp.data, key=lambda d: d.index)
        for (orig_idx, _), item in zip(batch, items):
            result[orig_idx] = item.embedding

        # 全量重建时避免 QPS 打满
        if start + BATCH < len(indexed):
            time.sleep(0.2)

    return result


def embed_query(text: str) -> list[float]:
    vecs = embed_texts([text])
    return vecs[0] if vecs else []