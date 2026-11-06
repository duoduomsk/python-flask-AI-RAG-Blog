# backend/rag/eval.py
import json
import logging
import os
from backend.rag.retriever import retrieve

logging.basicConfig(level=logging.INFO, format='%(message)s')


def run_eval():
    # 1. 读取考题
    file_path = os.path.join(os.path.dirname(__file__), "eval_cases.json")
    with open(file_path, "r", encoding="utf-8") as f:
        cases = json.load(f)

    total = len(cases)
    recall_1 = 0
    recall_3 = 0
    recall_5 = 0
    mrr_sum = 0.0

    print(f"\n📝 开始评测，共 {total} 道题目...\n" + "=" * 50)

    for i, case in enumerate(cases):
        query = case["query"]
        expected_ids = set(case["expected_article_ids"])

        # 走真实的 RAG 检索链路 (混合检索 + Rerank)
        results = retrieve(query, top_k=5)

        # 提取召回的 article_id，保持排名顺序
        retrieved_ids = [r["article_id"] for r in results]

        # 判断命中情况
        hit_rank = None
        for rank, aid in enumerate(retrieved_ids, start=1):
            if aid in expected_ids:
                hit_rank = rank
                break

        # 计算指标
        if hit_rank == 1:
            recall_1 += 1
            recall_3 += 1
            recall_5 += 1
            mrr_sum += 1.0 / 1
            print(f"✅ [第{i + 1}题] 完美命中 Top1 | Query: {query}")
        elif hit_rank in (2, 3):
            recall_3 += 1
            recall_5 += 1
            mrr_sum += 1.0 / hit_rank
            print(f"🟡 [第{i + 1}题] 命中 Top{hit_rank}   | Query: {query}")
        elif hit_rank in (4, 5):
            recall_5 += 1
            mrr_sum += 1.0 / hit_rank
            print(f"🟠 [第{i + 1}题] 命中 Top{hit_rank}   | Query: {query}")
        else:
            print(f"❌ [第{i + 1}题] 未命中 (Miss)   | Query: {query}")
            print(f"   期望: {expected_ids} | 实际召回: {retrieved_ids}")

    # 输出成绩单
    print("\n" + "=" * 50)
    print("📊 RAG 检索评测报告")
    print(f"Recall@1 : {recall_1 / total * 100:.1f}% ({recall_1}/{total})")
    print(f"Recall@3 : {recall_3 / total * 100:.1f}% ({recall_3}/{total})")
    print(f"Recall@5 : {recall_5 / total * 100:.1f}% ({recall_5}/{total})")
    print(f"MRR      : {mrr_sum / total:.3f}")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    run_eval()