# backend_fastapi/models/ai.py
"""
AI 模块的请求模型。字段全部 Optional，把业务校验留给路由内部。
"""
from typing import Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    user_id: Optional[str] = "default"
    message: Optional[str] = None
    reset_context: Optional[bool] = False
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    system_prompt: Optional[str] = None
    temperature: Optional[float] = 0.3
    max_tokens: Optional[int] = 500


class ChatStreamRequest(BaseModel):
    """SSE 流式聊天请求，字段与 ChatRequest 一致"""
    user_id: Optional[str] = "default"
    message: Optional[str] = None
    reset_context: Optional[bool] = False
    model_provider: Optional[str] = None
    model_name: Optional[str] = None
    system_prompt: Optional[str] = None
    temperature: Optional[float] = 0.3
    max_tokens: Optional[int] = 500


class GenerateArticleRequest(BaseModel):
    topic: Optional[str] = None


class ConfigCreateRequest(BaseModel):
    provider: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = ""
    is_active: Optional[bool] = False


class ConfigUpdateRequest(BaseModel):
    provider: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    is_active: Optional[bool] = None