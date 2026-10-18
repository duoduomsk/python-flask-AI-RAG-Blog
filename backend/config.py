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
    or "mysql+pymysql://root:1234@127.0.0.1:3306/ai_blog?charset=utf8mb4"
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
