# backend/rag/fusion.py
def rrf_fusion(dense_results: list, sparse_results: list, k: int = 60) -> list:
    """
    Reciprocal Rank Fusion (RRF)
    dense_results / sparse_results: Qdrant 返回的 ScoredPoint 列表
    k: 平滑参数，通常取 60
    """
    fused_scores = {}
    point_map = {} # 存 ID -> 原始 payload 等数据，方便后续取

    # 处理 Dense 排名
    for rank, hit in enumerate(dense_results):
        point_id = hit.id
        point_map[point_id] = hit
        if point_id not in fused_scores:
            fused_scores[point_id] = 0.0
        fused_scores[point_id] += 1.0 / (k + rank + 1)

    # 处理 Sparse 排名
    for rank, hit in enumerate(sparse_results):
        point_id = hit.id
        point_map[point_id] = hit # 如果两路都有，随便存一个即可，payload 一样
        if point_id not in fused_scores:
            fused_scores[point_id] = 0.0
        fused_scores[point_id] += 1.0 / (k + rank + 1)

    # 按融合得分降序排序
    sorted_ids = sorted(fused_scores.keys(), key=lambda x: fused_scores[x], reverse=True)

    results = []
    for pid in sorted_ids:
        hit = point_map[pid]
        results.append({
            "id": pid,
            "payload": hit.payload,
            "score": fused_scores[pid],
        })
    return results