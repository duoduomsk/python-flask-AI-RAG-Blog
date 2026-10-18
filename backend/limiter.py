# duoduomsk
"""
Flask-Limiter 实例：与 app.py 解耦，独立模块创建避免循环依赖。

- storage_uri 指向 REDIS_URL（生产必须带密码）。
- 关掉限流开关时切到 memory://，避免无效 Redis 连接。
- swallow_errors=True：Redis 故障时放行请求（博客场景选 fail-open）。
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from backend.config import REDIS_URL, RATELIMIT_ENABLED

_storage = REDIS_URL if RATELIMIT_ENABLED else "memory://"

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_storage,
    enabled=RATELIMIT_ENABLED,
    default_limits=[],          # 默认不限流，仅在具体路由打标签
    swallow_errors=True,
    strategy="fixed-window",
)