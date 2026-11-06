# backend/routes/rag.py
from flask import Blueprint, request, jsonify
from sqlalchemy import text
from backend.database import engine
from backend.middleware import token_required
from backend.limiter import limiter
from backend.tasks.rag_tasks import rebuild_index_task, delete_article_task
from backend.rag.retriever import retrieve, retrieve_debug

rag_bp = Blueprint('rag', __name__)


def _is_admin(conn, user_id: int) -> bool:
    row = conn.execute(text(
        "SELECT role, role_id FROM users WHERE id = :id"
    ), {"id": user_id}).mappings().fetchone()
    if not row:
        return False
    if row.get("role") == "admin":
        return True
    if row.get("role_id"):
        hit = conn.execute(text("""
            SELECT 1 FROM permissions p
            JOIN role_permissions rp ON rp.permission_id = p.id
            WHERE rp.role_id = :rid
              AND p.code IN ('ai:manage', 'article:manage')
            LIMIT 1
        """), {"rid": row["role_id"]}).fetchone()
        return bool(hit)
    return False


@rag_bp.route('/api/rag/rebuild', methods=['POST'])
@token_required
@limiter.limit("5/hour")
def rebuild(current_user_id):
    with engine.connect() as conn:
        if not _is_admin(conn, current_user_id):
            return jsonify({'status': 1, 'msg': '权限不足'}), 403
    task = rebuild_index_task.delay()
    return jsonify({'status': 0, 'msg': '已提交全量重建任务',
                    'data': {'task_id': task.id}})


@rag_bp.route('/api/rag/status', methods=['GET'])
@token_required
def status(current_user_id):
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT status, COUNT(*) AS cnt FROM rag_documents GROUP BY status"
        )).mappings().fetchall()
        summary = {r["status"]: r["cnt"] for r in rows}

        failed_rows = conn.execute(text("""
            SELECT article_id, error_msg, updated_at FROM rag_documents
            WHERE status = 'failed' ORDER BY updated_at DESC LIMIT 20
        """)).mappings().fetchall()

    failed = []
    for r in failed_rows:
        item = dict(r)
        if item.get("updated_at"):
            item["updated_at"] = item["updated_at"].isoformat()
        failed.append(item)

    return jsonify({'status': 0, 'data': {'summary': summary, 'failed': failed}})


@rag_bp.route('/api/rag/search-test', methods=['GET'])
@token_required
def search_test(current_user_id):
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'status': 1, 'msg': '缺少 q 参数'}), 400
    results = retrieve(q, top_k=5)
    return jsonify({'status': 0, 'data': results})


@rag_bp.route('/api/rag/documents/<int:article_id>', methods=['DELETE'])
@token_required
def delete_doc(current_user_id, article_id):
    with engine.connect() as conn:
        if not _is_admin(conn, current_user_id):
            return jsonify({'status': 1, 'msg': '权限不足'}), 403
    delete_article_task.delay(article_id)
    return jsonify({'status': 0, 'msg': '已提交删除索引任务'})

@rag_bp.route('/api/rag/search-debug', methods=['GET'])
@token_required
def search_debug(current_user_id):
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'status': 1, 'msg': '缺少 q 参数'}), 400
    try:
        data = retrieve_debug(q)
        return jsonify({'status': 0, 'data': data})
    except Exception as e:
        return jsonify({'status': 1, 'msg': str(e)}), 500