# backend_fastapi/services/ai_provider.py
"""
AI Provider 抽象层。

把原 backend/routes/ai.py 中重复 3 次的逻辑抽到一处：
    1. 从 ai_configs 表查 is_active=1 的配置
    2. 用配置构建 OpenAI 客户端
    3. 同步 / 流式调用封装

后续 Phase 4 扩展多 Provider 时，只需在此文件增加分支即可，
路由层无需改动。
"""
import os
from typing import Dict, List, Optional

from openai import OpenAI
from sqlalchemy import text

from database import engine


# ------------------------------------------------------------------
# 支持的模型清单（供前端 /api/ai/models 使用）
# ------------------------------------------------------------------
SUPPORTED_MODELS: Dict[str, dict] = {
    "qwen": {
        "name": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-max"],
    },
    "volcano": {
        "name": "火山方舟",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": [
            "ep-20260125005850-g97x2",
            "doubao-seed-1-6-251015",
        ],
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-chat"],
    },
}


# ------------------------------------------------------------------
# 获取当前启用的 AI 配置
# ------------------------------------------------------------------
def get_active_config() -> dict:
    """
    从 ai_configs 表查询 is_active=1 的配置。
    查询失败或表中无启用配置时，返回默认（火山方舟）配置。
    与原 backend/routes/ai.py 中 get_active_ai_config() 逻辑一致。
    """
    active_config: Optional[dict] = None

    try:
        with engine.connect() as conn:
            result = (
                conn.execute(
                    text("SELECT * FROM ai_configs WHERE is_active = 1 LIMIT 1")
                )
                .mappings()
                .fetchone()
            )
            if result:
                active_config = dict(result)
    except Exception as e:
        print(f"[ai_provider] Error fetching active config: {e}")

    if not active_config:
        active_config = {
            "provider": "volcano",
            "api_key": os.environ.get("ARK_API_KEY", ""),
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "model": "ep-20260125005850-g97x2",
            "system_prompt": "You are a helpful assistant.",
        }

    return active_config


# ------------------------------------------------------------------
# 构建 OpenAI 客户端
# ------------------------------------------------------------------
def get_ai_client(config: Optional[dict] = None) -> OpenAI:
    """根据配置构建 OpenAI 客户端；未传 config 时自动查激活配置。"""
    if not config:
        config = get_active_config()
    return OpenAI(base_url=config["base_url"], api_key=config["api_key"])


# ------------------------------------------------------------------
# 同步调用封装
# ------------------------------------------------------------------
def chat_completion(
    messages: List[dict],
    config: Optional[dict] = None,
    **kwargs,
):
    """同步调用（stream=False）"""
    client = get_ai_client(config)
    return client.chat.completions.create(
        messages=messages, stream=False, **kwargs
    )


# ------------------------------------------------------------------
# 流式调用封装
# ------------------------------------------------------------------
def chat_completion_stream(
    messages: List[dict],
    config: Optional[dict] = None,
    **kwargs,
):
    """流式调用（stream=True），返回同步迭代器"""
    client = get_ai_client(config)
    return client.chat.completions.create(
        messages=messages, stream=True, **kwargs
    )