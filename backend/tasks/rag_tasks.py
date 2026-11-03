# backend/tasks/rag_tasks.py
from backend.celery_app import celery_app
from backend.rag import indexer


@celery_app.task(bind=True, max_retries=3,
                 name='backend.tasks.rag_tasks.index_article')
def index_article_task(self, article_id):
    try:
        indexer.index_article(article_id)
    except Exception as e:
        # embedding 限流 / 网络抖动 → 15 秒后重试
        raise self.retry(exc=e, countdown=15)


@celery_app.task(name='backend.tasks.rag_tasks.delete_article')
def delete_article_task(article_id):
    indexer.delete_article_index(article_id)


@celery_app.task(name='backend.tasks.rag_tasks.rebuild_all')
def rebuild_index_task():
    # 全量重建不重试，失败就失败，人工再触发
    return indexer.rebuild_all()


from sqlalchemy import text
from backend.database import engine


@celery_app.task(name='backend.tasks.rag_tasks.sync_check')
def sync_check_task():
    """
    定时对账：找出 MySQL 中已发布，但在台账里缺失、失败，或者更新时间晚于台账索引时间的文章，补投索引。
    """
    # 找出需要更新的文章 ID
    sql = """
          SELECT a.id
          FROM articles a
                   LEFT JOIN rag_documents r ON a.id = r.article_id
          WHERE a.status = 'published'
            AND (
              r.article_id IS NULL
                  OR r.status != 'indexed' 
              OR a.updated_at > r.indexed_at
              ) \
          """
    with engine.connect() as conn:
        # 注意 MySQL 中如果 indexed_at 为 NULL，a.updated_at > NULL 结果为 NULL，会被过滤掉
        # 所以上面用的是 OR 逻辑，只要 r.article_id IS NULL 就会查出来
        ids = conn.execute(text(sql)).scalars().all()

    if not ids:
        print("[RAG 对账] 台账与数据库一致，无需更新。")
        return {"status": "clean", "count": 0}

    print(f"[RAG 对账] 发现 {len(ids)} 篇文章需要更新索引，正在投递任务...")
    for aid in ids:
        index_article_task.delay(aid)

    return {"status": "fixed", "count": len(ids)}