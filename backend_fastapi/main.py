# backend_fastapi/main.py
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from limiter import limiter, rate_limit_handler
from slowapi.errors import RateLimitExceeded

from config import (
    ALLOWED_ORIGINS,
    ALLOWED_ORIGIN_REGEX,
    SECRET_KEY,
)

# ------------------------------------------------------------------
# 创建 FastAPI 应用
# ------------------------------------------------------------------
app = FastAPI(
    title="AI Blog API (FastAPI)",
    version="2.0.0",
    description="从 Flask 迁移到 FastAPI 的 AI 博客后端，接口与 Flask 版 100% 兼容。",
)

# ------------------------------------------------------------------
# CORS —— 与原 app.py 的 flask_cors.CORS(...) 参数一一对应
# ------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "APP-ID"],
    expose_headers=["*"],
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)


# ------------------------------------------------------------------
# 健康检查
# ------------------------------------------------------------------
@app.get("/", tags=["系统"])
def root():
    return {"message": "FastAPI started successfully."}


@app.get("/health", tags=["系统"])
def health():
    from cache import cache_stats
    return {
        "status": "ok",
        "service": "ai-blog-fastapi",
        "version": app.version,
        "cache": cache_stats(),
    }


# ------------------------------------------------------------------
# 注册路由（骨架阶段先注释掉，等对应 router 文件写好后逐行放开）
# ------------------------------------------------------------------
def register_routers():
    """
    逐模块注册路由。任一模块缺失或导入失败，都不影响应用启动，
    便于我们按 Step 2～Step 5 渐进式迁移。
    """
    try:
        from routers import auth as auth_router
        app.include_router(auth_router.router, prefix="/api", tags=["认证"])
        print("[FastAPI] ✅ 已注册 auth 路由")
    except ImportError as e:
        print(f"[FastAPI] ⏭️  auth 路由未就绪（{e}），跳过")

    try:
        from routers import ai as ai_router
        app.include_router(ai_router.router, prefix="/api", tags=["AI"])
        print("[FastAPI] ✅ 已注册 ai 路由")
    except ImportError as e:
        print(f"[FastAPI] ⏭️  ai 路由未就绪（{e}），跳过")

    try:
        from routers import article as article_router
        app.include_router(article_router.router, prefix="/api", tags=["文章"])
        print("[FastAPI] ✅ 已注册 article 路由")
    except ImportError as e:
        print(f"[FastAPI] ⏭️  article 路由未就绪（{e}），跳过")

    try:
        from routers import upload as upload_router
        app.include_router(upload_router.router, prefix="/api", tags=["上传"])
        print("[FastAPI] ✅ 已注册 upload 路由")
    except ImportError as e:
        print(f"[FastAPI] ⏭️  upload 路由未就绪（{e}），跳过")

    try:
        from routers import role as role_router
        app.include_router(role_router.router, prefix="/api", tags=["角色权限"])
        print("[FastAPI] ✅ 已注册 role 路由")
    except ImportError as e:
        print(f"[FastAPI] ⏭️  role 路由未就绪（{e}），跳过")

    try:
        from routers import context_pack as cp_router
        app.include_router(cp_router.router, prefix="/api", tags=["上下文包"])
        print("[FastAPI] ✅ 已注册 context_pack 路由")
    except ImportError as e:
        print(f"[FastAPI] ⏭️  context_pack 路由未就绪（{e}），跳过")


register_routers()


# ------------------------------------------------------------------
# 全局异常处理 —— 对齐原 Flask 的 {"status": 1, "msg": "...", "error": "..."} 格式
# ------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """
    FastAPI 默认返回 {"detail": <detail>}，前端 axios 拦截器读的是 response.data.status，
    会拿不到。这里：detail 是 dict 且包含 "status" 时，直接把它作为响应体输出，
    从而完全对齐 Flask 的 {"status":1,"msg":"..."} 格式。
    """
    detail = exc.detail
    if isinstance(detail, dict) and "status" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    # 兜底：非结构化 detail（如 404 自动生成），也包装成兼容格式
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": 1, "msg": str(detail)},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Pydantic 请求校验失败时也返回兼容格式（否则前端会拿到 422 + detail 数组）"""
    return JSONResponse(
        status_code=400,
        content={
            "status": 1,
            "msg": "请求参数不合法",
            "error": str(exc.errors()),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    import traceback
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"status": 1, "msg": "服务器内部错误", "error": str(exc)},
    )

# ------------------------------------------------------------------
# 直接运行（开发用）
#   python main.py
# 生产用：
#   uvicorn main:app --host 0.0.0.0 --port 5000 --workers 4
# ------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
    )