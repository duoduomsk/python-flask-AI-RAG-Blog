# backend_fastapi/tasks/cleanup_tasks.py
"""
定时清理任务（由 celery beat 调度）。
"""
from celery_app import celery_app
from cache import cache_delete_pattern, make_key


@celery_app.task(name="maintenance.cleanup_expired_contexts")
def cleanup_expired_contexts() -> dict:
    """
    Redis 场景下，上下文靠 TTL 自动过期；本任务用于兜底清理
    （避免误用了不带 TTL 的写入导致 key 堆积）。
    """
    pattern = make_key("ai", "context", "*")
    deleted = cache_delete_pattern(pattern)
    print(f"[cleanup] 清理过期上下文，删除 {deleted} 个 key")
    return {"deleted": deleted}


@celery_app.task(name="maintenance.cleanup_old_ai_configs")
def cleanup_old_ai_configs(keep_recent: int = 20) -> dict:
    """
    保留最近的 keep_recent 条 ai_configs，删除更旧的（防止 ai_configs 无限膨胀）。
    """
    from sqlalchemy import text
    from database import engine

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id FROM ai_configs ORDER BY created_at DESC LIMIT :n, 1000"
        ), {"n": keep_recent}).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            conn.execute(text(
                f"DELETE FROM ai_configs WHERE id IN ({','.join([':id%d' % i for i in range(len(ids))])})"
            ), {f"id{i}": v for i, v in enumerate(ids)})
            conn.commit()
    return {"deleted": len(ids)}