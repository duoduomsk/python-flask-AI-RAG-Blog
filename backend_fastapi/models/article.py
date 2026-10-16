# backend_fastapi/models/article.py
"""
文章模块的请求模型。

设计原则：
- 全部字段 Optional，把校验交给路由内部的 if/else（返回与 Flask 一致的自定义错误消息）
- tags 使用 Optional[List[str]]，Pydantic 会把 ["a","b"] 保持为列表，
  存储时再 json.dumps() 成字符串（与原代码一致）
"""
from typing import List, Optional

from pydantic import BaseModel


class ArticleCreateRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    summary: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    cover_image: Optional[str] = None
    status: Optional[str] = "published"   # 默认 published，与原 Flask 一致


class ArticleUpdateRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    summary: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    cover_image: Optional[str] = None
    status: Optional[str] = None


class CommentCreateRequest(BaseModel):
    content: Optional[str] = None