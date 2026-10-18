# duoduomsk
"""
Redis 缓存模块：连接池单例、Key 工厂、缓存装饰器、前缀失效、计数器、AI 上下文。

设计原则：
- fail-open：Redis 异常一律吞掉并打 warning，业务侧不受影响。
- 只缓存幂等 GET 接口，只缓存 status=0 的 JSON 成功响应。
- Key 命名规范：blog:{域}:{实体}:{标识}[:v{版本}]
"""

import functools
import json
import logging
from datetime import date, datetime
from decimal import Decimal

import redis
from flask import jsonify, request

from backend.config import REDIS_URL, REDIS_CONTEXT_TTL

logger = logging.getLogger(__name__)

# ---------- 连接池单例 ----------
_pool = redis.ConnectionPool.from_url(
    REDIS_URL,
    decode_responses=True,
    health_check_interval=30,
    max_connections=50,
    socket_timeout=2,
    socket_connect_timeout=2,
)
_client = redis.Redis(connection_pool=_pool)


def get_redis():
    """返回共享的 Redis 客户端（供健康检查 / 计数器使用）。"""
    return _client

# ---------- Key 工厂 ----------
def make_key(*parts) -> str:
    """
    拼接规范 Key：make_key("article", "list", "v1", category) -> "blog:article:list:v1:..."。
    空字符串与 None 会被过滤。
    """
    cleaned = [str(p) for p in parts if p is not None and p != ""]
    return "blog:" + ":".join(cleaned)

# ---------- 序列化 ----------
def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", errors="replace")
    raise TypeError(f"not JSON serializable: {type(o).__name__}")

def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)

def _load(s: str):
    return json.loads(s)

# ---------- 基础操作（fail-open） ----------
def cache_get(key: str):
    try:
        return _client.get(key)
    except Exception as e:
        logger.warning("redis GET failed key=%s err=%s", key, e)
        return None


def cache_set(key: str, value: str, ttl: int):
    try:
        _client.setex(key, ttl, value)
    except Exception as e:
        logger.warning("redis SET failed key=%s err=%s", key, e)


def cache_delete(*keys):
    if not keys:
        return
    try:
        _client.delete(*keys)
    except Exception as e:
        logger.warning("redis DEL failed err=%s", e)


def invalidate_prefix(prefix: str):
    """按前缀删除，使用 SCAN 迭代，严禁 KEYS（会阻塞单线程 Redis）。"""
    try:
        cursor = 0
        match = f"{prefix}*"
        while True:
            cursor, keys = _client.scan(cursor=cursor, match=match, count=100)
            if keys:
                _client.delete(*keys)
            if cursor == 0:
                break
    except Exception as e:
        logger.warning("redis invalidate_prefix failed prefix=%s err=%s", prefix, e)

# ---------- 缓存装饰器 ----------
def cached(ttl: int, key_builder):
    """
    幂等 GET 接口缓存装饰器。

    :param ttl: 缓存秒数
    :param key_builder: 接收 flask.request，返回完整缓存 Key（含命名空间）
                        示例：lambda r: make_key("article", "list", "v1", r.args.get("category",""))

    只缓存 status=0 的成功响应；异常/失败响应不写缓存。
    命中缓存时直接返回 {"status":0, ...} 的 jsonify 响应。
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                key = key_builder(request)
            except Exception as e:
                logger.warning("cached key_builder failed err=%s", e)
                return fn(*args, **kwargs)

            cached_raw = cache_get(key)
            if cached_raw is not None:
                try:
                    payload = _load(cached_raw)
                    return jsonify(payload["body"]), payload["status"]
                except Exception as e:
                    logger.warning("cached decode failed key=%s err=%s", key, e)

            resp = fn(*args, **kwargs)

            # 解包 (response, status) 或裸 response
            status = 200
            resp_obj = resp
            if isinstance(resp, tuple):
                resp_obj = resp[0]
                status = resp[1] if len(resp) > 1 else 200

            # 只缓存 2xx 成功且 body.status == 0
            if 200 <= status < 300 and hasattr(resp_obj, "get_json"):
                try:
                    body = resp_obj.get_json()
                    if isinstance(body, dict) and body.get("status") == 0:
                        cache_set(key, _dump({"body": body, "status": status}), ttl)
                except Exception as e:
                    logger.warning("cached write skipped key=%s err=%s", key, e)

            return resp
        return wrapper
    return decorator

# ============================================================
# 浏览量计数器（Redis INCR，阶段 2 由 Celery beat 回写 DB）
# ============================================================
def incr_view(article_id: int):
    """文章浏览量 +1（仅在 Redis 内累积）。"""
    key = make_key("views", article_id)
    try:
        _client.incr(key)
    except Exception as e:
        logger.warning("incr_view failed aid=%s err=%s", article_id, e)


def get_view_delta(article_id: int) -> int:
    """读取尚未回写 DB 的浏览量增量。"""
    key = make_key("views", article_id)
    try:
        val = _client.get(key)
        return int(val) if val else 0
    except Exception:
        return 0


def scan_view_counters() -> dict:
    """
    扫描所有待回写的浏览量 Key，返回 {article_id: delta}。
    供阶段 2 的 Celery beat flush_views 任务调用。
    """
    result = {}
    try:
        cursor = 0
        while True:
            cursor, keys = _client.scan(cursor=cursor, match="blog:views:*", count=200)
            for k in keys:
                try:
                    aid = int(k.rsplit(":", 1)[1])
                    val = _client.get(k)
                    if val:
                        result[aid] = result.get(aid, 0) + int(val)
                except Exception as e:
                    logger.warning("scan_view_counters parse err key=%s err=%s", k, e)
            if cursor == 0:
                break
    except Exception as e:
        logger.warning("scan_view_counters failed err=%s", e)
    return result


def clear_view_counters(article_ids):
    """回写成功后清空对应计数器（先回写后删除，删失败最多重复累加一次）。"""
    if not article_ids:
        return
    keys = [make_key("views", aid) for aid in article_ids]
    cache_delete(*keys)

# ============================================================
# AI 会话上下文（Redis List，替代进程内字典）
# ============================================================
AI_CONTEXT_MAX_LEN = 18   # 不含 system，大约 9 轮对话


def get_ai_context(user_id) -> list:
    """读取会话历史（不含 system），最多 AI_CONTEXT_MAX_LEN 条。"""
    key = make_key("ai", "ctx", user_id)
    try:
        items = _client.lrange(key, 0, -1)
        return [_load(i) for i in items][-AI_CONTEXT_MAX_LEN:]
    except Exception as e:
        logger.warning("get_ai_context failed uid=%s err=%s", user_id, e)
        return []


def append_ai_message(user_id, message: dict):
    """追加一条消息并 LTRIM 裁剪到最近 AI_CONTEXT_MAX_LEN 条。"""
    key = make_key("ai", "ctx", user_id)
    try:
        pipe = _client.pipeline()
        pipe.rpush(key, _dump(message))
        pipe.ltrim(key, -AI_CONTEXT_MAX_LEN, -1)
        pipe.expire(key, REDIS_CONTEXT_TTL)
        pipe.execute()
    except Exception as e:
        logger.warning("append_ai_message failed uid=%s err=%s", user_id, e)


def clear_ai_context(user_id):
    cache_delete(make_key("ai", "ctx", user_id))