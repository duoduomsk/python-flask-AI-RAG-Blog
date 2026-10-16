# backend_fastapi/models/role.py
from typing import List, Optional
from pydantic import BaseModel


class RoleCreateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[List[int]] = None   # 权限 ID 列表


class RoleUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[List[int]] = None