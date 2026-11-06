# backend/rag/sparse_embedder.py
import logging
from fastembed import SparseTextEmbedding

logger = logging.getLogger(__name__)

_sparse_model = None


def get_sparse_model():
    global _sparse_model
    if _sparse_model is None:
        # 使用轻量级 BM25 模型
        _sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
    return _sparse_model


def embed_sparse(texts: list[str]) -> list[dict]:
    """返回稀疏向量格式：{'indices': [...], 'values': [...]}"""
    if not texts:
        return []

    model = get_sparse_model()
    # fastembed 的 embed 返回生成器，转为列表
    results = list(model.embed(texts))

    output = []
    for res in results:
        output.append({
            "indices": res.indices.tolist(),
            "values": res.values.tolist()
        })
    return output