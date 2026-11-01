# backend_fastapi/cache.py
"""
统一缓存层。

设计：
  - 优先使用 Redis（生产环境）
  - Redis 不可用时自动回退到进程内内存（开发零配置）
  - Key 命名：blog:{域}:{实体}:{标识}:v{版本}

内容分三块：
  1. 通用 KV 缓存        （cache_get / cache_set / cache_delete / cache_delete_pattern）
  2. 浏览量计数器         （incr_view / get_view_delta / scan_view_counters / clear_view_counters）
  3. AI 会话上下文         （get_ai_context / append_ai_message / clear_ai_context）

⚠️ 内存回退在多 worker 下不共享，生产环境必须配 REDIS_URL。
"""
import json
import threading
from typing import Any, Dict, List, Optional

try:
    import redis
    _REDIS_LIB_AVAILABLE = True
except ImportError:
    _REDIS_LIB_AVAILABLE = False

from config import (
    CACHE_TTL_DETAIL,
    CACHE_TTL_LIST,
    REDIS_CONTEXT_TTL,
    REDIS_URL,
)


# ==================================================================
# 内存回退：KV 和 List 分开存（防止类型冲突）
# ==================================================================
_memory_store: Dict[str, str] = {}                 # KV：存字符串
_memory_list_store: Dict[str, List[Dict]] = {}     # List：存 AI 上下文
_memory_lock = threading.Lock()

_redis_client = None
_redis_checked = False


def _get_redis():
    """懒加载 Redis 客户端；首次失败后不再重试，直接走内存。"""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True

    if not _REDIS_LIB_AVAILABLE:
        print("[cache] redis 库未安装，使用内存缓存")
        return None

    try:
        client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
        _redis_client = client
        print(f"[cache] ✅ 已连接 Redis: {REDIS_URL}")
    except Exception as e:
        print(f"[cache] ⚠️  Redis 连接失败（{e}），使用内存缓存")
        _redis_client = None

    return _redis_client


def get_redis():
    """
    暴露给 auth.py 等模块复用。
    登录失败计数直接用 redis.incr / redis.get 原生命令，不走封装。
    """
    return _get_redis()


def _backend_name() -> str:
    return "redis" if _get_redis() else "memory"


# ==================================================================
# Key 命名规范
# ==================================================================
def make_key(domain: str, entity: str, ident: str, version: int = 1) -> str:
    """
    统一 Key 格式：blog:{域}:{实体}:{标识}:v{版本}
    例：blog:ai:context:user_42:v1
         blog:article:list:category=backend:v1
         blog:article:detail:123:v1
    """
    return f"blog:{domain}:{entity}:{ident}:v{version}"


# ==================================================================
# 通用 KV 操作
# ==================================================================
def cache_get(key: str) -> Optional[str]:
    r = _get_redis()
    if r:
        try:
            return r.get(key)
        except Exception as e:
            print(f"[cache] redis GET 失败: {e}")
    with _memory_lock:
        return _memory_store.get(key)


def cache_set(key: str, value: str, ttl: int = 60) -> None:
    r = _get_redis()
    if r:
        try:
            r.setex(key, ttl, value)
            return
        except Exception as e:
            print(f"[cache] redis SET 失败: {e}")
    with _memory_lock:
        _memory_store[key] = value
    # 内存模式不实现 TTL（简单起见）


def cache_delete(key: str) -> None:
    r = _get_redis()
    if r:
        try:
            r.delete(key)
            return
        except Exception as e:
            print(f"[cache] redis DEL 失败: {e}")
    with _memory_lock:
        _memory_store.pop(key, None)


def cache_delete_pattern(pattern: str) -> int:
    """
    按 glob pattern 删除 key（如 blog:article:list:*）。
    生产环境慎用 SCAN（会遍历），只用于小规模场景。
    """
    r = _get_redis()
    if r:
        try:
            keys = list(r.scan_iter(match=pattern, count=200))
            if keys:
                return r.delete(*keys)
            return 0
        except Exception as e:
            print(f"[cache] redis SCAN/DEL 失败: {e}")

    # 内存模式：简单前缀匹配
    with _memory_lock:
        prefix = pattern.rstrip("*")
        targets = [k for k in _memory_store if k.startswith(prefix)]
        for k in targets:
            _memory_store.pop(k, None)
        return len(targets)


# ==================================================================
# 【新增】前缀失效（兼容 Flask 调用风格）
# ==================================================================
def invalidate_prefix(prefix: str) -> int:
    """
    与 Flask 版同名，自动补 *。
    调用方写 invalidate_prefix("blog:article:list:") 即可。
    """
    return cache_delete_pattern(f"{prefix}*")


# ==================================================================
# JSON 辅助
# ==================================================================
def cache_get_json(key: str) -> Any:
    raw = cache_get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def cache_set_json(key: str, value: Any, ttl: int = 60) -> None:
    cache_set(key, json.dumps(value, ensure_ascii=False), ttl)


# ==================================================================
# Key 构造工具
# ==================================================================
def list_cache_key(resource: str, **params) -> str:
    """
    生成列表缓存 key，参数按字段名排序保证幂等。
    例：list_cache_key("article", category="backend", page=1)
        → blog:article:list:category=backend&page=1:v1
    """
    if not params:
        suffix = "all"
    else:
        suffix = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return make_key(resource, "list", suffix)


def detail_cache_key(resource: str, ident) -> str:
    return make_key(resource, "detail", str(ident))


# ==================================================================
# 【新增 A】浏览量计数器
#   设计：Redis 里只存「未回写的增量」
#   用户访问 → INCR → 返回时 views = DB值 + 增量
#   Celery beat 每 2 分钟 → 批量刷回 DB → 清空这些 key
# ==================================================================
def _view_key(article_id: int) -> str:
    return f"blog:article:view:{article_id}"


def incr_view(article_id: int) -> int:
    """浏览量 +1，Redis INCR 原子操作，比 SQL UPDATE 快百倍"""
    r = _get_redis()
    if r:
        try:
            return r.incr(_view_key(article_id))
        except Exception as e:
            print(f"[cache] incr_view 失败: {e}")

    with _memory_lock:
        k = _view_key(article_id)
        _memory_store[k] = str(int(_memory_store.get(k, 0)) + 1)
        return int(_memory_store[k])


def get_view_delta(article_id: int) -> int:
    """读取未回写的增量，返回给前端时叠加到 DB 值上"""
    r = _get_redis()
    if r:
        try:
            v = r.get(_view_key(article_id))
            return int(v) if v else 0
        except Exception as e:
            print(f"[cache] get_view_delta 失败: {e}")

    with _memory_lock:
        return int(_memory_store.get(_view_key(article_id), 0))


def scan_view_counters() -> Dict[int, int]:
    """
    Celery beat 用：扫描所有还有未回写增量的文章。
    返回 {article_id(int): delta(int)}
    """
    result: Dict[int, int] = {}
    r = _get_redis()
    if r:
        try:
            for key in r.scan_iter(match="blog:article:view:*", count=200):
                val = r.get(key)
                if not val:
                    continue
                try:
                    aid = int(key.rsplit(":", 1)[-1])
                    result[aid] = int(val)
                except ValueError:
                    continue
            return result
        except Exception as e:
            print(f"[cache] scan_view_counters 失败: {e}")

    with _memory_lock:
        for k, v in _memory_store.items():
            if k.startswith("blog:article:view:"):
                try:
                    aid = int(k.rsplit(":", 1)[-1])
                    result[aid] = int(v)
                except ValueError:
                    continue
    return result


def clear_view_counters(article_ids) -> None:
    """Celery 刷回 DB 后，清空这些文章的增量计数"""
    r = _get_redis()
    if r:
        try:
            keys = [_view_key(aid) for aid in article_ids]
            if keys:
                r.delete(*keys)
            return
        except Exception as e:
            print(f"[cache] clear_view_counters 失败: {e}")

    with _memory_lock:
        for aid in article_ids:
            _memory_store.pop(_view_key(aid), None)


# ==================================================================
# 【新增 B】AI 会话上下文（Redis List 实现）
#   List 里只存 user/assistant 消息，system 由调用方动态拼在最前面
# ==================================================================
AI_CONTEXT_MAX_LEN = 18  # 保留最近 18 条（不含 system）


def _ctx_key(user_id) -> str:
    return make_key("ai", "context", f"user_{user_id}")


def get_ai_context(user_id) -> List[Dict]:
    """返回对话历史列表（不含 system），不存在时返回空列表"""
    key = _ctx_key(user_id)
    r = _get_redis()
    if r:
        try:
            raws = r.lrange(key, 0, -1)
            out: List[Dict] = []
            for raw in raws:
                try:
                    out.append(json.loads(raw))
                except Exception:
                    continue
            return out
        except Exception as e:
            print(f"[cache] get_ai_context 失败: {e}")

    with _memory_lock:
        return list(_memory_list_store.get(key, []))


def append_ai_message(user_id, message: Dict) -> None:
    """
    追加一条消息，自动裁剪到最近 AI_CONTEXT_MAX_LEN 条，
    并刷新 TTL（防止长期不聊被误删）。
    """
    key = _ctx_key(user_id)
    payload = json.dumps(message, ensure_ascii=False)

    r = _get_redis()
    if r:
        try:
            r.rpush(key, payload)
            r.ltrim(key, -AI_CONTEXT_MAX_LEN, -1)   # 保留最近 N 条
            r.expire(key, REDIS_CONTEXT_TTL)
            return
        except Exception as e:
            print(f"[cache] append_ai_message 失败: {e}")

    with _memory_lock:
        lst = _memory_list_store.get(key, [])
        lst.append(message)
        if len(lst) > AI_CONTEXT_MAX_LEN:
            lst = lst[-AI_CONTEXT_MAX_LEN:]
        _memory_list_store[key] = lst


def clear_ai_context(user_id) -> None:
    key = _ctx_key(user_id)
    r = _get_redis()
    if r:
        try:
            r.delete(key)
            return
        except Exception as e:
            print(f"[cache] clear_ai_context 失败: {e}")

    with _memory_lock:
        _memory_list_store.pop(key, None)


# ==================================================================
# 健康检查 / 调试
# ==================================================================
def cache_stats() -> dict:
    return {
        "backend": _backend_name(),
        "redis_url": REDIS_URL if _redis_client else None,
        "memory_kv_keys": len(_memory_store),
        "memory_list_keys": len(_memory_list_store),
    }