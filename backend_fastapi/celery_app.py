# backend_fastapi/celery_app.py
"""
Celery 实例。

- broker / backend 默认指向 Redis
- 自动发现 tasks/ 下的任务
- 通过命令启动 worker：
      celery -A celery_app worker --loglevel=info
  启动 beat（定时任务）：
      celery -A celery_app beat --loglevel=info
"""
from celery import Celery

from config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND


celery_app = Celery(
    "ai_blog",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=[
        "tasks.ai_tasks",
        "tasks.cleanup_tasks",
    ],
)


celery_app.conf.update(
    # 序列化
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # 时区
    timezone="Asia/Shanghai",
    enable_utc=True,

    # 任务执行参数
    task_track_started=True,
    task_time_limit=600,         # 硬超时 10 分钟
    task_soft_time_limit=540,    # 软超时 9 分钟

    # 结果过期
    result_expires=3600,

    # 防止 worker 预取过多任务导致内存占用
    worker_prefetch_multiplier=1,
    task_acks_late=True,

    # 定时任务（cleanup）
    beat_schedule={
        "cleanup-expired-contexts": {
            "task": "maintenance.cleanup_expired_contexts",
            "schedule": 3600.0,   # 每小时
        },
    },
)