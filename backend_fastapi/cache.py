# backend_fastapi/cache.py
"""
统一缓存层。

设计：
  - 优先使用 Redis（生产环境）
  - Redis 不可用时，自动回退到进程内内存字典（开发环境零配置）
  - Key 命名规范（temp3.txt 1.4）：blog:{域}:{实体}:{标识}:v{版本}
  - 所有涉及上下文的操作使用同一个 TTL：REDIS_CONTEXT_TTL

⚠️ 内存回退在多 worker 下不共享（和原 Flask 的 context_store 一样）。
   生产环境必须配置 REDIS_URL 才能获得跨 worker 一致的上下文。
"""
import json
import threading
from typing import Any, Dict, List, Optional

try:
    import redis  # noqa: F401
    _REDIS_LIB_AVAILABLE = True
except ImportError:
    _REDIS_LIB_AVAILABLE = False

from config import (
    CACHE_TTL_DETAIL,
    CACHE_TTL_LIST,
    REDIS_CONTEXT_TTL,
    REDIS_URL,
)


# ------------------------------------------------------------------
# 内存回退（Redis 不可用或未配置时使用）
# ------------------------------------------------------------------
_memory_store: Dict[str, Any] = {}
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


def _backend_name() -> str:
    return "redis" if _get_redis() else "memory"


# ------------------------------------------------------------------
# Key 命名规范
# ------------------------------------------------------------------
def make_key(domain: str, entity: str, ident: str, version: int = 1) -> str:
    """
    统一 Key 格式：blog:{域}:{实体}:{标识}:v{版本}
    例：
      blog:ai:context:user_42:v1
      blog:article:list:v1
      blog:article:detail:123:v1
    """
    return f"blog:{domain}:{entity}:{ident}:v{version}"


# ------------------------------------------------------------------
# 通用 KV 操作
# ------------------------------------------------------------------
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
    # 内存模式不实现 TTL 过期（简单起见）


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
    """按前缀删除（生产环境谨慎使用；只用于小规模场景）。"""
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
        targets = [k for k in _memory_store if k.startswith(pattern.rstrip("*"))]
        for k in targets:
            _memory_store.pop(k, None)
        return len(targets)


# ------------------------------------------------------------------
# JSON 辅助
# ------------------------------------------------------------------
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


# ------------------------------------------------------------------
# AI 上下文专用 API（替换原 ai.py 里的 context_store）
# ------------------------------------------------------------------
def get_context(user_id: str) -> Optional[List[Dict]]:
    """返回消息列表，不存在时返回 None（区别于空列表）。"""
    return cache_get_json(make_key("ai", "context", user_id))


def save_context(user_id: str, messages: List[Dict]) -> None:
    cache_set_json(
        make_key("ai", "context", user_id),
        messages,
        ttl=REDIS_CONTEXT_TTL,
    )


def clear_context(user_id: str) -> None:
    cache_delete(make_key("ai", "context", user_id))


# ------------------------------------------------------------------
# 通用列表缓存辅助（temp3.txt 1.2 预留）
# ------------------------------------------------------------------
def list_cache_key(resource: str, **params) -> str:
    """blog:article:list:category=backend&page=1:v1 —— 参数顺序稳定"""
    if not params:
        suffix = "all"
    else:
        suffix = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return make_key(resource, "list", suffix)


def detail_cache_key(resource: str, ident) -> str:
    return make_key(resource, "detail", str(ident))


# ------------------------------------------------------------------
# 健康检查 / 调试
# ------------------------------------------------------------------
def cache_stats() -> dict:
    """用于 /health 或调试面板"""
    return {
        "backend": _backend_name(),
        "redis_url": REDIS_URL if _redis_client else None,
        "memory_keys": len(_memory_store),
    }