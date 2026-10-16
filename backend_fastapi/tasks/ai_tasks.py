# backend_fastapi/tasks/ai_tasks.py
"""
AI 相关的异步任务。

预埋：当 AI_ASYNC_ENABLED=true 时，/api/ai/generate-article 会改为投递任务，
      返回 task_id，前端轮询 /api/ai/tasks/{task_id} 获取结果。
当前默认 AI_ASYNC_ENABLED=false（同步行为，与 Flask 一致）。
"""
import json
import re

from celery_app import celery_app
from services import ai_provider


_GENERATE_ARTICLE_SYSTEM_PROMPT = """
你是一个专业的文章创作者。请根据用户提供的主题或概要，创作一篇完整的文章。

### 任务要求：
1. 文章内容必须丰富、充实，字数建议在 1000 字以上（如有必要可分章节）。
2. 使用 Markdown 语法编写 `content` 字段。
3. 严禁在 JSON 字符串外部包含任何解释性文字或 Markdown 代码块标记（如 ```json）。

### 返回格式（必须是纯 JSON）：
{
    "title": "文章标题",
    "content": "文章正文内容（支持 Markdown 格式，内容要丰富充实）",
    "summary": "文章摘要（100字以内）",
    "category": "文章分类（从以下选项中选择一个：frontend, backend, database, algorithm, devops, architecture, ai, other）",
    "tags": ["标签1", "标签2", "标签3"]
}
"""


def _parse_article_json(content: str, topic: str) -> dict:
    """健壮的 JSON 提取，与同步路由内的逻辑一致。"""
    try:
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            content = json_match.group(0)
        return json.loads(content.strip(), strict=False)
    except Exception:
        return {
            "title": topic[:20],
            "content": content,
            "summary": content[:150].replace("\n", " ") + "...",
            "category": "other",
            "tags": ["AI生成"],
        }


@celery_app.task(name="ai.generate_article", bind=True, max_retries=2)
def generate_article_task(self, topic: str) -> dict:
    """
    异步生成文章。
    返回结构：{"status": 0, "msg": "生成成功", "data": {...}}
    """
    if not topic:
        return {"status": 1, "msg": "请输入文章概要或主题"}

    try:
        active_config = ai_provider.get_active_config()
        if not active_config or not active_config.get("api_key"):
            return {"status": 1, "msg": "AI服务未配置"}

        response = ai_provider.chat_completion(
            messages=[
                {"role": "system", "content": _GENERATE_ARTICLE_SYSTEM_PROMPT},
                {"role": "user", "content": f"请直接输出符合格式的 JSON 字符串，文章主题关于：{topic}"},
            ],
            config=active_config,
            model=active_config.get("model"),
            temperature=0.7,
            max_tokens=2500,
        )

        content = response.choices[0].message.content
        article_data = _parse_article_json(content, topic)
        return {"status": 0, "msg": "生成成功", "data": article_data}

    except Exception as e:
        # 失败时按指数退避重试
        raise self.retry(exc=e, countdown=5 * (self.request.retries + 1))


@celery_app.task(name="ai.generate_summary")
def generate_summary_task(content: str, max_length: int = 100) -> dict:
    """
    异步生成文章摘要（供后续「一键摘要」功能使用）。
    """
    if not content:
        return {"status": 1, "msg": "内容不能为空"}

    try:
        active_config = ai_provider.get_active_config()
        response = ai_provider.chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": f"请将下面的文章压缩成不超过 {max_length} 字的摘要，直接输出摘要文本，不要任何前缀。",
                },
                {"role": "user", "content": content[:4000]},
            ],
            config=active_config,
            model=active_config.get("model"),
            temperature=0.3,
            max_tokens=200,
        )
        summary = response.choices[0].message.content.strip()
        return {"status": 0, "msg": "生成成功", "data": {"summary": summary}}
    except Exception as e:
        return {"status": 1, "msg": f"生成失败: {str(e)}"}