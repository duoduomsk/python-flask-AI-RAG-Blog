# routes/auth.py
from flask import Blueprint, current_app, request, jsonify
# 数据库
from sqlalchemy import text
from backend.database import engine
# 加密库
from werkzeug.security import generate_password_hash, check_password_hash
# jwt
import datetime
import jwt
from backend.middleware import token_required

# 导入限流器与 Redis
from backend.limiter import limiter
from backend.cache import get_redis
from flask_limiter.util import get_remote_address

auth_bp = Blueprint('auth', __name__)


# ------------------------------------------------------------
# 登录限流键函数：按「IP+用户名」双维度限流
# 既能防单IP爆破，又能防分布式多IP打同一个账号
# ------------------------------------------------------------
def get_login_key():
    # silent=True 避免请求体不是JSON时报错
    data = request.get_json(silent=True) or {}
    username = data.get('username', '')
    if not username:
        return get_remote_address()
    return f"{get_remote_address()}:{username}"


# ------------------------------------------------------------
# 注册接口：3 次/小时/IP
# ------------------------------------------------------------
@auth_bp.route('/api/register', methods=['POST'])
@limiter.limit("3/hour")  # 限流：3次/小时/IP
def register():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    avatar = data.get('avatar')
    email = data.get('email')

    # 简单校验
    if not username or not password:
        return {"status": 1, "msg": "用户名和密码不能为空"}, 400

    # ⚠️ 核心修复：拒绝弱密码/占位符作为真实密码
    banned_passwords = ['......', '******', 'password']
    if password.strip() in banned_passwords:
        return {"status": 1, "msg": "密码过于简单或包含非法字符，请重新设置"}, 400

    # 获取角色，默认为 user
    role_id = data.get('role_id')  # New logic, if passed

    with engine.connect() as conn:
        # 如果没有指定role_id，默认使用 'user' 角色的ID
        if not role_id:
            role_id = conn.execute(text("SELECT id FROM roles WHERE name='user'")).scalar()

        # 判断用户名是否存在
        existing_user = conn.execute(
            text("SELECT * FROM users WHERE username=:username"), {"username": username}
        ).fetchone()

        if existing_user:
            return {"status": 1, "msg": "用户名已存在", "data": {
                'success': False,
            }}, 400

        # 密码加密
        hashed_password = generate_password_hash(password)

        # 获取角色名称用于兼容旧字段
        role_name = conn.execute(text("SELECT name FROM roles WHERE id=:id"), {"id": role_id}).scalar()

        conn.execute(
            text(
                "INSERT INTO users (username, password, avatar, role, role_id, email) VALUES (:username, :password, :avatar, :role, :role_id, :email)"),
            {"username": username, "password": hashed_password, "avatar": avatar or '', "role": role_name,
             "role_id": role_id, "email": email}
        )
        conn.commit()

    return {'status': 0, "msg": "注册成功", "data": {
        'success': True,
    }}


# ------------------------------------------------------------
# 获取用户信息
# ------------------------------------------------------------
@auth_bp.route('/api/user/info', methods=['GET'])
@token_required
def get_user_info(current_user_id):
    with engine.connect() as conn:
        user = conn.execute(
            text("SELECT id, username, avatar, role, role_id, email, created_at FROM users WHERE id=:id"),
            {"id": current_user_id}
        ).mappings().fetchone()

    if user:
        user_data = dict(user)
        if user_data.get('created_at'):
            user_data['created_at'] = user_data['created_at'].strftime('%Y-%m-%d')

        # Avue 需要 roles 数组
        user_data['roles'] = [user_data.get('role', 'user')]

        # 权限列表，根据角色ID查询 permissions 表
        permissions = []
        if user_data.get('role_id'):
            with engine.connect() as conn:
                perms = conn.execute(text("""
                      SELECT p.code
                      FROM permissions p
                               JOIN role_permissions rp ON p.id = rp.permission_id
                      WHERE rp.role_id = :rid
                      """), {"rid": user_data['role_id']}).scalars().all()
                permissions = list(perms)

        user_data['permissions'] = permissions
        return jsonify({'status': 0, 'data': user_data})

    return jsonify({'status': 1, 'msg': '用户不存在'}), 404


# ------------------------------------------------------------
# 登录接口：挂载限流 + 失败计数器
# ------------------------------------------------------------
@auth_bp.route('/api/login', methods=['POST'])
@limiter.limit("5/minute", key_func=get_login_key)  # 限流：IP+用户名 5次/分钟
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    # 验证输入
    if not username or not password:
        return jsonify({'status': 1, 'msg': '用户名和密码不能为空'}), 400

    r = get_redis()
    fail_key = f"blog:login:fail:{username}"

    # ⚠️ 增强安全：检查失败计数器（防分布式爆破）
    try:
        fails = int(r.get(fail_key) or 0)
        if fails >= 10:
            return jsonify({'status': 1, 'msg': '该账号由于失败次数过多已被暂时锁定，请15分钟后再试'}), 429
    except Exception as e:
        # Redis 异常时 fail-open 放行，避免全站登录瘫痪
        print(f"Redis login fail check error: {e}")

    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT * FROM users WHERE username=:username "
                ), {"username": username}
            ).mappings().fetchone()

        if result and check_password_hash(result['password'], password):
            # 登录成功，清空失败计数器
            try:
                r.delete(fail_key)
            except Exception as e:
                print(f"Redis delete fail count error: {e}")

            # 获取权限列表
            permissions = []
            if result.get('role_id'):
                with engine.connect() as conn:
                    perms = conn.execute(text("""
                          SELECT p.code
                          FROM permissions p
                                   JOIN role_permissions rp ON p.id = rp.permission_id
                          WHERE rp.role_id = :rid
                          """), {"rid": result['role_id']}).scalars().all()
                    permissions = list(perms)

            # 生成JWT token
            token = jwt.encode({
                'user_id': result['id'],
                'username': result['username'],
                'role': result.get('role', 'user'),
                'role_id': result.get('role_id'),  # ⚠️ 新增：把角色ID打入令牌用于实时监测会话
                'pwd_hash': result['password'],  # ⚠️ 新增：把密码哈希打入令牌用于实时监测会话
                'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=24)
            }, current_app.config['SECRET_KEY'], algorithm='HS256')

            return jsonify({'status': 0, 'msg': '登录成功', 'data': {
                'id': result['id'],
                'username': result['username'],
                'role': result.get('role', 'user'),
                'email': result.get('email'),
                'permissions': permissions,
                'token': token,
            }})

        # 登录失败，增加失败计数（15分钟窗口，10次失败锁账号）
        try:
            r.incr(fail_key)
            r.expire(fail_key, 900)  # 900秒 = 15分钟
        except Exception as e:
            print(f"Redis incr login fail error: {e}")

        return jsonify({'status': 1, 'msg': '用户名或密码错误'}), 401
    except Exception as e:
        return jsonify({'status': 1, 'msg': '登录失败', 'error': str(e)}), 500


# ------------------------------------------------------------
# 获取用户列表
# ------------------------------------------------------------
@auth_bp.route('/api/users', methods=['GET'])
def get_users():
    try:
        username = request.args.get('username')
        email = request.args.get('email')

        with engine.connect() as conn:
            sql = "SELECT id, username, avatar, created_at, role, role_id, email FROM users WHERE 1=1"
            params = {}

            if username:
                sql += " AND username LIKE :username"
                params['username'] = f"%{username}%"

            if email:
                sql += " AND email LIKE :email"
                params['email'] = f"%{email}%"

            sql += " ORDER BY created_at DESC"

            result = conn.execute(text(sql), params).mappings().fetchall()
            users = []
            for row in result:
                user = dict(row)
                if user.get('created_at'):
                    user['created_at'] = user['created_at'].isoformat()
                users.append(user)
            return jsonify({'status': 0, 'data': users})
    except Exception as e:
        return jsonify({'status': 1, 'msg': '获取用户列表失败', 'error': str(e)}), 500


# ------------------------------------------------------------
# 更新用户信息（含权限校验）修复原有的UI回显漏洞
# ------------------------------------------------------------
@auth_bp.route('/api/users/<int:user_id>', methods=['PUT'])
@token_required
def update_user(current_user_id, user_id):
    data = request.get_json()
    username = data.get('username')
    avatar = data.get('avatar')
    password = data.get('password')
    role_id = data.get('role_id')
    email = data.get('email')

    try:
        with engine.connect() as conn:
            # 1. 获取当前操作者和目标用户的信息
            current_user = conn.execute(
                text("SELECT id, username, role_id FROM users WHERE id=:id"), {"id": current_user_id}
            ).mappings().fetchone()

            target_user = conn.execute(
                text("SELECT id, role_id FROM users WHERE id=:id"), {"id": user_id}
            ).mappings().fetchone()

            if not current_user:
                return jsonify({'status': 1, 'msg': '操作者不存在'})
            if not target_user:
                return jsonify({'status': 1, 'msg': '目标用户不存在'})

            # 2. 绑定 admin 为最高级管理员
            is_super_admin = (current_user['username'] == 'admin')
            new_role_id = int(role_id) if role_id is not None else None

            # ----- 权限拦截逻辑 开始 -----
            if current_user_id == user_id:
                # 修改自己的情况：不允许修改自己的角色（防止自我降级）
                if new_role_id is not None and new_role_id != int(target_user['role_id']):
                    return jsonify({'status': 1, 'msg': '不能修改自己的管理员状态！'})
            else:
                # 修改他人的情况：只有超级管理员(admin)才有权限修改他人
                if not is_super_admin:
                    return jsonify({'status': 1, 'msg': '权限不足！只有超级管理员(admin)才能修改其他用户信息'})
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
                if password.strip() in ['......', '******', 'password']:
                    # 如果是占位符，我们跳过，不更新密码
                    pass
                else:
                    update_fields.append("password=:password")
                    params["password"] = generate_password_hash(password)

            if new_role_id is not None:
                role_name = conn.execute(text("SELECT name FROM roles WHERE id=:id"), {"id": new_role_id}).scalar()
                if role_name:
                    update_fields.append("role=:role")
                    update_fields.append("role_id=:role_id")
                    params["role"] = role_name
                    params["role_id"] = new_role_id

            # ⚠️ 核心修复：解决“前端假成功”的问题
            if not update_fields:
                return jsonify({'status': 1, 'msg': '没有需要更新的内容，请检查是否输入了新密码或修改了角色'})

            update_sql = f"UPDATE users SET {', '.join(update_fields)} WHERE id=:id"
            conn.execute(text(update_sql), params)
            conn.commit()

            return jsonify({'status': 0, 'msg': '更新成功'})

    except Exception as e:
        print(f"========= 更新用户报错 =========")
        import traceback
        traceback.print_exc()
        print(f"================================")
        return jsonify({'status': 1, 'msg': '更新失败', 'error': str(e)})


# ------------------------------------------------------------
# 删除用户
# ------------------------------------------------------------
@auth_bp.route('/api/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    try:
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM users WHERE id=:id"), {"id": user_id})
            conn.commit()
            return jsonify({'status': 0, 'msg': '删除成功'})
    except Exception as e:
        return jsonify({'status': 1, 'msg': '删除失败', 'error': str(e)}), 500