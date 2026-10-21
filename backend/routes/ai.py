# backend/routes/ai.py
from flask import Blueprint, request, jsonify, Response, stream_with_context
import os
from openai import OpenAI
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

def format_sse_event(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

def _build_messages(user_id, system_prompt, user_message):
    """
    组装本次请求的完整 messages：
      [system] + 会话历史（不含 system） + 本次 user 消息
    会话历史来自 Redis，不再依赖进程内字典。
    """
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

    messages = _build_messages(user_id, system_prompt, message)

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
@ai_bp.route('/api/ai/chat/stream', methods=['POST'])
@token_required
def chat_stream(current_user_id):
    data = request.get_json() or {}
    user_id = current_user_id
    message = data.get('message')
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

    def generate():
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

        messages = _build_messages(user_id, system_prompt, message)
        assistant_reply = ''

        try:
            client = OpenAI(base_url=base_url, api_key=api_key)

            yield format_sse_event({
                'type': 'start',
                'model_used': model_name,
                'provider_used': model_provider,
            })

            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if not delta:
                    continue
                assistant_reply += delta
                yield format_sse_event({'type': 'delta', 'content': delta})

            # 落库到 Redis
            append_ai_message(user_id, {"role": "user", "content": message})
            append_ai_message(user_id, {"role": "assistant", "content": assistant_reply})
            ctx_len = len(get_ai_context(user_id)) + 1

            yield format_sse_event({
                'type': 'done',
                'context_length': ctx_len,
                'model_used': model_name,
                'provider_used': model_provider,
            })
        except Exception as e:
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