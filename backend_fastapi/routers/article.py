# backend_fastapi/routers/article.py
"""
文章路由，对应原 backend/routes/article.py。

10 个接口（按注册顺序，my-likes 必须在 {article_id} 之前）：
  GET    /api/articles                          无认证
  GET    /api/articles/my-likes                 需要认证
  GET    /api/articles/{article_id}             可选认证（判断 is_liked）
  POST   /api/articles                          需要认证
  PUT    /api/articles/{article_id}             需要认证
  DELETE /api/articles/{article_id}             需要认证
  POST   /api/articles/{article_id}/like        需要认证
  POST   /api/articles/{article_id}/view        无认证
  GET    /api/articles/{article_id}/comments    无认证
  POST   /api/articles/{article_id}/comments    需要认证

响应体格式与原 Flask 完全一致。
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from database import engine
from dependencies import get_current_user, get_current_user_optional
from models.article import (
    ArticleCreateRequest,
    ArticleUpdateRequest,
    CommentCreateRequest,
)

router = APIRouter()


# ==================================================================
# 内部工具：把数据库行转成前端友好的 dict
# ==================================================================
def _row_to_article(row, *, parse_tags=True, iso_dates=True):
    """把一行文章记录转成 dict，处理 tags JSON 解析和日期格式化。"""
    article = dict(row)

    if parse_tags and article.get("tags"):
        try:
            article["tags"] = json.loads(article["tags"])
        except Exception:
            article["tags"] = []
    elif parse_tags:
        article["tags"] = []

    if iso_dates:
        if article.get("created_at"):
            article["created_at"] = article["created_at"].isoformat()
        if article.get("updated_at"):
            article["updated_at"] = article["updated_at"].isoformat()

    return article


# ==================================================================
# GET /api/articles   —— 文章列表（无认证）
# ⚠️ 必须放在 /{article_id} 之前？其实不需要——列表走 /articles 根路径，
#    跟 /articles/{article_id} 不冲突。但 my-likes 必须放在详情之前。
# ==================================================================
@router.get("/articles")
def get_articles(
    search: Optional[str] = Query(None),
    title: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    author_id: Optional[str] = Query(None),
):
    try:
        with engine.connect() as conn:
            query_str = """
                SELECT a.*, u.username as author_name, u.avatar as author_avatar
                FROM articles a
                LEFT JOIN users u ON a.author_id = u.id
                WHERE 1=1
            """
            params = {}

            if search:
                query_str += (
                    " AND (a.title LIKE :search OR a.summary LIKE :search "
                    "OR a.content LIKE :search)"
                )
                params["search"] = f"%{search}%"

            if title:
                query_str += " AND a.title LIKE :title"
                params["title"] = f"%{title}%"

            if category:
                query_str += " AND a.category = :category"
                params["category"] = category

            if tag:
                # tags 存的是 JSON 字符串，用 LIKE 模糊匹配（与原 Flask 一致）
                query_str += " AND a.tags LIKE :tag"
                params["tag"] = f"%{tag}%"

            if status:
                query_str += " AND a.status = :status"
                params["status"] = status

            if author_id:
                query_str += " AND a.author_id = :author_id"
                params["author_id"] = author_id

            query_str += " ORDER BY a.created_at DESC"

            result = conn.execute(text(query_str), params).mappings().fetchall()
            articles_list = [_row_to_article(row) for row in result]

            return {"status": 0, "data": articles_list}

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "获取列表失败", "error": str(e)},
        )


# ==================================================================
# GET /api/articles/my-likes   —— 我点赞的文章（需要认证）
# ⚠️ 必须放在 /articles/{article_id} 之前
# ==================================================================
@router.get("/articles/my-likes")
def get_my_likes(current_user_id: int = Depends(get_current_user)):
    try:
        with engine.connect() as conn:
            result = (
                conn.execute(
                    text(
                        """
                        SELECT a.*, u.username as author_name, u.avatar as author_avatar
                        FROM articles a
                        JOIN article_likes al ON a.id = al.article_id
                        LEFT JOIN users u ON a.author_id = u.id
                        WHERE al.user_id = :uid
                        ORDER BY al.created_at DESC
                        """
                    ),
                    {"uid": current_user_id},
                )
                .mappings()
                .fetchall()
            )

            articles_list = [_row_to_article(row) for row in result]
            return {"status": 0, "data": articles_list}

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "获取失败", "error": str(e)},
        )


# ==================================================================
# GET /api/articles/{article_id}   —— 文章详情（可选认证）
# ==================================================================
@router.get("/articles/{article_id}")
def get_article(
    article_id: int,
    current_user_id: Optional[int] = Depends(get_current_user_optional),
):
    try:
        with engine.connect() as conn:
            result = (
                conn.execute(
                    text(
                        """
                        SELECT a.*, u.username as author_name, u.avatar as author_avatar
                        FROM articles a
                        LEFT JOIN users u ON a.author_id = u.id
                        WHERE a.id = :id
                        """
                    ),
                    {"id": article_id},
                )
                .mappings()
                .fetchone()
            )

            if not result:
                raise HTTPException(
                    status_code=404,
                    detail={"status": 1, "msg": "文章不存在"},
                )

            article = _row_to_article(result)

            # 当前用户是否已点赞
            article["is_liked"] = False
            if current_user_id:
                liked = conn.execute(
                    text(
                        "SELECT id FROM article_likes "
                        "WHERE user_id = :uid AND article_id = :aid"
                    ),
                    {"uid": current_user_id, "aid": article_id},
                ).fetchone()
                if liked:
                    article["is_liked"] = True

            return {"status": 0, "data": article}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "获取失败", "error": str(e)},
        )


# ==================================================================
# POST /api/articles   —— 创建文章（需要认证）
# ==================================================================
@router.post("/articles")
def create_article(
    req: ArticleCreateRequest,
    current_user_id: int = Depends(get_current_user),
):
    title = req.title
    content = req.content
    summary = req.summary
    category = req.category
    tags = req.tags
    cover_image = req.cover_image
    status = req.status or "published"

    if not title or not content:
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "标题和内容不能为空"},
        )

    try:
        tags_json = json.dumps(tags if tags else [], ensure_ascii=False)

        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                    INSERT INTO articles
                        (title, content, summary, category, tags,
                         author_id, cover_image, status)
                    VALUES
                        (:title, :content, :summary, :category, :tags,
                         :author_id, :cover_image, :status)
                    """
                ),
                {
                    "title": title,
                    "content": content,
                    "summary": summary,
                    "category": category,
                    "tags": tags_json,
                    "author_id": current_user_id,
                    "cover_image": cover_image,
                    "status": status,
                },
            )
            conn.commit()

            article_id = result.lastrowid
            if not article_id:
                article_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()

        return {
            "status": 0,
            "msg": "文章发布成功",
            "data": {"id": article_id, "title": title},
        }

    except Exception as e:
        print(f"Error creating article: {e}")
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "发布失败", "error": str(e)},
        )


# ==================================================================
# PUT /api/articles/{article_id}   —— 更新文章（需要认证）
# 保留原 Flask 的 admin 旁路：admin 或拥有 article:manage 权限可修改他人文章
# ==================================================================
@router.put("/articles/{article_id}")
def update_article(
    article_id: int,
    req: ArticleUpdateRequest,
    current_user_id: int = Depends(get_current_user),
):
    title = req.title
    content = req.content
    summary = req.summary
    category = req.category
    tags = req.tags
    cover_image = req.cover_image
    status = req.status

    try:
        tags_json = json.dumps(tags if tags else [], ensure_ascii=False)

        with engine.connect() as conn:
            # 1. 文章是否存在
            article = (
                conn.execute(
                    text("SELECT author_id FROM articles WHERE id = :id"),
                    {"id": article_id},
                )
                .mappings()
                .fetchone()
            )

            if not article:
                raise HTTPException(
                    status_code=404,
                    detail={"status": 1, "msg": "文章不存在"},
                )

            # 2. 权限校验（作者本人 or 管理员/有 article:manage 权限）
            user = (
                conn.execute(
                    text("SELECT role, role_id FROM users WHERE id = :id"),
                    {"id": current_user_id},
                )
                .mappings()
                .fetchone()
            )

            is_admin = False
            if user:
                if user.get("role") == "admin":
                    is_admin = True
                elif user.get("role_id"):
                    perm = conn.execute(
                        text(
                            """
                            SELECT p.code FROM permissions p
                            JOIN role_permissions rp ON p.id = rp.permission_id
                            WHERE rp.role_id = :rid AND p.code = 'article:manage'
                            """
                        ),
                        {"rid": user["role_id"]},
                    ).fetchone()
                    if perm:
                        is_admin = True

            if article["author_id"] != current_user_id and not is_admin:
                raise HTTPException(
                    status_code=403,
                    detail={"status": 1, "msg": "无权修改此文章"},
                )

            # 3. 动态拼接 UPDATE
            update_parts = []
            params = {"id": article_id}

            if title is not None:
                update_parts.append("title=:title")
                params["title"] = title
            if content is not None:
                update_parts.append("content=:content")
                params["content"] = content
            if summary is not None:
                update_parts.append("summary=:summary")
                params["summary"] = summary
            if category is not None:
                update_parts.append("category=:category")
                params["category"] = category
            if tags is not None:
                update_parts.append("tags=:tags")
                params["tags"] = tags_json
            if cover_image is not None:
                update_parts.append("cover_image=:cover_image")
                params["cover_image"] = cover_image
            if status is not None:
                update_parts.append("status=:status")
                params["status"] = status

            if not update_parts:
                return {"status": 0, "msg": "无变更"}

            update_sql = (
                f"UPDATE articles SET {', '.join(update_parts)} WHERE id=:id"
            )
            conn.execute(text(update_sql), params)
            conn.commit()

        return {"status": 0, "msg": "文章更新成功"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "更新失败", "error": str(e)},
        )


# ==================================================================
# DELETE /api/articles/{article_id}   —— 删除文章（需要认证）
# ⚠️ 与原 Flask 一致：仅允许作者本人删除，admin 也不放开
# ==================================================================
@router.delete("/articles/{article_id}")
def delete_article(
    article_id: int,
    current_user_id: int = Depends(get_current_user),
):
    try:
        with engine.connect() as conn:
            article = (
                conn.execute(
                    text("SELECT author_id FROM articles WHERE id = :id"),
                    {"id": article_id},
                )
                .mappings()
                .fetchone()
            )

            if not article:
                raise HTTPException(
                    status_code=404,
                    detail={"status": 1, "msg": "文章不存在"},
                )

            if article["author_id"] != current_user_id:
                raise HTTPException(
                    status_code=403,
                    detail={"status": 1, "msg": "无权删除此文章"},
                )

            conn.execute(
                text("DELETE FROM articles WHERE id=:id"), {"id": article_id}
            )
            conn.commit()
            return {"status": 0, "msg": "删除成功"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "删除失败", "error": str(e)},
        )


# ==================================================================
# POST /api/articles/{article_id}/like   —— 点赞/取消（需要认证）
# ==================================================================
@router.post("/articles/{article_id}/like")
def toggle_like(
    article_id: int,
    current_user_id: int = Depends(get_current_user),
):
    try:
        with engine.connect() as conn:
            # 是否已点赞
            liked = conn.execute(
                text(
                    "SELECT id FROM article_likes "
                    "WHERE user_id = :uid AND article_id = :aid"
                ),
                {"uid": current_user_id, "aid": article_id},
            ).fetchone()

            is_liked = False
            if liked:
                # 取消点赞
                conn.execute(
                    text(
                        "DELETE FROM article_likes "
                        "WHERE user_id = :uid AND article_id = :aid"
                    ),
                    {"uid": current_user_id, "aid": article_id},
                )
                conn.execute(
                    text("UPDATE articles SET likes = likes - 1 WHERE id = :aid"),
                    {"aid": article_id},
                )
                msg = "已取消点赞"
                is_liked = False
            else:
                # 点赞
                conn.execute(
                    text(
                        "INSERT INTO article_likes (user_id, article_id) "
                        "VALUES (:uid, :aid)"
                    ),
                    {"uid": current_user_id, "aid": article_id},
                )
                conn.execute(
                    text("UPDATE articles SET likes = likes + 1 WHERE id = :aid"),
                    {"aid": article_id},
                )
                msg = "点赞成功"
                is_liked = True

            conn.commit()

            # 返回最新点赞数
            likes_count = conn.execute(
                text("SELECT likes FROM articles WHERE id = :aid"),
                {"aid": article_id},
            ).scalar()

            return {
                "status": 0,
                "msg": msg,
                "data": {"liked": is_liked, "likes": likes_count},
            }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "操作失败", "error": str(e)},
        )


# ==================================================================
# POST /api/articles/{article_id}/view   —— 浏览量 +1（无认证）
# ==================================================================
@router.post("/articles/{article_id}/view")
def increment_view(article_id: int):
    try:
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE articles SET views = views + 1 WHERE id = :id"),
                {"id": article_id},
            )
            conn.commit()
            return {"status": 0, "msg": "浏览量已增加"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "操作失败", "error": str(e)},
        )


# ==================================================================
# GET /api/articles/{article_id}/comments   —— 评论列表（无认证）
# ==================================================================
@router.get("/articles/{article_id}/comments")
def get_article_comments(article_id: int):
    try:
        with engine.connect() as conn:
            query = """
                SELECT c.*, u.username as user_name, u.avatar as user_avatar
                FROM comments c
                LEFT JOIN users u ON c.user_id = u.id
                WHERE c.article_id = :aid
                ORDER BY c.created_at DESC
            """
            result = (
                conn.execute(text(query), {"aid": article_id})
                .mappings()
                .fetchall()
            )

            comments_list = []
            for row in result:
                comment = dict(row)
                if comment.get("created_at"):
                    comment["created_at"] = comment["created_at"].isoformat()
                comments_list.append(comment)

            return {"status": 0, "data": comments_list}

    except Exception as e:
        print(f"Error getting comments: {e}")
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "获取评论失败", "error": str(e)},
        )


# ==================================================================
# POST /api/articles/{article_id}/comments   —— 发表评论（需要认证）
# ==================================================================
@router.post("/articles/{article_id}/comments")
def create_article_comment(
    article_id: int,
    req: CommentCreateRequest,
    current_user_id: int = Depends(get_current_user),
):
    content = req.content

    if not content or not content.strip():
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "评论内容不能为空"},
        )

    try:
        with engine.connect() as conn:
            # 检查文章是否存在
            article = conn.execute(
                text("SELECT id FROM articles WHERE id = :aid"),
                {"aid": article_id},
            ).fetchone()

            if not article:
                raise HTTPException(
                    status_code=404,
                    detail={"status": 1, "msg": "文章不存在"},
                )

            # 插入评论
            conn.execute(
                text(
                    """
                    INSERT INTO comments (article_id, user_id, content)
                    VALUES (:aid, :uid, :content)
                    """
                ),
                {"aid": article_id, "uid": current_user_id, "content": content},
            )
            conn.commit()

            return {"status": 0, "msg": "评论发表成功"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error creating comment: {e}")
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "发表评论失败", "error": str(e)},
        )