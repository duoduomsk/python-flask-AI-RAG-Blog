# backend_fastapi/routers/auth.py
"""
认证路由，对应原 backend/routes/auth.py。

6 个接口：
  POST   /api/register
  POST   /api/login
  GET    /api/user/info
  GET    /api/users
  PUT    /api/users/{user_id}
  DELETE /api/users/{user_id}

所有响应体格式与原 Flask 完全一致：{"status": 0/1, "msg": "...", "data": {...}}
"""
import datetime
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

from config import JWT_ALGORITHM, JWT_EXPIRE_HOURS, SECRET_KEY
from database import engine
from dependencies import get_current_user
from models.auth import LoginRequest, RegisterRequest, UserUpdateRequest

router = APIRouter()


# ==================================================================
# POST /api/register
# ==================================================================
@router.post("/register")
def register(req: RegisterRequest):
    username = req.username
    password = req.password
    avatar = req.avatar
    email = req.email

    # 简单校验
    if not username or not password:
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "用户名和密码不能为空"},
        )

    # ⚠️ 核心修复：拒绝弱密码/占位符作为真实密码（保留原逻辑）
    banned_passwords = ["......", "******", "password"]
    if password.strip() in banned_passwords:
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "密码过于简单或包含非法字符，请重新设置"},
        )

    role_id = req.role_id

    with engine.connect() as conn:
        # 如果没有指定 role_id，默认使用 'user' 角色的 ID
        if not role_id:
            role_id = conn.execute(
                text("SELECT id FROM roles WHERE name='user'")
            ).scalar()

        # 判断用户名是否存在
        existing_user = conn.execute(
            text("SELECT * FROM users WHERE username=:username"),
            {"username": username},
        ).fetchone()

        if existing_user:
            raise HTTPException(
                status_code=400,
                detail={
                    "status": 1,
                    "msg": "用户名已存在",
                    "data": {"success": False},
                },
            )

        # 密码加密（沿用 werkzeug，保证与原后端数据库完全兼容）
        hashed_password = generate_password_hash(password)

        # role 列用于兼容旧表结构，同步写入角色名
        role_name = conn.execute(
            text("SELECT name FROM roles WHERE id=:id"), {"id": role_id}
        ).scalar()

        conn.execute(
            text(
                "INSERT INTO users (username, password, avatar, role, role_id, email) "
                "VALUES (:username, :password, :avatar, :role, :role_id, :email)"
            ),
            {
                "username": username,
                "password": hashed_password,
                "avatar": avatar or "",
                "role": role_name,
                "role_id": role_id,
                "email": email,
            },
        )
        conn.commit()

    return {"status": 0, "msg": "注册成功", "data": {"success": True}}


# ==================================================================
# POST /api/login
# ==================================================================
@router.post("/login")
def login(req: LoginRequest):
    username = req.username
    password = req.password

    if not username or not password:
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "用户名和密码不能为空"},
        )

    try:
        with engine.connect() as conn:
            result = (
                conn.execute(
                    text("SELECT * FROM users WHERE username=:username"),
                    {"username": username},
                )
                .mappings()
                .fetchone()
            )

        if not result or not check_password_hash(result["password"], password):
            raise HTTPException(
                status_code=401,
                detail={"status": 1, "msg": "用户名或密码错误"},
            )

        # 获取权限列表
        permissions = []
        if result.get("role_id"):
            with engine.connect() as conn:
                perms = (
                    conn.execute(
                        text(
                            """
                            SELECT p.code FROM permissions p
                            JOIN role_permissions rp ON p.id = rp.permission_id
                            WHERE rp.role_id = :rid
                            """
                        ),
                        {"rid": result["role_id"]},
                    )
                    .scalars()
                    .all()
                )
                permissions = list(perms)

        # 生成 JWT token（payload 与原 Flask 完全一致，可互认）
        token = jwt.encode(
            {
                "user_id": result["id"],
                "username": result["username"],
                "role": result.get("role", "user"),
                "role_id": result.get("role_id"),   # 用于实时会话校验
                "pwd_hash": result["password"],     # 用于实时会话校验
                "exp": datetime.datetime.utcnow()
                + datetime.timedelta(hours=JWT_EXPIRE_HOURS),
            },
            SECRET_KEY,
            algorithm=JWT_ALGORITHM,
        )

        return {
            "status": 0,
            "msg": "登录成功",
            "data": {
                "id": result["id"],
                "username": result["username"],
                "role": result.get("role", "user"),
                "email": result.get("email"),
                "permissions": permissions,
                "token": token,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "登录失败", "error": str(e)},
        )


# ==================================================================
# GET /api/user/info
# ==================================================================
@router.get("/user/info")
def get_user_info(current_user_id: int = Depends(get_current_user)):
    with engine.connect() as conn:
        user = (
            conn.execute(
                text(
                    "SELECT id, username, avatar, role, role_id, email, created_at "
                    "FROM users WHERE id=:id"
                ),
                {"id": current_user_id},
            )
            .mappings()
            .fetchone()
        )

    if not user:
        raise HTTPException(
            status_code=404,
            detail={"status": 1, "msg": "用户不存在"},
        )

    user_data = dict(user)

    # 与原 Flask 一致：created_at 格式化到日（不是 isoformat）
    if user_data.get("created_at"):
        user_data["created_at"] = user_data["created_at"].strftime("%Y-%m-%d")

    # Avue 需要 roles 数组
    user_data["roles"] = [user_data.get("role", "user")]

    # 权限列表
    permissions = []
    if user_data.get("role_id"):
        with engine.connect() as conn:
            perms = (
                conn.execute(
                    text(
                        """
                        SELECT p.code FROM permissions p
                        JOIN role_permissions rp ON p.id = rp.permission_id
                        WHERE rp.role_id = :rid
                        """
                    ),
                    {"rid": user_data["role_id"]},
                )
                .scalars()
                .all()
            )
            permissions = list(perms)

    user_data["permissions"] = permissions
    return {"status": 0, "data": user_data}


# ==================================================================
# GET /api/users
# ==================================================================
@router.get("/users")
def get_users(
    username: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
):
    try:
        with engine.connect() as conn:
            sql = (
                "SELECT id, username, avatar, created_at, role, role_id, email "
                "FROM users WHERE 1=1"
            )
            params = {}

            if username:
                sql += " AND username LIKE :username"
                params["username"] = f"%{username}%"

            if email:
                sql += " AND email LIKE :email"
                params["email"] = f"%{email}%"

            sql += " ORDER BY created_at DESC"

            result = conn.execute(text(sql), params).mappings().fetchall()

            users = []
            for row in result:
                user = dict(row)
                if user.get("created_at"):
                    user["created_at"] = user["created_at"].isoformat()
                users.append(user)

            return {"status": 0, "data": users}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "获取用户列表失败", "error": str(e)},
        )


# ==================================================================
# PUT /api/users/{user_id}
# ⚠️ 核心修复：移除硬编码 ROLE_LEVELS，绑定 username='admin' 为最高级
# ==================================================================
@router.put("/users/{user_id}")
def update_user(
    user_id: int,
    req: UserUpdateRequest,
    current_user_id: int = Depends(get_current_user),
):
    username = req.username
    avatar = req.avatar
    password = req.password
    role_id = req.role_id
    email = req.email

    try:
        with engine.connect() as conn:
            # 1. 获取当前操作者和目标用户的信息
            current_user = (
                conn.execute(
                    text("SELECT id, username, role_id FROM users WHERE id=:id"),
                    {"id": current_user_id},
                )
                .mappings()
                .fetchone()
            )

            target_user = (
                conn.execute(
                    text("SELECT id, role_id FROM users WHERE id=:id"),
                    {"id": user_id},
                )
                .mappings()
                .fetchone()
            )

            if not current_user:
                return {"status": 1, "msg": "操作者不存在"}
            if not target_user:
                return {"status": 1, "msg": "目标用户不存在"}

            # 2. 绑定 admin 为最高级管理员
            is_super_admin = current_user["username"] == "admin"
            new_role_id = int(role_id) if role_id is not None else None

            # ----- 权限拦截逻辑 开始 -----
            if current_user_id == user_id:
                # 修改自己：不允许改自己的角色（防止自我降级）
                if new_role_id is not None and new_role_id != int(target_user["role_id"]):
                    return {"status": 1, "msg": "不能修改自己的管理员状态！"}
            else:
                # 修改他人：只有超级管理员(admin)才允许
                if not is_super_admin:
                    return {
                        "status": 1,
                        "msg": "权限不足！只有超级管理员(admin)才能修改其他用户信息",
                    }
            # ----- 权限拦截逻辑 结束 -----

            # 3. 动态拼接 SQL，只更新真正传过来的字段
            update_fields = []
            params = {"id": user_id}

            if username:
                update_fields.append("username=:username")
                params["username"] = username
            if avatar is not None:
                update_fields.append("avatar=:avatar")
                params["avatar"] = avatar
            if email is not None:
                update_fields.append("email=:email")
                params["email"] = email

            # ⚠️ 核心修复：处理密码占位符与真实修改
            if password and password.strip():
                if password.strip() in ["......", "******", "password"]:
                    # 占位符，跳过，不更新密码
                    pass
                else:
                    update_fields.append("password=:password")
                    params["password"] = generate_password_hash(password)

            if new_role_id is not None:
                role_name = conn.execute(
                    text("SELECT name FROM roles WHERE id=:id"), {"id": new_role_id}
                ).scalar()
                if role_name:
                    update_fields.append("role=:role")
                    update_fields.append("role_id=:role_id")
                    params["role"] = role_name
                    params["role_id"] = new_role_id

            # ⚠️ 核心修复：解决「前端假成功」的问题
            if not update_fields:
                return {
                    "status": 1,
                    "msg": "没有需要更新的内容，请检查是否输入了新密码或修改了角色",
                }

            update_sql = f"UPDATE users SET {', '.join(update_fields)} WHERE id=:id"
            conn.execute(text(update_sql), params)
            conn.commit()

            return {"status": 0, "msg": "更新成功"}

    except Exception as e:
        import traceback
        print("========= 更新用户报错 =========")
        traceback.print_exc()
        print("================================")
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "更新失败", "error": str(e)},
        )


# ==================================================================
# DELETE /api/users/{user_id}
# 注意：与原 Flask 一致，此接口未加 token_required（保持前端调用兼容）
# ==================================================================
@router.delete("/users/{user_id}")
def delete_user(user_id: int):
    try:
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM users WHERE id=:id"), {"id": user_id})
            conn.commit()
            return {"status": 0, "msg": "删除成功"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "删除失败", "error": str(e)},
        )