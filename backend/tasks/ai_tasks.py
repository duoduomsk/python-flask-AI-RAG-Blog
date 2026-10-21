# backend/tasks/ai_tasks.py
from backend.celery_app import celery_app
from sqlalchemy import text
from backend.database import engine
from openai import OpenAI
import json
import re
import os


def get_active_ai_config():
    # 提取之前的获取配置逻辑
    active_config = None
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM ai_configs WHERE is_active = 1 LIMIT 1")).mappings().fetchone()
            if row:
                active_config = dict(row)
    except Exception:
        pass
    if not active_config:
        active_config = {
            'provider': 'volcano',
            'api_key': os.environ.get('ARK_API_KEY', ''),
            'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
            'model': 'ep-20260125005850-g97x2',
            'system_prompt': 'You are a helpful assistant.'
        }
    return active_config


@celery_app.task(bind=True, max_retries=3)
def generate_article_task(self, topic):
    """后台生成文章任务"""
    print(f"【Celery 开始执行】生成文章: {topic}")
    active_config = get_active_ai_config()
    api_key = active_config.get('api_key')
    base_url = active_config.get('base_url')
    model_name = active_config.get('model')

    system_prompt = """
    你是一个专业的文章创作者。请根据用户提供的主题或概要，创作一篇完整的文章。
    必须返回纯 JSON 格式（不要包含 ```json 等标记）：
    {
        "title": "文章标题",
        "content": "文章正文（支持 Markdown）",
        "summary": "文章摘要",
        "category": "分类(frontend/backend/ai/other)",
        "tags": ["标签1", "标签2"]
    }
    """
    user_prompt = f"请直接输出符合格式的 JSON 字符串，文章主题关于：{topic}"

    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            stream=False,
            temperature=0.7,
            max_tokens=2500
        )
        content = response.choices[0].message.content

        # 解析 JSON
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            content = json_match.group(0)
        article_data = json.loads(content.strip(), strict=False)

        print(f"【Celery 执行成功】文章生成完毕")
        return article_data  # 这个返回结果会存入 Redis 的 result backend

    except Exception as e:
        print(f"【Celery 执行失败】{str(e)}，准备重试...")
        # 如果失败，等待 10 秒后重试（最多 3 次）
        raise self.retry(exc=e, countdown=10)