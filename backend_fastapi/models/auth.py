# backend_fastapi/models/auth.py
"""
认证模块的请求模型。

设计说明：
- 字段全部 Optional，由路由内部做业务校验（例如"用户名和密码不能为空"），
  这样能返回与 Flask 一致的 {"status":1,"msg":"..."} + 400，而不是 Pydantic 默认的 422。
- role_id 用 Optional[int]，Pydantic 会自动把前端传来的字符串数字（如 "1"）强转成 int。
"""
from typing import Optional

from pydantic import BaseModel


class RegisterRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    avatar: Optional[str] = ""
    email: Optional[str] = None
    role_id: Optional[int] = None


class LoginRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None


class UserUpdateRequest(BaseModel):
    username: Optional[str] = None
    avatar: Optional[str] = None
    password: Optional[str] = None
    role_id: Optional[int] = None
    email: Optional[str] = None