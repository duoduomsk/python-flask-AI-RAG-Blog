# backend/tasks/cleanup_tasks.py
from backend.celery_app import celery_app
from backend.cache import scan_view_counters, clear_view_counters
from backend.database import engine
from sqlalchemy import text


@celery_app.task
def flush_views():
    """把 Redis 里的浏览量累加回 MySQL"""
    print("【Celery 定时任务】开始回写浏览量...")
    views_data = scan_view_counters()  # {article_id: delta}
    if not views_data:
        print("没有需要回写的浏览量")
        return

    try:
        with engine.connect() as conn:
            for article_id, delta in views_data.items():
                conn.execute(
                    text("UPDATE articles SET views = views + :delta WHERE id = :id"),
                    {"delta": delta, "id": article_id}
                )
            conn.commit()

        # 写回成功后，清除 Redis 里的计数器
        clear_view_counters(views_data.keys())
        print(f"【Celery 定时任务】成功回写 {len(views_data)} 篇文章的浏览量")
    except Exception as e:
        print(f"【Celery 定时任务】回写失败: {e}")