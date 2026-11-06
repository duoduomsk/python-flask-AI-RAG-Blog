# backend/rag/query_analyzer.py
import json
import logging
from openai import OpenAI
from backend.cache import get_ai_context
from backend.utils.ai_config import get_active_ai_config

logger = logging.getLogger(__name__)


def fast_rule_analysis(message: str) -> dict | None:
    """规则快路径：极短问候、明显报错，直接返回，省一次大模型调用"""
    msg = message.strip().lower()

    # 1. 纯问候
    greetings = ["你好", "hello", "hi", "你是谁", "在吗", "谢谢", "感谢"]
    if msg in greetings or len(msg) < 3:
        return {
            "intent": "chitchat", "sentiment": "neutral", "emotion": "neutral",
            "urgency": "low", "need_rag": False, "rewritten_query": message
        }

    # 2. 明显的排错
    error_keys = ["报错", "error", "bug", "异常", "失败", "连不上", "500", "404"]
    if any(k in msg for k in error_keys):
        return {
            "intent": "troubleshooting", "sentiment": "negative", "emotion": "frustrated",
            "urgency": "medium", "need_rag": True, "rewritten_query": message
        }

    return None


def analyze_query(user_id: int, message: str) -> dict:
    """
    返回分析结果字典：intent, sentiment, emotion, urgency, need_rag, rewritten_query
    """
    # 1. 尝试规则快筛
    rule_result = fast_rule_analysis(message)
    if rule_result:
        return rule_result

    # 2. LLM 慢路径
    active_config = get_active_ai_config()
    api_key = active_config.get('api_key')
    base_url = active_config.get('base_url')
    model_name = active_config.get('model')

    if not api_key:
        # 降级默认值
        return {"intent": "knowledge_qa", "sentiment": "neutral", "emotion": "neutral", "urgency": "low",
                "need_rag": True, "rewritten_query": message}

    # 获取最近两轮历史用于指代消解
    history = get_ai_context(user_id)[-4:]
    history_text = "\n".join([f"{m['role']}: {m['content']}" for m in history]) if history else "无历史"

    prompt = f"""你是一个查询分析器。请分析下面用户的输入，并结合历史记录。
历史记录：
{history_text}

用户输入：{message}

请返回严格的 JSON 格式（不要包含 ```json 等标记），字段如下：
{{
  "intent": "knowledge_qa / troubleshooting / article_recommend / writing_assist / summarize / navigation / out_of_scope",
  "sentiment": "positive / neutral / negative",
  "emotion": "calm / confused / frustrated / angry / excited",
  "urgency": "low / medium / high",
  "need_rag": true/false (是否需要检索知识库，闲聊和超纲问题为 false),
  "rewritten_query": "结合历史记录，补全代词指代后，用于搜索的独立完整句子"
}}
"""

    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        response = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            temperature=0.1,
            max_tokens=200,
        )
        content = response.choices[0].message.content.strip()

        # 解析 JSON
        import re
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            content = json_match.group(0)

        result = json.loads(content)
        logger.info(f"[Query Analyzer] 分析完成: {result}")
        return result

    except Exception as e:
        logger.warning(f"LLM 意图分析失败，降级为默认: {e}")
        return {"intent": "knowledge_qa", "sentiment": "neutral", "emotion": "neutral", "urgency": "low",
                "need_rag": True, "rewritten_query": message}