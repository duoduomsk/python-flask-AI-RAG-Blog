# backend_fastapi/dependencies.py
"""
替代原 backend/middleware.py 中的 @token_required 装饰器。

FastAPI 使用 Depends() 而不是装饰器，因此把原校验逻辑封装成依赖函数。

与原 middleware.py 保持完全一致的校验步骤：
  1. 从 Authorization 头提取 token（支持可选 "Bearer " 前缀）
  2. 用 SECRET_KEY 解码 JWT（HS256）
  3. 查库验证 pwd_hash（密码哈希）和 role_id 是否被管理员修改
  4. 任一不通过 → 401 + {"status": 1, "msg": "..."}（由 main.py 的处理器输出）
"""
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text

from config import JWT_ALGORITHM, SECRET_KEY
from database import engine


# auto_error=False → 缺失/格式不对的 Authorization 返回 None，由我们自定义错误消息。
# （默认 auto_error=True 会直接返回 403 且响应体是 {"detail":"Not authenticated"}，格式不兼容）
_bearer_scheme = HTTPBearer(auto_error=False)


def _extract_token(request: Request) -> Optional[str]:
    """
    按原 middleware.py 的方式提取 token：
    优先取 Authorization 头，如果以 "Bearer " 开头则去掉该前缀。
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None
    token = auth_header
    if token.startswith("Bearer "):
        token = token.split(" ", 1)[1]
    return token.strip() or None


def _401(msg: str, error: Optional[str] = None) -> HTTPException:
    """构造 401 响应体，格式与原 Flask 完全一致"""
    detail = {"status": 1, "msg": msg}
    if error:
        detail["error"] = error
    return HTTPException(status_code=401, detail=detail)


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> int:
    """
    FastAPI 依赖：替代原 @token_required。
    返回 current_user_id（int）。用法：

        @router.get("/foo")
        def foo(current_user_id: int = Depends(get_current_user)):
            ...
    """
    token = _extract_token(request)
    if not token:
        raise _401("Token is missing!")

    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise _401("Token is invalid!", "Token expired")
    except jwt.InvalidTokenError as e:
        raise _401("Token is invalid!", str(e))

    current_user_id = data.get("user_id")
    if not current_user_id:
        raise _401("Token is invalid!", "missing user_id")

    # 与原 middleware.py 一致：实时比对数据库，检测密码 / 角色是否被管理员修改
    with engine.connect() as conn:
        user = conn.execute(
            text("SELECT password, role_id FROM users WHERE id=:id"),
            {"id": current_user_id},
        ).mappings().fetchone()

    if not user:
        raise _401("用户不存在!")

    if data.get("pwd_hash") != user["password"]:
        raise _401("密码已被修改，请重新登录!")

    if data.get("role_id") != user["role_id"]:
        raise _401("账号权限已被修改，请重新登录!")

    return int(current_user_id)


def get_current_user_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[int]:
    """
    可选认证依赖：用于「登录了附带信息、没登录也能看」的接口（例如 GET /api/articles/{id}）。
    解码失败或用户不存在时返回 None，不抛错。
    （与原 Flask 版用 try/except 包裹 jwt.decode 的效果一致）
    """
    token = _extract_token(request)
    if not token:
        return None
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        uid = data.get("user_id")
        return int(uid) if uid else None
    except Exception:
        return None


def get_db():
    """SQLAlchemy Session 依赖（暂时未用，预留给后续 service 层）"""
    from database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()