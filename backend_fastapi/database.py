# backend_fastapi/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL

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