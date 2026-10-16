# backend_fastapi/routers/role.py
"""
角色权限路由，对应原 backend/routes/role.py。

5 个接口（全部需要认证）：
  GET    /api/roles
  POST   /api/roles
  PUT    /api/roles/{role_id}
  DELETE /api/roles/{role_id}
  GET    /api/permissions
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from database import engine
from dependencies import get_current_user
from models.role import RoleCreateRequest, RoleUpdateRequest

router = APIRouter()


# ==================================================================
# GET /api/roles
# ==================================================================
@router.get("/roles")
def get_roles(
    name: str = Query(None),
    current_user_id: int = Depends(get_current_user),
):
    try:
        with engine.connect() as conn:
            sql = "SELECT * FROM roles WHERE 1=1"
            params = {}
            if name:
                sql += " AND name LIKE :name"
                params["name"] = f"%{name}%"

            sql += " ORDER BY id"
            roles = conn.execute(text(sql), params).mappings().fetchall()

            roles_data = []
            for role in roles:
                role_dict = dict(role)
                if role_dict.get("created_at"):
                    role_dict["created_at"] = role_dict["created_at"].isoformat()

                # 关联查询该角色的权限
                perms = (
                    conn.execute(
                        text(
                            """
                            SELECT p.id, p.code, p.name
                            FROM permissions p
                            JOIN role_permissions rp ON p.id = rp.permission_id
                            WHERE rp.role_id = :rid
                            """
                        ),
                        {"rid": role_dict["id"]},
                    )
                    .mappings()
                    .fetchall()
                )

                # 前端 Avue 需要 id 列表（用于权限勾选）
                role_dict["permissions"] = [p["id"] for p in perms]
                role_dict["permissionNames"] = [p["name"] for p in perms]

                roles_data.append(role_dict)

            return {"status": 0, "data": roles_data}

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "获取角色列表失败", "error": str(e)},
        )


# ==================================================================
# POST /api/roles
# ==================================================================
@router.post("/roles")
def create_role(
    req: RoleCreateRequest,
    current_user_id: int = Depends(get_current_user),
):
    name = req.name
    description = req.description
    permission_ids = req.permissions or []

    if not name:
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "角色名称不能为空"},
        )

    try:
        with engine.connect() as conn:
            # 重名检测
            existing = conn.execute(
                text("SELECT id FROM roles WHERE name=:name"), {"name": name}
            ).scalar()
            if existing:
                raise HTTPException(
                    status_code=400,
                    detail={"status": 1, "msg": "角色名称已存在"},
                )

            result = conn.execute(
                text(
                    "INSERT INTO roles (name, description) "
                    "VALUES (:name, :description)"
                ),
                {"name": name, "description": description},
            )
            role_id = result.lastrowid
            if not role_id:
                role_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()

            # 插入权限关联
            for pid in permission_ids:
                conn.execute(
                    text(
                        "INSERT INTO role_permissions (role_id, permission_id) "
                        "VALUES (:rid, :pid)"
                    ),
                    {"rid": role_id, "pid": pid},
                )

            conn.commit()
            return {"status": 0, "msg": "创建角色成功"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "创建角色失败", "error": str(e)},
        )


# ==================================================================
# PUT /api/roles/{role_id}
# ==================================================================
@router.put("/roles/{role_id}")
def update_role(
    role_id: int,
    req: RoleUpdateRequest,
    current_user_id: int = Depends(get_current_user),
):
    name = req.name
    description = req.description
    permission_ids = req.permissions   # None 表示不动权限；[] 表示清空

    try:
        with engine.connect() as conn:
            # 更新基本信息（有 name 才更新）
            if name:
                conn.execute(
                    text(
                        "UPDATE roles SET name=:name, description=:description "
                        "WHERE id=:id"
                    ),
                    {"name": name, "description": description, "id": role_id},
                )

            # 更新权限关联（只有权限字段存在时才动）
            if permission_ids is not None:
                conn.execute(
                    text("DELETE FROM role_permissions WHERE role_id=:id"),
                    {"id": role_id},
                )
                for pid in permission_ids:
                    conn.execute(
                        text(
                            "INSERT INTO role_permissions (role_id, permission_id) "
                            "VALUES (:rid, :pid)"
                        ),
                        {"rid": role_id, "pid": pid},
                    )

            conn.commit()
            return {"status": 0, "msg": "更新角色成功"}

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "更新角色失败", "error": str(e)},
        )


# ==================================================================
# DELETE /api/roles/{role_id}
# ==================================================================
@router.delete("/roles/{role_id}")
def delete_role(
    role_id: int,
    current_user_id: int = Depends(get_current_user),
):
    try:
        with engine.connect() as conn:
            # 有用户在用则拒绝删除
            count = conn.execute(
                text("SELECT COUNT(*) FROM users WHERE role_id=:id"),
                {"id": role_id},
            ).scalar()
            if count and count > 0:
                raise HTTPException(
                    status_code=400,
                    detail={"status": 1, "msg": "该角色下还有用户，无法删除"},
                )

            conn.execute(text("DELETE FROM roles WHERE id=:id"), {"id": role_id})
            conn.commit()
            return {"status": 0, "msg": "删除角色成功"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "删除角色失败", "error": str(e)},
        )


# ==================================================================
# GET /api/permissions
# ==================================================================
@router.get("/permissions")
def get_permissions(current_user_id: int = Depends(get_current_user)):
    try:
        with engine.connect() as conn:
            perms = conn.execute(text("SELECT * FROM permissions")).mappings().fetchall()
            return {"status": 0, "data": [dict(p) for p in perms]}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "获取权限列表失败", "error": str(e)},
        )