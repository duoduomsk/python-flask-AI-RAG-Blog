# backend/utils/ai_config.py
import os
from sqlalchemy import text
from backend.database import engine

def get_active_ai_config():
    """读取当前启用的 AI 配置，缺省回落到环境变量。"""
    active_config = None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM ai_configs WHERE is_active = 1 LIMIT 1")
            ).mappings().fetchone()
            if row:
                active_config = dict(row)
    except Exception as e:
        print(f"Error fetching active config: {e}")

    if not active_config:
        active_config = {
            'provider': 'volcano',
            'api_key': os.environ.get('ARK_API_KEY', ''),
            'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
            'model': 'ep-20260125005850-g97x2',
            'system_prompt': 'You are a helpful assistant.'
        }
    return active_config