# backend_fastapi/routers/ai.py
"""
AI 路由，对应原 backend/routes/ai.py。

11 个接口：
  POST   /api/ai/chat                           同步聊天（无认证）
  POST   /api/ai/chat/stream                    SSE 流式聊天（无认证）★核心
  POST   /api/ai/generate-article               生成文章（无认证）
  GET    /api/ai/context                        查询上下文（无认证）
  DELETE /api/ai/context                        清空上下文（无认证）
  GET    /api/ai/models                         支持的模型列表
  GET    /api/ai/configs                        配置列表
  POST   /api/ai/configs                        新建配置
  PUT    /api/ai/configs/{config_id}            更新配置
  DELETE /api/ai/configs/{config_id}            删除配置
  POST   /api/ai/configs/{config_id}/activate   启用配置

★ SSE 流式聊天：
  - 用 StreamingResponse 替代 Flask 的 Response(stream_with_context(...))
  - 生成器为同步生成器，Starlette 会在线程池里迭代，不阻塞事件循环
  - 事件负载：{"type": "start"/"delta"/"done"/"error", ...}
  - 每帧格式："data: {json}\n\n"

⚠️ context_store 仍是内存字典（与原 Flask 一致），多 worker 会各存一份。
   未来接入 Redis 时替换此处的 context_store 即可。
"""
import json
import re
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from database import engine
from models.ai import (
    ChatRequest,
    ChatStreamRequest,
    ConfigCreateRequest,
    ConfigUpdateRequest,
    GenerateArticleRequest,
)
from services import ai_provider
from services.ai_provider import (
    SUPPORTED_MODELS,
    chat_completion,
    chat_completion_stream,
    get_active_config,
)

router = APIRouter()


# ==================================================================
# 内存上下文（与原 Flask 一致）
# ==================================================================
context_store: Dict[str, List[Dict]] = {}
# from cache import (
#     get_context as cache_get_context,
#     save_context as cache_save_context,
#     clear_context as cache_clear_context,
# )


def format_sse_event(payload: dict) -> bytes:
    """
    格式化为 SSE 帧：b"data: {json}\n\n"
    直接返回 bytes，避免 Starlette 编码环节出问题（尤其是中文）。
    ensure_ascii=False 保留中文原样输出，前端更容易调试。
    """
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def trim_context(user_id: str):
    """
    上下文太长时裁剪：保留首条 system + 最近 18 条。
    与原 Flask 的 trim_context 完全一致。
    """
    if len(context_store[user_id]) > 20:
        context_store[user_id] = (
            context_store[user_id][0:1] + context_store[user_id][-18:]
        )


# ==================================================================
# 1. POST /api/ai/chat   —— 同步聊天
# ==================================================================
@router.post("/ai/chat")
def chat(req: ChatRequest):
    user_id = req.user_id or "default"
    message = req.message
    reset_context = req.reset_context or False

    # 查激活配置
    active_config = get_active_config()

    model_provider = req.model_provider or active_config.get("provider")
    model_name = req.model_name or active_config.get("model")
    system_prompt = req.system_prompt or active_config.get("system_prompt")
    api_key = active_config.get("api_key")
    base_url = active_config.get("base_url")

    temperature = req.temperature if req.temperature is not None else 0.3
    max_tokens = req.max_tokens if req.max_tokens is not None else 500

    # 重置上下文
    if reset_context:
        context_store[user_id] = []
        if not message:
            return {
                "status": 0,
                "msg": "上下文已重置",
                "data": {"context_length": 0},
            }

    if not message:
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "消息不能为空"},
        )

    # 初始化或获取上下文
    if user_id not in context_store or reset_context:
        context_store[user_id] = [{"role": "system", "content": system_prompt}]

    context_store[user_id].append({"role": "user", "content": message})

    try:
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail={
                    "status": 1,
                    "msg": "未配置有效的 API Key，请联系管理员配置 AI",
                },
            )

        response = chat_completion(
            messages=context_store[user_id],
            config=active_config,
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        assistant_reply = response.choices[0].message.content
        context_store[user_id].append(
            {"role": "assistant", "content": assistant_reply}
        )

        if len(context_store[user_id]) > 20:
            context_store[user_id] = (
                context_store[user_id][0:1] + context_store[user_id][-18:]
            )

        return {
            "status": 0,
            "msg": "成功",
            "data": {
                "reply": assistant_reply,
                "context_length": len(context_store[user_id]),
                "model_used": model_name,
                "provider_used": model_provider,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": f"请求失败: {str(e)}"},
        )


# ==================================================================
# 2. POST /api/ai/chat/stream   —— SSE 流式聊天 ★核心
# ==================================================================
@router.post("/ai/chat/stream")
def chat_stream(req: ChatStreamRequest):
    user_id = req.user_id or "default"
    message = req.message
    reset_context = req.reset_context or False

    active_config = get_active_config()

    model_provider = req.model_provider or active_config.get("provider")
    model_name = req.model_name or active_config.get("model")
    system_prompt = req.system_prompt or active_config.get("system_prompt")
    api_key = active_config.get("api_key")
    base_url = active_config.get("base_url")
    temperature = req.temperature if req.temperature is not None else 0.3
    max_tokens = req.max_tokens if req.max_tokens is not None else 500

    # 若请求里带 reset_context，先清空（与原 Flask 一致）
    if reset_context:
        context_store[user_id] = []

    # ---------------- 同步生成器 ----------------
    def generate():
        # 提前校验（错误也用 SSE 事件返回，保持 HTTP 200）
        if reset_context and not message:
            yield format_sse_event(
                {
                    "type": "done",
                    "message": "上下文已重置",
                    "context_length": 0,
                    "model_used": model_name,
                    "provider_used": model_provider,
                }
            )
            return

        if not message:
            yield format_sse_event(
                {"type": "error", "message": "消息不能为空"}
            )
            return

        if not api_key:
            yield format_sse_event(
                {
                    "type": "error",
                    "message": "未配置有效的 API Key，请联系管理员配置 AI",
                }
            )
            return

        if user_id not in context_store or reset_context:
            context_store[user_id] = [
                {"role": "system", "content": system_prompt}
            ]

        context_store[user_id].append({"role": "user", "content": message})
        assistant_reply = ""

        try:
            # 发送 start 事件
            yield format_sse_event(
                {
                    "type": "start",
                    "model_used": model_name,
                    "provider_used": model_provider,
                }
            )

            # 调 OpenAI 流式接口
            response = chat_completion_stream(
                messages=context_store[user_id],
                config=active_config,
                model=model_name,
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

                yield format_sse_event({"type": "delta", "content": delta})

            # 保存助手回复
            context_store[user_id].append(
                {"role": "assistant", "content": assistant_reply}
            )
            trim_context(user_id)

            # 发送 done 事件
            yield format_sse_event(
                {
                    "type": "done",
                    "context_length": len(context_store[user_id]),
                    "model_used": model_name,
                    "provider_used": model_provider,
                }
            )

        except Exception as e:
            yield format_sse_event(
                {"type": "error", "message": f"请求失败: {str(e)}"}
            )

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",   # Nginx 反代时禁用缓冲，否则打字机效果会失效
    }

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=headers,
    )


# ==================================================================
# 3. POST /api/ai/generate-article   —— 生成文章
# ==================================================================
@router.post("/ai/generate-article")
def generate_article(req: GenerateArticleRequest):
    topic = req.topic
    print(f"[generate-article] topic: {topic}")

    if not topic:
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "请输入文章概要或主题"},
        )

    active_config = get_active_config()
    api_key = active_config.get("api_key")
    base_url = active_config.get("base_url")
    model_name = active_config.get("model")

    if not active_config:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "AI服务未配置"},
        )

    print(
        f"[generate-article] provider={active_config.get('provider')}, model={model_name}"
    )

    system_prompt = """
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

    user_prompt = f"请直接输出符合格式的 JSON 字符串，文章主题关于：{topic}"

    try:
        print("[generate-article] Calling OpenAI API...")
        response = chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            config=active_config,
            model=model_name,
            temperature=0.7,
            max_tokens=2500,
        )

        content = response.choices[0].message.content
        print(f"[generate-article] AI response length: {len(content)}")

        try:
            # 健壮的 JSON 提取
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                content = json_match.group(0)

            article_data = json.loads(content.strip(), strict=False)
            print("[generate-article] JSON parsed successfully")

            return {"status": 0, "msg": "生成成功", "data": article_data}

        except Exception as json_err:
            print(f"[generate-article] JSON parse failed: {json_err}")
            return {
                "status": 0,
                "msg": "生成成功（非标准格式）",
                "data": {
                    "title": topic[:20],
                    "content": content,
                    "summary": content[:150].replace("\n", " ") + "...",
                    "category": "other",
                    "tags": ["AI生成"],
                },
            }

    except Exception as e:
        print(f"[generate-article] AI generation failed: {e}")
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": f"生成失败: {str(e)}"},
        )


# ==================================================================
# 4. GET /api/ai/context   —— 查询当前用户上下文
# ==================================================================
@router.get("/ai/context")
def get_context(user_id: str = Query("default")):
    if user_id not in context_store:
        return {"status": 0, "data": {"context": []}}

    return {
        "status": 0,
        "data": {
            "context": context_store[user_id],
            "length": len(context_store[user_id]),
        },
    }


# ==================================================================
# 5. DELETE /api/ai/context   —— 清空上下文
# ==================================================================
@router.delete("/ai/context")
def clear_context(user_id: str = Query("default")):
    if user_id in context_store:
        del context_store[user_id]
    return {"status": 0, "msg": "上下文已清除"}


# ==================================================================
# 6. GET /api/ai/models   —— 支持的模型列表
# ==================================================================
@router.get("/ai/models")
def get_models():
    return {"status": 0, "data": {"models": SUPPORTED_MODELS}}


# ==================================================================
# 7. GET /api/ai/configs   —— 配置列表（api_key 脱敏）
# ==================================================================
@router.get("/ai/configs")
def get_configs(
    provider: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
):
    try:
        with engine.connect() as conn:
            sql = "SELECT * FROM ai_configs WHERE 1=1"
            params = {}

            if provider:
                sql += " AND provider = :provider"
                params["provider"] = provider
            if model:
                sql += " AND model LIKE :model"
                params["model"] = f"%{model}%"

            sql += " ORDER BY created_at DESC"
            result = conn.execute(text(sql), params).mappings().fetchall()

            configs = []
            for row in result:
                config = dict(row)
                api_key = config.pop("api_key", "") or ""

                if len(api_key) > 8:
                    config["api_key_masked"] = (
                        api_key[:4] + "******" + api_key[-4:]
                    )
                else:
                    config["api_key_masked"] = "******"

                if config.get("created_at"):
                    config["created_at"] = config["created_at"].isoformat()
                if config.get("updated_at"):
                    config["updated_at"] = config["updated_at"].isoformat()

                configs.append(config)

            return {"status": 0, "data": configs}

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "获取配置列表失败", "error": str(e)},
        )


# ==================================================================
# 8. POST /api/ai/configs   —— 新建配置
# ==================================================================
@router.post("/ai/configs")
def create_config(req: ConfigCreateRequest):
    provider = req.provider
    api_key = req.api_key
    base_url = req.base_url
    model = req.model
    system_prompt = req.system_prompt or ""
    is_active = req.is_active or False

    if not all([provider, api_key, base_url, model]):
        raise HTTPException(
            status_code=400,
            detail={"status": 1, "msg": "缺少必要参数"},
        )

    try:
        with engine.connect() as conn:
            if is_active:
                conn.execute(text("UPDATE ai_configs SET is_active = 0"))

            conn.execute(
                text(
                    """
                    INSERT INTO ai_configs
                        (provider, api_key, base_url, model, system_prompt, is_active)
                    VALUES
                        (:provider, :api_key, :base_url, :model, :system_prompt, :is_active)
                    """
                ),
                {
                    "provider": provider,
                    "api_key": api_key,
                    "base_url": base_url,
                    "model": model,
                    "system_prompt": system_prompt,
                    "is_active": 1 if is_active else 0,
                },
            )
            conn.commit()
            return {"status": 0, "msg": "配置创建成功"}

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "创建失败", "error": str(e)},
        )


# ==================================================================
# 9. PUT /api/ai/configs/{config_id}   —— 更新配置
# ==================================================================
@router.put("/ai/configs/{config_id}")
def update_config_item(config_id: int, req: ConfigUpdateRequest):
    try:
        with engine.connect() as conn:
            if req.is_active:
                conn.execute(text("UPDATE ai_configs SET is_active = 0"))

            update_parts = []
            params = {"id": config_id}

            if req.provider:
                update_parts.append("provider = :provider")
                params["provider"] = req.provider
            if req.api_key:
                update_parts.append("api_key = :api_key")
                params["api_key"] = req.api_key
            if req.base_url:
                update_parts.append("base_url = :base_url")
                params["base_url"] = req.base_url
            if req.model:
                update_parts.append("model = :model")
                params["model"] = req.model
            if req.system_prompt is not None:
                update_parts.append("system_prompt = :system_prompt")
                params["system_prompt"] = req.system_prompt
            if req.is_active is not None:
                update_parts.append("is_active = :is_active")
                params["is_active"] = 1 if req.is_active else 0

            if not update_parts:
                return {"status": 0, "msg": "无变更"}

            sql = (
                f"UPDATE ai_configs SET {', '.join(update_parts)} WHERE id = :id"
            )
            conn.execute(text(sql), params)
            conn.commit()

            return {"status": 0, "msg": "更新成功"}

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "更新失败", "error": str(e)},
        )


# ==================================================================
# 10. DELETE /api/ai/configs/{config_id}   —— 删除配置
# ==================================================================
@router.delete("/ai/configs/{config_id}")
def delete_config(config_id: int):
    try:
        with engine.connect() as conn:
            conn.execute(
                text("DELETE FROM ai_configs WHERE id = :id"), {"id": config_id}
            )
            conn.commit()
            return {"status": 0, "msg": "删除成功"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "删除失败", "error": str(e)},
        )


# ==================================================================
# 11. POST /api/ai/configs/{config_id}/activate   —— 启用配置
# ==================================================================
@router.post("/ai/configs/{config_id}/activate")
def activate_config(config_id: int):
    try:
        with engine.connect() as conn:
            # 先禁用所有，再启用指定
            conn.execute(text("UPDATE ai_configs SET is_active = 0"))
            conn.execute(
                text("UPDATE ai_configs SET is_active = 1 WHERE id = :id"),
                {"id": config_id},
            )
            conn.commit()
            return {"status": 0, "msg": "已启用该配置"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"status": 1, "msg": "操作失败", "error": str(e)},
        )