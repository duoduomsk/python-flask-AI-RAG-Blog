# backend_fastapi/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from contextlib import contextmanager

# 与原 backend/database.py 保持相同的引擎参数，便于平滑切换
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,   # 避免 MySQL "server has gone away"
    future=True,
)

# SessionLocal 供后续 routers / services 使用（Depends(get_db)）
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

@contextmanager
def get_db_session():
    """
    Celery 任务 / 后台脚本专用 Session 工厂。

    用法：
        from database import get_db_session
        with get_db_session() as db:
            db.execute(text("UPDATE ..."))
            db.commit()

    说明：
        - with 块结束时自动 close，不会泄漏连接
        - 不自动 commit，需要你自己调 db.commit()（和 FastAPI 的 Depends(get_db) 行为一致）
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()