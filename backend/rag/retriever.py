# backend/rag/retriever.py
import logging
from qdrant_client.http.models import Filter, FieldCondition, MatchValue, MatchAny, SparseVector

from backend.config import QDRANT_COLLECTION, RAG_TOP_K, RAG_CANDIDATE_K
from backend.rag.vector_store import get_qdrant, ensure_collection
from backend.rag.embedder import embed_query
from backend.rag.sparse_embedder import embed_sparse
from backend.rag.fusion import rrf_fusion
from backend.rag.reranker import rerank  # ⚠️ 引入 Rerank 模块

logger = logging.getLogger(__name__)


# 新增调试专用的函数
def retrieve_debug(query: str):
    """后台调试用：返回 dense、sparse 和 rerank 各阶段的原始结果"""
    ensure_collection()
    client = get_qdrant()

    dense_vec = embed_query(query)
    sparse_vec_dict = embed_sparse([query])[0]
    sparse_vec = SparseVector(indices=sparse_vec_dict["indices"], values=sparse_vec_dict["values"])

    # 1. 纯 Dense 结果
    dense_resp = client.query_points(
        collection_name=QDRANT_COLLECTION, query=dense_vec, using="dense", limit=10
    ).points
    dense_results = [{"id": p.id, "score": p.score, "title": p.payload.get("title"), "text": p.payload.get("text")[:50]}
                     for p in dense_resp]

    # 2. 纯 Sparse 结果
    sparse_resp = client.query_points(
        collection_name=QDRANT_COLLECTION, query=sparse_vec, using="sparse", limit=10
    ).points
    sparse_results = [
        {"id": p.id, "score": p.score, "title": p.payload.get("title"), "text": p.payload.get("text")[:50]} for p in
        sparse_resp]

    # 3. RRF 融合结果
    fused = rrf_fusion(dense_resp, sparse_resp, k=60)

    # 4. Rerank 结果 (取前 10 个重排)
    from backend.rag.reranker import rerank
    candidates = [{"id": f["id"], "text": f["payload"].get("parent_text", ""), "title": f["payload"].get("title")} for f
                  in fused]
    reranked = rerank(query, candidates, top_n=5)

    return {
        "dense_results": dense_results,
        "sparse_results": sparse_results,
        "rerank_results": reranked
    }


def retrieve(query: str, top_k: int | None = None,
             category: str | None = None,
             article_ids: list[int] | None = None) -> list[dict]:
    """
    混合检索 -> RRF 融合 -> Rerank 精排。
    """
    if not query or not query.strip():
        return []

    final_top_k = top_k or RAG_TOP_K
    # 召回池要远大于最终需要的数量
    candidate_k = RAG_CANDIDATE_K

    try:
        ensure_collection()
        client = get_qdrant()

        # 1. 向量化
        dense_vec = embed_query(query)
        sparse_vec_dict = embed_sparse([query])[0]
        sparse_vec = SparseVector(indices=sparse_vec_dict["indices"], values=sparse_vec_dict["values"])

        if not dense_vec:
            return []

        # 2. 过滤条件
        must = []
        if category:
            must.append(FieldCondition(key="category", match=MatchValue(value=category)))
        if article_ids:
            must.append(FieldCondition(key="article_id", match=MatchAny(any=article_ids)))
        flt = Filter(must=must) if must else None

        # 3. 双路召回
        dense_resp = client.query_points(
            collection_name=QDRANT_COLLECTION,
            query=dense_vec,
            using="dense",
            limit=candidate_k,
            query_filter=flt,
            with_payload=True,
        ).points

        sparse_resp = client.query_points(
            collection_name=QDRANT_COLLECTION,
            query=sparse_vec,
            using="sparse",
            limit=candidate_k,
            query_filter=flt,
            with_payload=True,
        ).points

        # 4. RRF 融合
        fused = rrf_fusion(dense_resp, sparse_resp, k=60)

        # 5. 父块展开 & 去重
        # ⚠️ 注意：这里不再限制 top_k，而是把所有的候选池准备好，交给 Rerank
        candidates = []
        seen_parents = set()

        for item in fused:
            payload = item["payload"]
            parent_id = payload.get("parent_id")

            if not parent_id or parent_id in seen_parents:
                continue

            seen_parents.add(parent_id)

            parent_text = payload.get("parent_text", "")
            text = parent_text if parent_text else payload.get("text", "")

            candidates.append({
                "text": text,
                "raw_text": payload.get("text", ""),
                "title": payload.get("title", ""),
                "article_id": payload.get("article_id"),
                "chunk_id": payload.get("chunk_id"),
                "heading_path": payload.get("heading_path", ""),
                "chunk_type": payload.get("chunk_type", ""),
                "score": item["score"], # 这是 RRF 的分数
            })

        # 6. Rerank 精排 (取前 20 个去精排，最后留 final_top_k 个)
        logger.info(f"RRF 融合完成，候选池共 {len(candidates)} 个片段，开始 Rerank...")
        final_results = rerank(query, candidates[:candidate_k], top_n=final_top_k)

        return final_results

    except Exception as e:
        logger.warning("retrieve failed, fallback to empty: %s", e)
        return []