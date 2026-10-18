# from functools import wraps
# from flask import request, jsonify, current_app
# import jwt
#
# def token_required(f):
#     @wraps(f)
#     def decorated(*args, **kwargs):
#         token = None
#         if 'Authorization' in request.headers:
#             token = request.headers['Authorization']
#
#         if not token:
#             return jsonify({'status': 1, 'msg': 'Token is missing!'}), 401
#
#         try:
#             # Check if token starts with Bearer
#             if token.startswith('Bearer '):
#                 token = token.split(" ")[1]
#
#             data = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
#             current_user_id = data['user_id']
#             # Pass full token data if possible or just user_id.
#             # To support admin check, we might want to pass role info too.
#             # But the signature is f(current_user_id, ...).
#             # Let's keep it simple and fetch user role inside the route if needed, OR
#             # modify this to pass a user object/dict.
#             # For minimal impact, let's just stick to user_id and fetch role in the route.
#         except Exception as e:
#             return jsonify({'status': 1, 'msg': 'Token is invalid!', 'error': str(e)}), 401
#
#         return f(current_user_id, *args, **kwargs)
#
#     return decorated

#duoduomsk
#添加了实时监测会话是否有效的功能，这段要配合前端修改代码front/src/api/index.ts的handleNetworkError方法
#不过由于原本项目的接口原因导致现在目前只能按f5刷新，当密码或用户状态更改时才能自动退出账号登录
from functools import wraps
from flask import request, jsonify, current_app
import jwt
# ⚠️ 新增导入：为了查数据库
from backend.database import engine
from sqlalchemy import text


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            token = request.headers['Authorization']

        if not token:
            return jsonify({'status': 1, 'msg': 'Token is missing!'}), 401

        try:
            if token.startswith('Bearer '):
                token = token.split(" ")[1]

            data = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=["HS256"])
            current_user_id = data['user_id']

            # ⚠️ 核心修复：比对数据库，验证密码和角色是否被管理员修改
            with engine.connect() as conn:
                user = conn.execute(
                    text("SELECT password, role_id FROM users WHERE id=:id"),
                    {"id": current_user_id}
                ).mappings().fetchone()

                if not user:
                    return jsonify({'status': 1, 'msg': '用户不存在!'}), 401

                # 1. 比对密码指纹（如果管理员改了用户密码，Token里的旧哈希和数据库新哈希对不上）
                if data.get('pwd_hash') != user['password']:
                    return jsonify({'status': 1, 'msg': '密码已被修改，请重新登录!'}), 401

                # 2. 比对角色ID（如果管理员改了用户状态，Token里的旧角色和数据库新角色对不上）
                if data.get('role_id') != user['role_id']:
                    return jsonify({'status': 1, 'msg': '账号权限已被修改，请重新登录!'}), 401

        except Exception as e:
            return jsonify({'status': 1, 'msg': 'Token is invalid!', 'error': str(e)}), 401

        return f(current_user_id, *args, **kwargs)

    return decorated
