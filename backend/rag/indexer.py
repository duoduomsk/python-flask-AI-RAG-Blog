# backend/rag/indexer.py
import hashlib
import logging
import uuid

from sqlalchemy import text
from qdrant_client.http.models import (
    Filter, FieldCondition, MatchValue, PointStruct,
)

from backend.database import engine
from backend.rag.vector_store import get_qdrant, ensure_collection
from backend.rag.chunker import chunk_article
from backend.rag.embedder import embed_texts

from backend.config import (
    QDRANT_COLLECTION,
    EMBEDDING_MODEL,
    RAG_CHUNKER_VERSION,
)

import json
from backend.rag.structure_parser import estimate_tokens  # 复用
from backend.rag.sparse_embedder import embed_sparse
from qdrant_client.http.models import SparseVector, PointStruct

logger = logging.getLogger(__name__)

_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "blog.rag")


def _content_hash(title: str, content: str) -> str:
    """
    内容 hash 包含 chunker_version 和 embedding_model：
    - 切片规则改了 → hash 变 → 自动触发重建
    - 换 embedding 模型了 → hash 变 → 自动触发重建
    """
    raw = (
        f"{title or ''}\n{content or ''}\n"
        f"{RAG_CHUNKER_VERSION}\n{EMBEDDING_MODEL}"
    ).encode("utf-8")
    return hashlib.md5(raw).hexdigest()


def _point_id(article_id: int, chunk_index: int) -> str:
    return str(uuid.uuid5(_NS, f"{article_id}:{chunk_index}"))


def _delete_points(client, article_id: int):
    client.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=Filter(
            must=[FieldCondition(key="article_id", match=MatchValue(value=article_id))]
        ),
    )


def _upsert_ledger(article_id, status=None, chunk_count=None,
                   content_hash=None, error_msg=None,
                   chunker_version=None, embedding_model=None,
                   parent_count=None):
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM rag_documents WHERE article_id = :id"),
            {"id": article_id},
        ).fetchone()

        if exists:
            sets, params = [], {"id": article_id}
            if status is not None:         sets.append("status = :status"); params["status"] = status
            if chunk_count is not None:    sets.append("chunk_count = :cc"); params["cc"] = chunk_count
            if content_hash is not None:   sets.append("content_hash = :h"); params["h"] = content_hash
            if error_msg is not None:      sets.append("error_msg = :err"); params["err"] = error_msg
            if chunker_version is not None:sets.append("chunker_version = :cv"); params["cv"] = chunker_version
            if embedding_model is not None:sets.append("embedding_model = :em"); params["em"] = embedding_model
            if parent_count is not None:   sets.append("parent_count = :pc"); params["pc"] = parent_count
            if status == "indexed":        sets.append("indexed_at = CURRENT_TIMESTAMP")
            if sets:
                conn.execute(
                    text(f"UPDATE rag_documents SET {', '.join(sets)} WHERE article_id = :id"),
                    params,
                )
        else:
            cols, vals = ["article_id", "status"], [":id", ":status"]
            params = {"id": article_id, "status": status or "pending"}
            for value, col, key in [
                (chunk_count, "chunk_count", "cc"),
                (content_hash, "content_hash", "h"),
                (error_msg, "error_msg", "err"),
                (chunker_version, "chunker_version", "cv"),
                (embedding_model, "embedding_model", "em"),
                (parent_count, "parent_count", "pc"),
            ]:
                if value is not None:
                    cols.append(col)
                    vals.append(f":{key}")
                    params[key] = value
            conn.execute(
                text(f"INSERT INTO rag_documents ({', '.join(cols)}) VALUES ({', '.join(vals)})"),
                params,
            )
        conn.commit()


def _parse_tags(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []

def _infer_doc_intent(title: str, tags: list, content: str) -> str:
    """规则推断文档意图，阶段 1.4 可换成小模型分类。"""
    text = f"{title} {' '.join(tags)} {content[:500]}".lower()
    if any(k in text for k in ["报错", "踩坑", "解决", "错误", "error", "bug", "fix", "troubleshoot"]):
        return "troubleshooting"
    if any(k in text for k in ["教程", "入门", "实战", "指南", "how to", "tutorial"]):
        return "tutorial"
    if any(k in text for k in ["原理", "是什么", "概念", "深入理解", "concept"]):
        return "concept"
    if any(k in text for k in ["观点", "思考", "随笔", "感想", "opinion"]):
        return "opinion"
    return "reference"


def index_article(article_id: int):
    ensure_collection()
    client = get_qdrant()

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id, title, content, summary, category, tags, status, updated_at
            FROM articles WHERE id = :id
        """), {"id": article_id}).mappings().fetchone()

    if not row:
        logger.warning("article not found: %s", article_id)
        delete_article_index(article_id)
        return

    article = dict(row)

    if article.get("status") != "published":
        logger.info("article %s status=%s -> delete index", article_id, article.get("status"))
        delete_article_index(article_id)
        return

    new_hash = _content_hash(article["title"], article["content"])

    # 内容未变 + 版本未变 → 跳过
    try:
        with engine.connect() as conn:
            prev = conn.execute(
                text("""SELECT content_hash, status, chunker_version, embedding_model
                        FROM rag_documents WHERE article_id = :id"""),
                {"id": article_id},
            ).mappings().fetchone()
        if (prev and prev["status"] == "indexed"
                and prev["content_hash"] == new_hash
                and prev["chunker_version"] == RAG_CHUNKER_VERSION
                and prev["embedding_model"] == EMBEDDING_MODEL):
            logger.info("article %s unchanged, skip", article_id)
            return
    except Exception as e:
        logger.warning("ledger read failed aid=%s err=%s", article_id, e)

    _upsert_ledger(article_id, status="indexing")

    try:
        tags_list = _parse_tags(article.get("tags"))
        doc_intent = _infer_doc_intent(article["title"], tags_list, article["content"] or "")

        chunks = chunk_article(
            article["title"], article["content"] or "",
            doc_intent=doc_intent,
        )
        if not chunks:
            _delete_points(client, article_id)
            _upsert_ledger(
                article_id, status="indexed", chunk_count=0,
                content_hash=new_hash, parent_count=0,
                chunker_version=RAG_CHUNKER_VERSION, embedding_model=EMBEDDING_MODEL,
            )
            return

        # 1. 生成 Dense 向量
        vectors = embed_texts([c["text"] for c in chunks])

        # 2. 生成 Sparse 向量
        sparse_vectors = embed_sparse([c["text"] for c in chunks])

        # 先删后插
        _delete_points(client, article_id)

        article_updated_at = article.get("updated_at")
        if article_updated_at:
            article_updated_at = article_updated_at.isoformat()

        points = []
        parent_ids = set()
        for c, vec, s_vec in zip(chunks, vectors, sparse_vectors):
            if not vec or not s_vec:
                continue
            parent_ids.add(c["parent_id"])
            points.append(PointStruct(
                id=_point_id(article_id, c["chunk_index"]),
                vector={
                    "dense": vec,
                    "sparse": SparseVector(
                        indices=s_vec["indices"],
                        values=s_vec["values"]
                    )
                },
                payload={
                    # 定位字段
                    "article_id": article_id,
                    "chunk_id": _point_id(article_id, c["chunk_index"]),
                    "chunk_index": c["chunk_index"],
                    "parent_id": c["parent_id"],
                    # 父子块
                    "text": c["raw_text"],
                    "parent_text": c["parent_text"][:4000],  # 控大小
                    # 类型与结构
                    "chunk_type": c["chunk_type"],
                    "heading_path": c["heading_path"],
                    "keywords": c.get("keywords") or [],
                    "token_count": c["token_count"],
                    "chunk_hash": c["chunk_hash"],
                    # 文档元信息
                    "title": article["title"],
                    "category": article.get("category") or "",
                    "tags": tags_list,                     # 数组，阶段 1.2 MatchAny 用
                    "summary": article.get("summary") or "",
                    "doc_intent": c.get("doc_intent") or "",
                    "difficulty": c.get("difficulty") or "",
                    "article_updated_at": article_updated_at,
                    # 版本
                    "chunker_version": c["chunker_version"],
                    "embedding_model": EMBEDDING_MODEL,
                },
            ))

        # 批量 upsert：每 100 条一批
        BATCH = 100
        for i in range(0, len(points), BATCH):
            client.upsert(collection_name=QDRANT_COLLECTION, points=points[i:i + BATCH])

        _upsert_ledger(
            article_id, status="indexed",
            chunk_count=len(points),
            content_hash=new_hash,
            parent_count=len(parent_ids),
            chunker_version=RAG_CHUNKER_VERSION,
            embedding_model=EMBEDDING_MODEL,
        )
        logger.info("indexed article %s chunks=%s parents=%s",
                    article_id, len(points), len(parent_ids))

    except Exception as e:
        logger.exception("index_article failed aid=%s err=%s", article_id, e)
        try:
            _upsert_ledger(article_id, status="failed", error_msg=str(e)[:500])
        except Exception:
            pass
        raise


def delete_article_index(article_id: int):
    ensure_collection()
    client = get_qdrant()
    try:
        _delete_points(client, article_id)
    except Exception as e:
        logger.warning("qdrant delete failed aid=%s err=%s", article_id, e)
    try:
        _upsert_ledger(article_id, status="deleted", chunk_count=0, error_msg="")
    except Exception as e:
        logger.warning("ledger delete failed aid=%s err=%s", article_id, e)


def rebuild_all() -> dict:
    ensure_collection()
    with engine.connect() as conn:
        ids = conn.execute(text(
            "SELECT id FROM articles WHERE status = 'published'"
        )).scalars().all()

    total = len(ids)
    indexed = failed = 0
    for aid in ids:
        try:
            index_article(aid)
            indexed += 1
        except Exception as e:
            logger.error("rebuild aid=%s failed: %s", aid, e)
            failed += 1
    return {"total": total, "indexed": indexed, "failed": failed}