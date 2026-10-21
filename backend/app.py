import os

from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from backend.routes.auth import auth_bp
from backend.routes.ai import ai_bp
from backend.routes.article import article_bp
from backend.routes.upload import upload_bp
from backend.routes.role import role_bp

from backend.config import SECRET_KEY
from backend.limiter import limiter
from backend.cache import get_redis

app = Flask(__name__)

def parse_env_list(name, defaults):
    value = os.environ.get(name, "")
    items = [item.strip().rstrip("/") for item in value.split(",") if item.strip()]
    return items or defaults


ALLOWED_ORIGINS = parse_env_list("CORS_ORIGINS", [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "https://ai-vue3-python-flask-blog.vercel.app",
    "https://ai-vue3-python-flask-blog-copy.vercel.app",
    "https://lindablog.xyz",
    "https://www.lindablog.xyz",
]) + parse_env_list("CORS_ORIGIN_REGEXES", [
    r"^https://ai-vue3-python-flask-blog-[a-z0-9-]+-linsk27s-projects\.vercel\.app$",
    r"^https://[a-z0-9-]+\.vercel\.app$",
    r"^https://[a-z0-9-]+-[a-z0-9-]+-linsk27s-projects\.vercel\.app$",
])

CORS(
    app,
    resources={r"/*": {"origins": ALLOWED_ORIGINS}},
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "APP-ID"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"]
)

# 信任 nginx 转发的一跳真实 IP（否则限流看到的 remote_addr 全是容器 IP）
app.wsgi_app = ProxyFix(
    app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0
)


app.config['SECRET_KEY'] = SECRET_KEY

# 初始化限流（与 app 解耦，避免循环导入）
limiter.init_app(app)

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        return "", 204

@app.route('/')
def home():
    return 'Flask started successfully.'

@app.route('/healthz')
def healthz():
    """健康检查：DB 与 Redis 连通性。DB 挂才 503，Redis 挂仅告警。"""
    checks = {"db": "unknown", "redis": "unknown"}

    try:
        from backend.database import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {e}"

    try:
        r = get_redis()
        if r.ping():
            checks["redis"] = "ok"
        else:
            checks["redis"] = "no-pong"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    # Redis 是可降级依赖，只有 DB 挂了才视为不健康
    healthy = checks["db"] == "ok"
    return (
        jsonify({"status": 0 if healthy else 1, "checks": checks}),
        200 if healthy else 503,
    )

# 注册蓝图
app.register_blueprint(auth_bp)
app.register_blueprint(ai_bp)
app.register_blueprint(article_bp)
app.register_blueprint(upload_bp)
app.register_blueprint(role_bp)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
