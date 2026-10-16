# backend_fastapi/models/context_pack.py
from typing import List, Optional
from pydantic import BaseModel


class ContextPackCreateRequest(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = "project"
    description: Optional[str] = ""
    intent: Optional[str] = ""
    summary: Optional[str] = ""
    # 驼峰 / 下划线都接受（与原 Flask 一致）
    keyPoints: Optional[List[str]] = None
    key_points: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    visibility: Optional[str] = "private"


class ContextPackUpdateRequest(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    intent: Optional[str] = None
    summary: Optional[str] = None
    keyPoints: Optional[List[str]] = None
    key_points: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    visibility: Optional[str] = None


class ContextPackDocumentRequest(BaseModel):
    document_id: Optional[int] = None
    note: Optional[str] = ""
    sort_order: Optional[int] = 0