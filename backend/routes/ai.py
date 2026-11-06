# backend/routes/ai.py
from flask import Blueprint, request, jsonify, Response, stream_with_context
import os
from openai import OpenAI
import time
from sqlalchemy import text
from backend.database import engine
from backend.middleware import token_required
from backend.cache import (
    get_ai_context,
    append_ai_message,
    clear_ai_context,
)
import json
import re
from backend.tasks.ai_tasks import generate_article_task
from celery.result import AsyncResult
from backend.celery_app import celery_app
from backend.rag.retriever import retrieve
from backend.config import RAG_ENABLED, EMBEDDING_API_KEY
from backend.rag.query_analyzer import analyze_query
from backend.prompts.rag_prompts import build_system_prompt, BASE_RAG_PROMPT
from backend.utils.ai_config import get_active_ai_config

ai_bp = Blueprint('ai', __name__)


# 前端参考用（不参与逻辑）
SUPPORTED_MODELS = {
    'qwen': {
        'name': '通义千问',
        'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'models': ['qwen-max']
    },
    'volcano': {
        'name': '火山方舟',
        'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
        'models': ['ep-20260125005850-g97x2', 'doubao-seed-1-6-251015']
    },
    'deepseek': {
        'name': 'DeepSeek',
        'base_url': 'https://api.deepseek.com',
        'models': ['deepseek-chat']
    }
}

RAG_SYSTEM_PROMPT = """你是博客的 AI 助手。请优先基于【参考资料】回答用户问题。
规则：
1. 只使用参考资料中的信息回答，禁止编造资料中不存在的内容；
2. 引用处用 [1][2] 标注来源编号；
3. 若参考资料不足以回答，明确告知"博客中暂无相关内容"，然后才可用通用知识补充并声明"以下为通用知识"；
4. 保持简洁，使用 Markdown。"""

def format_sse_event(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def log_ai_query(user_id, query, analysis, retrieved, latency_ms, model_name):
    """将查询记录异步写入 MySQL（简单同步写，如果嫌慢可以扔给 Celery）"""
    try:
        with engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO ai_query_logs 
                    (user_id, raw_query, rewritten_query, intent, sentiment, emotion, need_rag, retrieved_json, latency_ms, model)
                    VALUES (:uid, :raw, :rewritten, :intent, :sentiment, :emotion, :need_rag, :retrieved, :latency, :model)
                """),
                {
                    "uid": user_id,
                    "raw": query,
                    "rewritten": analysis.get("rewritten_query", query),
                    "intent": analysis.get("intent", "unknown"),
                    "sentiment": analysis.get("sentiment", "neutral"),
                    "emotion": analysis.get("emotion", "neutral"),
                    "need_rag": 1 if analysis.get("need_rag") else 0,
                    "retrieved": json.dumps(retrieved, ensure_ascii=False),
                    "latency": latency_ms,
                    "model": model_name
                }
            )
            conn.commit()
    except Exception as e:
        print(f"记录日志失败: {e}")


#非常重要，rag代词指代解决
def rewrite_query_with_history(user_id, message, api_key, base_url, model_name):
    """结合历史记录，将用户的最新追问改写为独立完整的检索句"""
    history = get_ai_context(user_id)
    if not history:
        return message  # 没有历史，直接返回原句

    # 只取最近 4 条历史（2轮对话），太长没必要还费 token
    recent_history = history[-4:]
    history_text = "\n".join([f"{m['role']}: {m['content']}" for m in recent_history])

    prompt = f"""下面是一段对话历史：
{history_text}

用户的最新问题是：{message}

请结合对话历史，将用户的最新问题改写为一个独立、完整、适合用于搜索引擎检索的句子。
要求：
1. 补全代词（如“它”、“这个”）指代的具体事物。
2. 直接输出改写后的句子，绝对不要包含任何解释、前缀或标点修饰。"""

    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        response = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            temperature=0.1,
            max_tokens=100,
        )
        rewritten = response.choices[0].message.content.strip()
        print(f"[RAG 改写] 原句: {message} -> 改写句: {rewritten}")
        return rewritten
    except Exception as e:
        print(f"[RAG 改写失败] {e}")
        return message  # 失败就降级用原句，不影响主流程


def _build_messages(user_id, system_prompt, user_message, rag_chunks=None, analysis=None):
    # 如果是 RAG 模式且有检索结果，重写 System Prompt
    if rag_chunks and analysis:
        system_prompt = build_system_prompt(analysis.get("intent", "knowledge_qa"), analysis.get("emotion", "neutral"))

        refs = "\n\n".join(
            f"[{i + 1}]（来自《{c['title']}》{c['heading_path']}）\n{c['text']}"
            for i, c in enumerate(rag_chunks)
        )
        system_prompt += f"\n\n【参考资料】\n{refs}"

    messages = [{"role": "system", "content": system_prompt or "You are a helpful assistant."}]
    messages.extend(get_ai_context(user_id))
    messages.append({"role": "user", "content": user_message})
    return messages

# ============================================================
# 同步聊天
# ============================================================
@ai_bp.route('/api/ai/chat', methods=['POST'])
@token_required
def chat(current_user_id):
    data = request.get_json() or {}
    user_id = current_user_id          # 核心修复：只信 token
    message = data.get('message')
    use_rag = data.get('use_rag', False) and RAG_ENABLED and bool(EMBEDDING_API_KEY)
    reset_context = data.get('reset_context', False)
    active_config = get_active_ai_config()

    model_provider = active_config.get('provider')
    model_name = active_config.get('model')
    system_prompt = active_config.get('system_prompt') or 'You are a helpful assistant.'
    api_key = active_config.get('api_key')
    base_url = active_config.get('base_url')

    temperature = data.get('temperature', 0.3)
    max_tokens = data.get('max_tokens', 500)

    if reset_context:
        clear_ai_context(user_id)
        if not message:
            return jsonify({
                'status': 0,
                'msg': '上下文已重置',
                'data': {'context_length': 0}
            })

    if not message:
        return jsonify({'status': 1, 'msg': '消息不能为空'}), 400

    if not api_key:
        return jsonify({'status': 1, 'msg': '未配置有效的 API Key，请联系管理员配置 AI'}), 400

    rag_chunks = retrieve(message) if use_rag else []
    # 提取由于参考资料需要的 max_tokens 变大
    if use_rag and max_tokens < 1500:
        max_tokens = 1500

    messages = _build_messages(user_id, system_prompt, message, rag_chunks)

    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        assistant_reply = response.choices[0].message.content

        # 持久化到 Redis：先 user 再 assistant
        append_ai_message(user_id, {"role": "user", "content": message})
        append_ai_message(user_id, {"role": "assistant", "content": assistant_reply})

        ctx_len = len(get_ai_context(user_id)) + 1  # +1 是 system

        return jsonify({
            'status': 0,
            'msg': '成功',
            'data': {
                'reply': assistant_reply,
                'context_length': ctx_len,
                'model_used': model_name,
                'provider_used': model_provider,
            }
        })
    except Exception as e:
        return jsonify({'status': 1, 'msg': f'请求失败: {str(e)}'}), 500


# ============================================================
# 流式聊天（SSE）
# ============================================================
# ============================================================
# 流式聊天（SSE）- 完美融合意图识别与动态 RAG
# ============================================================
@ai_bp.route('/api/ai/chat/stream', methods=['POST'])
@token_required
def chat_stream(current_user_id):
    data = request.get_json() or {}
    user_id = current_user_id
    pack_id = data.get('pack_id')
    message = data.get('message')

    # 前端如果强行传了 use_rag=False，我们就尊重前端；否则交给意图分析器决定
    force_rag = data.get('use_rag')  # True, False, 或 None
    reset_context = data.get('reset_context', False)

    active_config = get_active_ai_config()
    model_provider = active_config.get('provider')
    model_name = active_config.get('model')
    api_key = active_config.get('api_key')
    base_url = active_config.get('base_url')

    if reset_context:
        clear_ai_context(user_id)

    def generate():
        # 处理重置上下文
        if reset_context and not message:
            yield format_sse_event({
                'type': 'done',
                'message': '上下文已重置',
                'context_length': 0,
                'model_used': model_name,
                'provider_used': model_provider,
            })
            return

        if not message:
            yield format_sse_event({'type': 'error', 'message': '消息不能为空'})
            return

        if not api_key:
            yield format_sse_event({'type': 'error', 'message': '未配置有效的 API Key，请联系管理员配置 AI'})
            return

        try:
            # ========================================================
            # 1. 意图与情绪分析 (大模型前置分析)
            # ========================================================
            analysis = analyze_query(user_id, message)

            # 立即推送分析事件给前端，让前端可以展示“意图/情绪”标签
            yield format_sse_event({
                'type': 'analysis',
                'analysis': {
                    'intent': analysis.get('intent'),
                    'emotion': analysis.get('emotion'),
                    'need_rag': analysis.get('need_rag')
                }
            })

            # 决定是否走 RAG
            use_rag = analysis.get('need_rag', True) and RAG_ENABLED and bool(EMBEDDING_API_KEY)
            if force_rag is False:
                use_rag = False

            # 获取改写后的搜索词 (如果没有改写，就用原句)
            search_query = analysis.get('rewritten_query') or message

            # ========================================================
            # 2. RAG 检索 (混合检索 + 精排)
            # ========================================================
            article_ids = None
            if pack_id and use_rag:
                with engine.connect() as conn:
                    rows = conn.execute(
                        text("SELECT document_id FROM context_pack_documents WHERE pack_id = :pid"),
                        {"pid": pack_id}
                    ).scalars().all()
                    article_ids = list(rows) if rows else [-1]

            rag_chunks = []
            start_time = time.time()  # ⚠️ 新增：开始计时
            if use_rag:
                rag_chunks = retrieve(search_query, article_ids=article_ids)

            # 记录检索耗时
            latency_ms = int((time.time() - start_time) * 1000)

            references = [
                {"index": i + 1, "article_id": c["article_id"], "title": c["title"],
                 "score": round(c.get("rerank_score", c["score"]), 3), "heading": c["heading_path"]}
                for i, c in enumerate(rag_chunks)
            ]

            # ⚠️ 新增：写入数据库
            if use_rag:
                log_ai_query(
                    user_id=user_id,
                    query=message,
                    analysis=analysis,
                    retrieved=references,
                    latency_ms=latency_ms,
                    model_name=model_name
                )

            # ========================================================
            # 3. 构建 Prompt 并调用大模型
            # ========================================================
            # 注意：这里不再需要手动判断 use_rag，_build_messages 里会自动处理 analysis 和 rag_chunks
            messages = _build_messages(user_id, None, message, rag_chunks, analysis)

            assistant_reply = ''
            # 如果是 RAG，允许长一点；闲聊就短一点
            actual_max_tokens = 1500 if use_rag else 500

            client = OpenAI(base_url=base_url, api_key=api_key)

            # 发送开始生成的信号
            yield format_sse_event({
                'type': 'start',
                'model_used': model_name,
                'provider_used': model_provider,
            })

            # 发送参考资料引用
            if use_rag and references:
                yield format_sse_event({'type': 'references', 'references': references})

            # 调用大模型流式接口
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                stream=True,
                # 排错场景 temperature 降低，保证严谨；其他场景发挥创造性
                temperature=0.3 if analysis.get('intent') == 'troubleshooting' else 0.7,
                max_tokens=actual_max_tokens,
            )

            for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if not delta:
                    continue
                assistant_reply += delta
                yield format_sse_event({'type': 'delta', 'content': delta})

            # ========================================================
            # 4. 保存对话上下文
            # ========================================================
            append_ai_message(user_id, {"role": "user", "content": message})
            append_ai_message(user_id, {"role": "assistant", "content": assistant_reply})

            # 发送结束信号
            yield format_sse_event({
                'type': 'done',
                'context_length': len(get_ai_context(user_id)) + 1,
                'model_used': model_name,
                'provider_used': model_provider,
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield format_sse_event({'type': 'error', 'message': f'请求失败: {str(e)}'})

    headers = {
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
    }
    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers=headers)


# ============================================================
# AI 生成文章（加入了redis和celery）
# ============================================================
@ai_bp.route('/api/ai/generate-article', methods=['POST'])
def generate_article():
    data = request.get_json()
    topic = data.get('topic')

    if not topic:
        return jsonify({'status': 1, 'msg': '请输入文章概要或主题'}), 400

    # 投递任务给 Celery（这就类似是点外卖）
    task = generate_article_task.delay(topic)

    # 立刻返回 Task ID（这就是取餐号）
    return jsonify({
        'status': 0,
        'msg': '任务已提交，正在后台生成中',
        'data': {
            'task_id': task.id
        }
    })


# 新增一个查询任务结果的接口
@ai_bp.route('/api/ai/task-status/<task_id>', methods=['GET'])
def get_task_status(task_id):
    # 凭 Task ID 去 Redis 查进度
    task_result = AsyncResult(task_id, app=celery_app)

    if task_result.state == 'PENDING':
        response = {'state': task_result.state, 'msg': '任务排队中或正在执行'}
    elif task_result.state == 'SUCCESS':
        response = {'state': task_result.state, 'result': task_result.result}
    elif task_result.state == 'FAILURE':
        response = {'state': task_result.state, 'msg': '生成失败，请重试'}
    else:
        response = {'state': task_result.state, 'msg': '其他状态'}

    return jsonify({'status': 0, 'data': response})


# ============================================================
# 上下文管理（已迁移 Redis）
# ============================================================
@ai_bp.route('/api/ai/context', methods=['GET'])
@token_required
def get_context(current_user_id):
    msgs = get_ai_context(current_user_id)
    return jsonify({
        'status': 0,
        'data': {
            'context': msgs,
            'length': len(msgs),
        }
    })


@ai_bp.route('/api/ai/context', methods=['DELETE'])
@token_required
def clear_context(current_user_id):
    clear_ai_context(current_user_id)
    return jsonify({'status': 0, 'msg': '上下文已清除'})


# ============================================================
# 后台管理接口（保持原样）
# ============================================================
@ai_bp.route('/api/ai/models', methods=['GET'])
def get_models():
    return jsonify({'status': 0, 'data': {'models': SUPPORTED_MODELS}})


@ai_bp.route('/api/ai/configs', methods=['GET'])
def get_configs():
    try:
        provider = request.args.get('provider')
        model = request.args.get('model')

        with engine.connect() as conn:
            sql = "SELECT * FROM ai_configs WHERE 1=1"
            params = {}
            if provider:
                sql += " AND provider = :provider"
                params['provider'] = provider
            if model:
                sql += " AND model LIKE :model"
                params['model'] = f"%{model}%"
            sql += " ORDER BY created_at DESC"

            result = conn.execute(text(sql), params).mappings().fetchall()
            configs = []
            for row in result:
                config = dict(row)
                api_key = config.pop('api_key', '') or ''
                if len(api_key) > 8:
                    config['api_key_masked'] = api_key[:4] + '******' + api_key[-4:]
                else:
                    config['api_key_masked'] = '******'
                if config.get('created_at'):
                    config['created_at'] = config['created_at'].isoformat()
                if config.get('updated_at'):
                    config['updated_at'] = config['updated_at'].isoformat()
                configs.append(config)
            return jsonify({'status': 0, 'data': configs})
    except Exception as e:
        return jsonify({'status': 1, 'msg': '获取配置列表失败', 'error': str(e)}), 500


@ai_bp.route('/api/ai/configs', methods=['POST'])
def create_config():
    data = request.get_json()
    provider = data.get('provider')
    api_key = data.get('api_key')
    base_url = data.get('base_url')
    model = data.get('model')
    system_prompt = data.get('system_prompt', '')
    is_active = data.get('is_active', False)

    if not all([provider, api_key, base_url, model]):
        return jsonify({'status': 1, 'msg': '缺少必要参数'}), 400

    try:
        with engine.connect() as conn:
            if is_active:
                conn.execute(text("UPDATE ai_configs SET is_active = 0"))
            conn.execute(
                text("""
                    INSERT INTO ai_configs (provider, api_key, base_url, model, system_prompt, is_active)
                    VALUES (:provider, :api_key, :base_url, :model, :system_prompt, :is_active)
                """),
                {
                    "provider": provider,
                    "api_key": api_key,
                    "base_url": base_url,
                    "model": model,
                    "system_prompt": system_prompt,
                    "is_active": 1 if is_active else 0,
                }
            )
            conn.commit()
            return jsonify({'status': 0, 'msg': '配置创建成功'})
    except Exception as e:
        return jsonify({'status': 1, 'msg': '创建失败', 'error': str(e)}), 500

@ai_bp.route('/api/ai/configs/<int:config_id>', methods=['PUT'])
def update_config_item(config_id):
    data = request.get_json()
    provider = data.get('provider')
    api_key = data.get('api_key')
    base_url = data.get('base_url')
    model = data.get('model')
    system_prompt = data.get('system_prompt')
    is_active = data.get('is_active')

    try:
        with engine.connect() as conn:
            if is_active:
                conn.execute(text("UPDATE ai_configs SET is_active = 0"))

            update_parts = []
            params = {"id": config_id}
            if provider:
                update_parts.append("provider = :provider")
                params["provider"] = provider
            if api_key:
                update_parts.append("api_key = :api_key")
                params["api_key"] = api_key
            if base_url:
                update_parts.append("base_url = :base_url")
                params["base_url"] = base_url
            if model:
                update_parts.append("model = :model")
                params["model"] = model
            if system_prompt is not None:
                update_parts.append("system_prompt = :system_prompt")
                params["system_prompt"] = system_prompt
            if is_active is not None:
                update_parts.append("is_active = :is_active")
                params["is_active"] = 1 if is_active else 0

            if not update_parts:
                return jsonify({'status': 0, 'msg': '无变更'})

            sql = f"UPDATE ai_configs SET {', '.join(update_parts)} WHERE id = :id"
            conn.execute(text(sql), params)
            conn.commit()
            return jsonify({'status': 0, 'msg': '更新成功'})
    except Exception as e:
        return jsonify({'status': 1, 'msg': '更新失败', 'error': str(e)}), 500


@ai_bp.route('/api/ai/configs/<int:config_id>', methods=['DELETE'])
def delete_config(config_id):
    try:
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM ai_configs WHERE id = :id"), {"id": config_id})
            conn.commit()
            return jsonify({'status': 0, 'msg': '删除成功'})
    except Exception as e:
        return jsonify({'status': 1, 'msg': '删除失败', 'error': str(e)}), 500


@ai_bp.route('/api/ai/configs/<int:config_id>/activate', methods=['POST'])
def activate_config(config_id):
    try:
        with engine.connect() as conn:
            conn.execute(text("UPDATE ai_configs SET is_active = 0"))
            conn.execute(text("UPDATE ai_configs SET is_active = 1 WHERE id = :id"), {"id": config_id})
            conn.commit()
            return jsonify({'status': 0, 'msg': '已启用该配置'})
    except Exception as e:
        return jsonify({'status': 1, 'msg': '操作失败', 'error': str(e)}), 500