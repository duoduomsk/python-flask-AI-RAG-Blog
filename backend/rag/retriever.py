# backend/rag/retriever.py
import logging

from qdrant_client.http.models import Filter, FieldCondition, MatchValue

from backend.config import QDRANT_COLLECTION, RAG_TOP_K, RAG_SCORE_THRESHOLD
from backend.rag.vector_store import get_qdrant, ensure_collection
from backend.rag.embedder import embed_query

logger = logging.getLogger(__name__)


def retrieve(query: str, top_k: int | None = None,
             category: str | None = None) -> list[dict]:
    """检索 top_k 片段。Qdrant 挂掉 → 返回 []（fail-open）。"""
    if not query or not query.strip():
        return []

    top_k = top_k or RAG_TOP_K
    try:
        ensure_collection()
        vec = embed_query(query)
        if not vec:
            return []
        client = get_qdrant()

        must = []
        if category:
            must.append(FieldCondition(key="category", match=MatchValue(value=category)))
        flt = Filter(must=must) if must else None

        resp = client.query_points(
            collection_name=QDRANT_COLLECTION,
            query=vec,
            limit=top_k,
            score_threshold=RAG_SCORE_THRESHOLD,
            query_filter=flt,
            with_payload=True,
        )
        hits = resp.points or []

        # 同一篇文章最多取 2 块，提升来源多样性
        per_article, results = {}, []
        for h in hits:
            aid = h.payload.get("article_id")
            if per_article.get(aid, 0) >= 2:
                continue
            per_article[aid] = per_article.get(aid, 0) + 1
            results.append({
                "text": h.payload.get("text", ""),
                "title": h.payload.get("title", ""),
                "article_id": aid,
                "chunk_index": h.payload.get("chunk_index"),
                "heading_path": h.payload.get("heading_path", ""),
                "score": float(h.score),
            })
            if len(results) >= top_k:
                break
        return results

    except Exception as e:
        logger.warning("retrieve failed, fallback to empty: %s", e)
        return []