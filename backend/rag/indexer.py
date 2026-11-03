# backend/rag/indexer.py
import hashlib
import logging
import uuid

from sqlalchemy import text
from qdrant_client.http.models import (
    Filter, FieldCondition, MatchValue, PointStruct,
)

from backend.database import engine
from backend.config import QDRANT_COLLECTION
from backend.rag.vector_store import get_qdrant, ensure_collection
from backend.rag.chunker import chunk_article
from backend.rag.embedder import embed_texts

logger = logging.getLogger(__name__)

_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "blog.rag")


def _content_hash(title: str, content: str) -> str:
    raw = f"{title or ''}\n{content or ''}".encode("utf-8")
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
                   content_hash=None, error_msg=None):
    """写台账：存在则 update，不存在则 insert。"""
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM rag_documents WHERE article_id = :id"),
            {"id": article_id},
        ).fetchone()

        if exists:
            sets, params = [], {"id": article_id}
            if status is not None:
                sets.append("status = :status"); params["status"] = status
            if chunk_count is not None:
                sets.append("chunk_count = :cc"); params["cc"] = chunk_count
            if content_hash is not None:
                sets.append("content_hash = :h"); params["h"] = content_hash
            if error_msg is not None:
                sets.append("error_msg = :err"); params["err"] = error_msg
            if status == "indexed":
                sets.append("indexed_at = CURRENT_TIMESTAMP")
            if sets:
                conn.execute(
                    text(f"UPDATE rag_documents SET {', '.join(sets)} WHERE article_id = :id"),
                    params,
                )
        else:
            cols, vals = ["article_id", "status"], [":id", ":status"]
            params = {"id": article_id, "status": status or "pending"}
            if chunk_count is not None:
                cols.append("chunk_count"); vals.append(":cc"); params["cc"] = chunk_count
            if content_hash is not None:
                cols.append("content_hash"); vals.append(":h"); params["h"] = content_hash
            if error_msg is not None:
                cols.append("error_msg"); vals.append(":err"); params["err"] = error_msg
            conn.execute(
                text(f"INSERT INTO rag_documents ({', '.join(cols)}) VALUES ({', '.join(vals)})"),
                params,
            )
        conn.commit()


def index_article(article_id: int):
    """索引/重索引一篇文章（幂等）。失败抛异常，让 Celery 重试。"""
    ensure_collection()
    client = get_qdrant()

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id, title, content, summary, category, tags, status
            FROM articles WHERE id = :id
        """), {"id": article_id}).mappings().fetchone()

    if not row:
        logger.warning("article not found: %s", article_id)
        delete_article_index(article_id)
        return

    article = dict(row)

    # 非 published 的文章不进知识库
    if article.get("status") != "published":
        logger.info("article %s status=%s -> delete index", article_id, article.get("status"))
        delete_article_index(article_id)
        return

    new_hash = _content_hash(article["title"], article["content"])

    # 内容未变 → 跳过（省钱）
    try:
        with engine.connect() as conn:
            prev = conn.execute(
                text("SELECT content_hash, status FROM rag_documents WHERE article_id = :id"),
                {"id": article_id},
            ).mappings().fetchone()
        if prev and prev["status"] == "indexed" and prev["content_hash"] == new_hash:
            logger.info("article %s unchanged, skip", article_id)
            return
    except Exception as e:
        logger.warning("ledger read failed aid=%s err=%s", article_id, e)

    _upsert_ledger(article_id, status="indexing")

    try:
        chunks = chunk_article(article["title"], article["content"] or "")
        if not chunks:
            _delete_points(client, article_id)
            _upsert_ledger(article_id, status="indexed", chunk_count=0, content_hash=new_hash)
            return

        vectors = embed_texts([c["text"] for c in chunks])

        # 先删后插：防止改短的文章留下幽灵块
        _delete_points(client, article_id)

        points = []
        for c, vec in zip(chunks, vectors):
            if not vec:
                continue
            points.append(PointStruct(
                id=_point_id(article_id, c["chunk_index"]),
                vector=vec,
                payload={
                    "article_id": article_id,
                    "chunk_index": c["chunk_index"],
                    "title": article["title"],
                    "heading_path": c["heading_path"],
                    "text": c["raw_text"],                 # 原文块，检索回来直接拼 Prompt
                    "category": article.get("category") or "",
                    "tags": article.get("tags") or "",
                    "summary": article.get("summary") or "",
                },
            ))

        if points:
            client.upsert(collection_name=QDRANT_COLLECTION, points=points)

        _upsert_ledger(article_id, status="indexed",
                       chunk_count=len(points), content_hash=new_hash)
        logger.info("indexed article %s chunks=%s", article_id, len(points))

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