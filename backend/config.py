import os
from urllib.parse import quote_plus

def normalize_mysql_url(url):
    if url.startswith("mysql://"):
        return url.replace("mysql://", "mysql+pymysql://", 1)
    return url


def build_mysql_url_from_env():
    host = os.getenv("MYSQLHOST")
    user = os.getenv("MYSQLUSER")
    password = os.getenv("MYSQLPASSWORD")
    database = os.getenv("MYSQLDATABASE")
    port = os.getenv("MYSQLPORT", "3306")

    if not all([host, user, password, database]):
        return None

    return (
        "mysql+pymysql://"
        f"{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{database}"
        "?charset=utf8mb4"
    )


DATABASE_URL = normalize_mysql_url(
    os.getenv("DATABASE_URL")
    or os.getenv("MYSQL_URL")
    or build_mysql_url_from_env()
    or "mysql+pymysql://root:123456@127.0.0.1:3307/ai_blog?charset=utf8mb4"
)

#duoduomsk
# redis
def _resolve_redis_db(url: str, db_index: int) -> str:
    """把 redis://.../0 末尾的 db 号替换为指定值；没有 db 段时追加。"""
    if not url:
        return url
    base, sep, tail = url.rpartition("/")
    if not sep:
        return f"{url}/{db_index}"
    if tail.isdigit():
        return f"{base}/{db_index}"
    return f"{url}/{db_index}"

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
# Celery 用不同的 db，避免 FLUSHDB 清缓存时误杀任务队列
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL") or _resolve_redis_db(REDIS_URL, 1)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND") or _resolve_redis_db(REDIS_URL, 2)

# ---------- 缓存 TTL（秒） ----------
CACHE_TTL_LIST = int(os.getenv("CACHE_TTL_LIST", "60"))
CACHE_TTL_DETAIL = int(os.getenv("CACHE_TTL_DETAIL", "300"))
CACHE_TTL_CONFIG = int(os.getenv("CACHE_TTL_CONFIG", "30"))
CACHE_TTL_STATIC = int(os.getenv("CACHE_TTL_STATIC", "3600"))
REDIS_CONTEXT_TTL = int(os.getenv("REDIS_CONTEXT_TTL", "86400"))

# ---------- RAG ----------
QDRANT_URL        = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "blog_articles")

EMBEDDING_BASE_URL = os.getenv(
    "EMBEDDING_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
EMBEDDING_API_KEY  = os.getenv("DASHSCOPE_API_KEY", "")
# 请换成你的，这个已经失效
EMBEDDING_MODEL    = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
EMBEDDING_DIM      = int(os.getenv("EMBEDDING_DIM", "1024"))

# ---------- RAG 切片 v2 ----------
# 子块目标大小（token 估算），召回用
RAG_CHUNK_TOKENS = int(os.getenv("RAG_CHUNK_TOKENS", "320"))
# 父块最大大小，给模型看上下文用
RAG_PARENT_TOKENS = int(os.getenv("RAG_PARENT_TOKENS", "900"))
# 子块重叠 token
RAG_CHUNK_OVERLAP_TOKENS = int(os.getenv("RAG_CHUNK_OVERLAP_TOKENS", "40"))
# 切片器版本号：改切片规则就 +1，用于自动触发重建
RAG_CHUNKER_VERSION = os.getenv("RAG_CHUNKER_VERSION", "v2")
# 上下文头（Contextual Retrieval，可选，默认关）
RAG_CONTEXTUAL_HEADER = os.getenv("RAG_CONTEXTUAL_HEADER", "false").lower() in ("1", "true", "yes", "on")

# ---------- Rerank (精排) ----------
RAG_RERANK_ENABLED = os.getenv("RAG_RERANK_ENABLED", "true").lower() in ("1", "true", "yes", "on")
# 候选池大小，即 RRF 融合后交给 Rerank 的片段数量
RAG_CANDIDATE_K = int(os.getenv("RAG_CANDIDATE_K", "50"))
# 降级阈值：如果重排分低于这个值，说明完全不相关，直接丢弃
RAG_RERANK_MIN_SCORE = float(os.getenv("RAG_RERANK_MIN_SCORE", "0.1"))

RAG_CHUNK_SIZE      = int(os.getenv("RAG_CHUNK_SIZE", "500"))
RAG_CHUNK_OVERLAP   = int(os.getenv("RAG_CHUNK_OVERLAP", "80"))
RAG_TOP_K           = int(os.getenv("RAG_TOP_K", "5"))
RAG_SCORE_THRESHOLD = float(os.getenv("RAG_SCORE_THRESHOLD", "0.35"))
RAG_ENABLED         = os.getenv("RAG_ENABLED", "true").lower() in ("1", "true", "yes", "on")


# ---------- 限流总开关 ----------
RATELIMIT_ENABLED = os.getenv("RATELIMIT_ENABLED", "true").lower() in (
    "1", "true", "yes", "on"
)


# ---------- JWT 密钥（生产强制从环境变量读取） ----------
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if os.getenv("FLASK_ENV") == "production" or os.getenv("APP_ENV") == "production":
        raise RuntimeError(
            "SECRET_KEY 必须通过环境变量设置（生产环境）。"
            "请勿在代码中硬编码密钥。"
        )
    # 仅开发默认值：进程重启后 token 全部失效，符合预期
    SECRET_KEY = "dev-only-secret-key-change-me"
