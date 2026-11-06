# backend/rag/reranker.py
import logging
import os
import httpx

# 直接复用在 config.py 中已经写好的 EMBEDDING_API_KEY
from backend.config import EMBEDDING_API_KEY, RAG_RERANK_MIN_SCORE

logger = logging.getLogger(__name__)

# DashScope 的 Rerank 接口
DASHSCOPE_RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
RERANK_MODEL = "gte-rerank-v2"


def rerank(query: str, candidates: list[dict], top_n: int = 5) -> list[dict]:
    """
    对候选集进行重排。
    :param query: 用户的查询词
    :param candidates: 经过 RRF 融合后的候选列表
    :param top_n: 最终保留几条给大模型
    """
    if not candidates:
        return []

    # 这里直接用 config.py 里的 EMBEDDING_API_KEY
    api_key = EMBEDDING_API_KEY
    if not api_key:
        logger.warning("未配置 DASHSCOPE_API_KEY (EMBEDDING_API_KEY)，跳过 Rerank 直接返回 RRF 结果")
        return candidates[:top_n]

    # 准备请求体
    documents = [c["text"] for c in candidates]
    payload = {
        "model": RERANK_MODEL,
        "input": {
            "query": query,
            "documents": documents
        },
        "parameters": {
            "return_documents": False,
            "top_n": len(documents)
        }
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    try:
        # 超时设置 2 秒，防止拖垮整个接口
        with httpx.Client(timeout=2.0) as client:
            resp = client.post(DASHSCOPE_RERANK_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            results = data.get("output", {}).get("results", [])

            # 组装结果
            reranked = []
            for item in results:
                idx = item["index"]
                score = item["relevance_score"]

                # 过滤掉分数过低的（说明真不相关）
                if score < RAG_RERANK_MIN_SCORE:
                    continue

                c = candidates[idx]
                c["rerank_score"] = score
                reranked.append(c)

                if len(reranked) >= top_n:
                    break

            logger.info(f"Rerank 成功，筛选出 {len(reranked)} 个高分片段")
            return reranked

    except Exception as e:
        logger.warning(f"Rerank API 请求失败，降级使用 RRF 原始结果: {e}")
        # Fail-open：如果 Rerank 挂了，直接拿原来的前 top_n 个，不影响业务
        return candidates[:top_n]