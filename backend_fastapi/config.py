# backend_fastapi/config.py
import os
from urllib.parse import quote_plus


def normalize_mysql_url(url: str) -> str:
    """把 mysql:// 前缀转为 mysql+pymysql://（与原 config.py 一致）"""
    if url.startswith("mysql://"):
        return url.replace("mysql://", "mysql+pymysql://", 1)
    return url


def build_mysql_url_from_env() -> str | None:
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


# ------------------------------------------------------------------
# MySQL —— 与原 backend/config.py 完全一致的解析顺序
# ------------------------------------------------------------------
DATABASE_URL = normalize_mysql_url(
    os.getenv("DATABASE_URL")
    or os.getenv("MYSQL_URL")
    or build_mysql_url_from_env()
    or "mysql+pymysql://root:1234@127.0.0.1:3306/ai_blog?charset=utf8mb4"
)

# ------------------------------------------------------------------
# Redis（temp3.txt 阶段1 预埋）
# ------------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL.replace("/0", "/1"))
CELERY_RESULT_BACKEND = os.getenv(
    "CELERY_RESULT_BACKEND", REDIS_URL.replace("/0", "/2")
)

# ------------------------------------------------------------------
# 安全 —— 必须与原 Flask 后端的 SECRET_KEY 一致，
# 否则旧的 JWT Token 在新后端无法解析
# ------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "your-very-secret-key-123!@#")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24

# ------------------------------------------------------------------
# 限流（temp3.txt 阶段1 预埋）
# ------------------------------------------------------------------
RATELIMIT_ENABLED = os.getenv("RATELIMIT_ENABLED", "true").lower() == "true"

# ------------------------------------------------------------------
# 缓存 TTL（秒）
# ------------------------------------------------------------------
CACHE_TTL_LIST = int(os.getenv("CACHE_TTL_LIST", "60"))
CACHE_TTL_DETAIL = int(os.getenv("CACHE_TTL_DETAIL", "300"))
REDIS_CONTEXT_TTL = int(os.getenv("REDIS_CONTEXT_TTL", "86400"))


# ------------------------------------------------------------------
# CORS —— 与原 app.py 的 ALLOWED_ORIGINS 完全一致
# ------------------------------------------------------------------
def _parse_env_list(name: str, defaults: list[str]) -> list[str]:
    value = os.environ.get(name, "")
    items = [item.strip().rstrip("/") for item in value.split(",") if item.strip()]
    return items or defaults


ALLOWED_ORIGINS: list[str] = _parse_env_list("CORS_ORIGINS", [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "https://ai-vue3-python-flask-blog.vercel.app",
    "https://ai-vue3-python-flask-blog-copy.vercel.app",
    "https://lindablog.xyz",
    "https://www.lindablog.xyz",
])

# 正则匹配的 origins（如 Vercel 动态子域）
# Starlette 的 CORSMiddleware 支持 allow_origin_regex（单个），
# 如果有多条，合并为一条 alternation 正则。
ALLOWED_ORIGIN_REGEXES: list[str] = _parse_env_list("CORS_ORIGIN_REGEXES", [
    r"^https://ai-vue3-python-flask-blog-[a-z0-9-]+-linsk27s-projects\.vercel\.app$",
    r"^https://[a-z0-9-]+\.vercel\.app$",
    r"^https://[a-z0-9-]+-[a-z0-9-]+-linsk27s-projects\.vercel\.app$",
])

# 合并为单条正则（Starlette 只接受一个 allow_origin_regex）
ALLOWED_ORIGIN_REGEX: str | None = (
    "|".join(f"(?:{r})" for r in ALLOWED_ORIGIN_REGEXES)
    if ALLOWED_ORIGIN_REGEXES
    else None
)

# ------------------------------------------------------------------
# 上传目录（默认复用原 backend/uploads，保证历史图片不丢）
# ------------------------------------------------------------------
UPLOAD_FOLDER = os.getenv(
    "UPLOAD_FOLDER",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend", "uploads"),
)

# backend_fastapi/config.py（末尾追加）
AI_ASYNC_ENABLED = os.getenv("AI_ASYNC_ENABLED", "false").lower() == "true"