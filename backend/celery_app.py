# duoduomsk
from celery import Celery
from backend.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND

# 创建 Celery 实例
celery_app = Celery(
    'blog_tasks',
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=[
        'backend.tasks.ai_tasks',
        'backend.tasks.cleanup_tasks',
        'backend.tasks.rag_tasks',
    ],
)

# 配置
celery_app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],  # 只允许 JSON，禁用 pickle（防黑客攻击）
    timezone='Asia/Shanghai',
    enable_utc=False,
    task_acks_late=True,      # 任务执行完才确认，防止 Worker 崩溃导致任务丢失
    worker_prefetch_multiplier=1, # 一次只拿一个任务，防止囤积
    result_expires=3600,      # 结果在 Redis 里存 1 小时
)

# 定时任务配置（Beat）
celery_app.conf.beat_schedule = {
    'flush-views-every-2-minutes': {
        'task': 'backend.tasks.cleanup_tasks.flush_views',
        'schedule': 120.0,  # 每 120 秒执行一次
    },
    # 新增：每 30 分钟对账一次 RAG 索引
    'sync-rag-index-every-30-minutes': {
        'task': 'backend.tasks.rag_tasks.sync_check',
        'schedule': 1800.0, # 30 * 60 = 1800 秒
    },
}