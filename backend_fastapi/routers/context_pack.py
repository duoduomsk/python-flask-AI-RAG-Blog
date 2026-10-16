# backend_fastapi/routers/context_pack.py
"""
上下文包路由，对应原 backend/routes/context_pack.py。

7 个接口：
  GET    /api/context-packs                                       无认证
  GET    /api/context-packs/{pack_id}                             无认证
  POST   /api/context-packs                                       需要认证
  PUT    /api/context-packs/{pack_id}                             需要认证
  DELETE /api/context-packs/{pack_id}                             需要认证
  POST   /api/context-packs/{pack_id}/documents                   需要认证
  DELETE /api/context-packs/{pack_id}/documents/{document_id}     需要认证

⚠️ 假定 context_packs / context_pack_documents 两张表已存在。
   与原 Flask 版一致，不做自动建表。
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from database import engine
from dependencies import get_current_user
from models.context_pack import (
    ContextPackCreateRequest,
    ContextPackDocumentRequest,
    ContextPackUpdateRequest,
)

router = APIRouter()


# ==================================================================
# 工具函数
# ==================================================================
def parse_json_list(value):
    """把 DB 里存的 JSON 字符串解析为列表；失败/空返回 []。"""
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def pack_to_dict(row, documents=None) -> dict:
    """
    把 DB 行转成前端友好的 dict：
    - tags: JSON 字符串 → 列表
    - key_points: JSON 字符串 → 列表，并改名成 keyPoints
    - 日期 → isoformat
    - documents 挂到返回体上
    """
    pack = dict(row)
    pack["tags"] = parse_json_list(pack.get("tags"))
    pack["keyPoints"] = parse_json_list(pack.get("key_points"))
    pack.pop("key_points", None)
    pack["documents"] = documents if documents is not None else []

    if pack.get("created_at"):
        pack["created_at"] = pack["created_at"].isoformat()
    if pack.get("updated_at"):
        pack["updated_at"] = pack["updated_at"].isoformat()

    return pack


def fetch_pack_documents(conn, pack_id: int) -> list:
    """查询某个上下文包内引用的文档列表"""
    result = (
        conn.execute(
            text(
                """
                SELECT a.id, a.title, cpd.note, cpd.sort_order
                FROM context_pack_documents cpd
                JOIN articles a ON a.id = cpd.document_id
                WHERE cpd.pack_id = :pack_id
                ORDER BY cpd.sort_order ASC, cpd.created_at ASC
                """
            ),
            {"pack_id": pack_id},
        )
        .mappings()
        .fetchall()
    )
    return [dict(row) for row in result]


# ==================================================================
# GET /api/context-packs
# ==================================================================
@router.get("/context-packs")
def get_context_packs(
    type: str = Query(""),
    owner_id: str = Query(""),
):
    try:
        with engine.connect() as conn:
            sql = """
                SELECT cp.*, COUNT(cpd.id) AS documents_count
                FROM context_packs cp
                LEFT JOIN context_pack_documents cpd ON cp.id = cpd.pack_id
                WHERE 1=1
            """
            params = {}

            if type:
                sql += " AND cp.type = :type"
                params["type"] = type
            if owner_id:
                sql += " AND cp.owner_id = :owner_id"
                params["owner_id"] = owner_id

            sql += " GROUP BY cp.id ORDER BY cp.updated_at DESC, cp.created_at DESC"

            rows = conn.execute(text(sql), params).mappings().fetchall()

            packs = []
            for row in rows:
                documents = fetch_pack_documents(conn, row["id"])
                pack = pack_to_dict(row, documents)
                pack["documents_count"] = row["documents_count"]
                packs.append(pack)

            return {"status": 0, "data": packs}

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "获取上下文包失败", "error": str(e)},
        )


# ==================================================================
# GET /api/context-packs/{pack_id}
# ==================================================================
@router.get("/context-packs/{pack_id}")
def get_context_pack(pack_id: int):
    try:
        with engine.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT * FROM context_packs WHERE id = :id"),
                    {"id": pack_id},
                )
                .mappings()
                .fetchone()
            )

            if not row:
                raise HTTPException(
                    status_code=404,
                    detail={"status": 1, "msg": "上下文包不存在"},
                )

            documents = fetch_pack_documents(conn, pack_id)
            return {"status": 0, "data": pack_to_dict(row, documents)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "获取上下文包失败", "error": str(e)},
        )


# ==================================================================
# POST /api/context-packs
# ==================================================================
@router.post("/context-packs")
def create_context_pack(
    req: ContextPackCreateRequest,
    current_user_id: int = Depends(get_current_user),
):
    if not req.name:
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "上下文包名称不能为空"},
        )

    # 兼容 keyPoints / key_points 两种传参
    key_points = req.keyPoints or req.key_points or []

    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                    INSERT INTO context_packs (
                        name, type, description, intent, summary,
                        key_points, tags, owner_id, visibility
                    ) VALUES (
                        :name, :type, :description, :intent, :summary,
                        :key_points, :tags, :owner_id, :visibility
                    )
                    """
                ),
                {
                    "name": req.name,
                    "type": req.type or "project",
                    "description": req.description or "",
                    "intent": req.intent or "",
                    "summary": req.summary or "",
                    "key_points": json.dumps(key_points, ensure_ascii=False),
                    "tags": json.dumps(req.tags or [], ensure_ascii=False),
                    "owner_id": current_user_id,
                    "visibility": req.visibility or "private",
                },
            )
            conn.commit()

            pack_id = result.lastrowid
            if not pack_id:
                pack_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()

            return {
                "status": 0,
                "msg": "上下文包已创建",
                "data": {"id": pack_id},
            }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "创建上下文包失败", "error": str(e)},
        )


# ==================================================================
# PUT /api/context-packs/{pack_id}
# ==================================================================
@router.put("/context-packs/{pack_id}")
def update_context_pack(
    pack_id: int,
    req: ContextPackUpdateRequest,
    current_user_id: int = Depends(get_current_user),
):
    try:
        with engine.connect() as conn:
            # 存在性 + 权限校验
            existing = (
                conn.execute(
                    text("SELECT owner_id FROM context_packs WHERE id = :id"),
                    {"id": pack_id},
                )
                .mappings()
                .fetchone()
            )
            if not existing:
                raise HTTPException(
                    status_code=404,
                    detail={"status": 1, "msg": "上下文包不存在"},
                )
            if existing["owner_id"] and existing["owner_id"] != current_user_id:
                raise HTTPException(
                    status_code=403,
                    detail={"status": 1, "msg": "无权修改该上下文包"},
                )

            # 只更新实际传过来的字段
            update_parts = []
            params = {"id": pack_id}

            # 普通字段映射：请求字段名 → 数据库列名
            field_map = {
                "name": "name",
                "type": "type",
                "description": "description",
                "intent": "intent",
                "summary": "summary",
                "visibility": "visibility",
            }

            data = req.model_dump(exclude_unset=True)  # 只取显式传入的字段
            for body_field, column in field_map.items():
                if body_field in data:
                    update_parts.append(f"{column} = :{column}")
                    params[column] = data[body_field]

            # 特殊字段：tags
            if "tags" in data:
                update_parts.append("tags = :tags")
                params["tags"] = json.dumps(req.tags or [], ensure_ascii=False)

            # 特殊字段：keyPoints / key_points
            if "keyPoints" in data or "key_points" in data:
                kp = req.keyPoints if req.keyPoints is not None else req.key_points
                update_parts.append("key_points = :key_points")
                params["key_points"] = json.dumps(kp or [], ensure_ascii=False)

            if not update_parts:
                return {"status": 0, "msg": "无变更"}

            sql = (
                f"UPDATE context_packs SET {', '.join(update_parts)} "
                "WHERE id = :id"
            )
            conn.execute(text(sql), params)
            conn.commit()

            return {"status": 0, "msg": "上下文包已更新"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "更新上下文包失败", "error": str(e)},
        )


# ==================================================================
# DELETE /api/context-packs/{pack_id}
# ==================================================================
@router.delete("/context-packs/{pack_id}")
def delete_context_pack(
    pack_id: int,
    current_user_id: int = Depends(get_current_user),
):
    try:
        with engine.connect() as conn:
            existing = (
                conn.execute(
                    text("SELECT owner_id FROM context_packs WHERE id = :id"),
                    {"id": pack_id},
                )
                .mappings()
                .fetchone()
            )
            if not existing:
                raise HTTPException(
                    status_code=404,
                    detail={"status": 1, "msg": "上下文包不存在"},
                )
            if existing["owner_id"] and existing["owner_id"] != current_user_id:
                raise HTTPException(
                    status_code=403,
                    detail={"status": 1, "msg": "无权删除该上下文包"},
                )

            conn.execute(
                text("DELETE FROM context_packs WHERE id = :id"), {"id": pack_id}
            )
            conn.commit()
            return {"status": 0, "msg": "上下文包已删除"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "删除上下文包失败", "error": str(e)},
        )


# ==================================================================
# POST /api/context-packs/{pack_id}/documents
# ==================================================================
@router.post("/context-packs/{pack_id}/documents")
def add_context_pack_document(
    pack_id: int,
    req: ContextPackDocumentRequest,
    current_user_id: int = Depends(get_current_user),
):
    if not req.document_id:
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "缺少文档 ID"},
        )

    try:
        with engine.connect() as conn:
            pack = (
                conn.execute(
                    text("SELECT owner_id FROM context_packs WHERE id = :id"),
                    {"id": pack_id},
                )
                .mappings()
                .fetchone()
            )
            if not pack:
                raise HTTPException(
                    status_code=404,
                    detail={"status": 1, "msg": "上下文包不存在"},
                )
            if pack["owner_id"] and pack["owner_id"] != current_user_id:
                raise HTTPException(
                    status_code=403,
                    detail={"status": 1, "msg": "无权修改该上下文包"},
                )

            conn.execute(
                text(
                    """
                    INSERT INTO context_pack_documents
                        (pack_id, document_id, note, sort_order)
                    VALUES
                        (:pack_id, :document_id, :note, :sort_order)
                    ON DUPLICATE KEY UPDATE
                        note = VALUES(note),
                        sort_order = VALUES(sort_order)
                    """
                ),
                {
                    "pack_id": pack_id,
                    "document_id": req.document_id,
                    "note": req.note or "",
                    "sort_order": req.sort_order or 0,
                },
            )
            conn.commit()
            return {"status": 0, "msg": "文档已加入上下文包"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "添加文档失败", "error": str(e)},
        )


# ==================================================================
# DELETE /api/context-packs/{pack_id}/documents/{document_id}
# ==================================================================
@router.delete("/context-packs/{pack_id}/documents/{document_id}")
def remove_context_pack_document(
    pack_id: int,
    document_id: int,
    current_user_id: int = Depends(get_current_user),
):
    try:
        with engine.connect() as conn:
            pack = (
                conn.execute(
                    text("SELECT owner_id FROM context_packs WHERE id = :id"),
                    {"id": pack_id},
                )
                .mappings()
                .fetchone()
            )
            if not pack:
                raise HTTPException(
                    status_code=404,
                    detail={"status": 1, "msg": "上下文包不存在"},
                )
            if pack["owner_id"] and pack["owner_id"] != current_user_id:
                raise HTTPException(
                    status_code=403,
                    detail={"status": 1, "msg": "无权修改该上下文包"},
                )

            conn.execute(
                text(
                    """
                    DELETE FROM context_pack_documents
                    WHERE pack_id = :pack_id AND document_id = :document_id
                    """
                ),
                {"pack_id": pack_id, "document_id": document_id},
            )
            conn.commit()
            return {"status": 0, "msg": "文档已移出上下文包"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "移除文档失败", "error": str(e)},
        )