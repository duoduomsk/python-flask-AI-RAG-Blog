# backend_fastapi/limiter.py
"""
限流模块。

使用 slowapi，Key 默认基于客户端 IP。
- 通过环境变量 RATELIMIT_ENABLED=false 可以全局关闭（便于本地调试）
- 装饰器用法：@limiter.limit("10/minute")
- ⚠️ 被装饰的函数签名必须包含 request: Request 参数

已在 auth.py 中应用到：
  - POST /api/login    10 次/分钟
  - POST /api/register 5 次/分钟
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

from config import RATELIMIT_ENABLED


limiter = Limiter(
    key_func=get_remote_address,
    enabled=RATELIMIT_ENABLED,
    default_limits=[],  # 不设全局默认，按需挂
)


# ------------------------------------------------------------------
# 限流触发时的响应体（与项目通用格式保持一致）
# ------------------------------------------------------------------
from slowapi.errors import RateLimitExceeded
from fastapi import Request
from fastapi.responses import JSONResponse


async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """
    slowapi 默认响应是纯文本，这里替换为项目的 {"status":1,"msg":"..."} 格式，
    并返回 429，前端 axios 拦截器会走错误分支。
    """
    return JSONResponse(
        status_code=429,
        content={
            "status": 1,
            "msg": f"请求过于频繁，请稍后再试（{exc.detail}）",
        },
    )