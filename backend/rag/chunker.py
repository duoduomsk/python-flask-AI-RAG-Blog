# backend/rag/chunker.py
"""
RAG 切片器 v2：
- 保代码块、保表格、保列表
- 结构感知父子块：子块召回 + 父块给模型
- 按 token 而不是字符切分
- 输出 chunk_type / chunk_hash / token_count 等 payload 字段
"""
import hashlib
import re
from backend.rag.html_cleaner import clean_html_to_markdown
from backend.config import (
    RAG_CHUNK_TOKENS,
    RAG_PARENT_TOKENS,
    RAG_CHUNK_OVERLAP_TOKENS,
    RAG_CHUNKER_VERSION,
)
from backend.rag.structure_parser import (
    parse_blocks, build_sections, render_block, estimate_tokens,
    BLOCK_CODE, BLOCK_TABLE, BLOCK_LIST, BLOCK_QUOTE, BLOCK_PARAGRAPH,
)

# 句子分隔：中文句号/问号/感叹号/分号、英文 .!?;、换行
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


# ------------------------------------------------------------------
# 小工具
# ------------------------------------------------------------------
def _dominant_type(blocks) -> str:
    """一个子块里若有代码 → code；否则 table → list → paragraph。"""
    types = [b.type for b in blocks]
    for t in (BLOCK_CODE, BLOCK_TABLE, BLOCK_LIST, BLOCK_QUOTE):
        if t in types:
            return t
    return BLOCK_PARAGRAPH


def _join_blocks(blocks) -> str:
    return "\n\n".join(render_block(b) for b in blocks)


def _split_text(text: str, chunk_type: str, max_tokens: int) -> list[dict]:
    """按句子切分普通文本。"""
    sentences = _SENT_SPLIT.split(text)
    pieces, buf = [], ""
    for s in sentences:
        if not s:
            continue
        if estimate_tokens(buf + s) > max_tokens and buf:
            pieces.append(buf)
            # 重叠：取上一段尾部
            tail = buf[-RAG_CHUNK_OVERLAP_TOKENS * 2:] if RAG_CHUNK_OVERLAP_TOKENS > 0 else ""
            buf = tail + s
        else:
            buf += s
    if buf:
        pieces.append(buf)
    return [{"text": p, "raw_text": p, "chunk_type": chunk_type} for p in pieces]


def _split_code_block(block, max_tokens: int) -> list[dict]:
    """大代码块按行切，每段保留语言头和代码围栏。"""
    lines = block.content.split("\n")
    if len(lines) < 2:
        return [{"text": block.content, "raw_text": block.content, "chunk_type": BLOCK_CODE}]

    header = lines[0]                    # ```python
    footer = lines[-1] if lines[-1].strip().startswith("```") or lines[-1].strip().startswith("~~~") else "```"
    body = lines[1:-1] if footer != "```" or lines[-1].strip().startswith(("```", "~~~")) else lines[1:]

    pieces, buf, buf_tok = [], [], 0
    for ln in body:
        ln_tok = estimate_tokens(ln)
        if buf_tok + ln_tok > max_tokens and buf:
            pieces.append("\n".join(buf))
            buf, buf_tok = [], 0
        buf.append(ln)
        buf_tok += ln_tok
    if buf:
        pieces.append("\n".join(buf))

    result = []
    for p in pieces:
        text = f"{header}\n{p}\n{footer}"
        result.append({"text": text, "raw_text": text, "chunk_type": BLOCK_CODE})
    return result


def _split_section_blocks(blocks, max_tokens: int) -> list[dict]:
    """
    核心：把一个 section 的 blocks 切成子块。
    规则：
    - 代码块 / 表格整体不超过 max_tokens → 不拆
    - 超大代码块 → 按行拆
    - 普通文本 → 按句拆
    - 小块累加，超过阈值就 flush
    """
    chunks = []
    buf_blocks = []
    buf_tokens = 0

    def flush():
        nonlocal buf_blocks, buf_tokens
        if not buf_blocks:
            return
        text = _join_blocks(buf_blocks)
        chunks.append({
            "text": text,
            "raw_text": text,
            "chunk_type": _dominant_type(buf_blocks),
        })
        buf_blocks, buf_tokens = [], 0

    for b in blocks:
        b_text = render_block(b)
        b_tokens = estimate_tokens(b_text)

        # 代码块 / 表格：能整体放就整体放，尽量不拆
        if b.type in (BLOCK_CODE, BLOCK_TABLE):
            if b_tokens <= max_tokens:
                if buf_tokens + b_tokens > max_tokens and buf_blocks:
                    flush()
                buf_blocks.append(b)
                buf_tokens += b_tokens
                continue
            # 超大代码块
            if b.type == BLOCK_CODE:
                if buf_blocks:
                    flush()
                chunks.extend(_split_code_block(b, max_tokens))
                continue
            # 超大表格：暂时硬切（极少见）
            if buf_blocks:
                flush()
            chunks.extend(_split_text(b_text, BLOCK_TABLE, max_tokens))
            continue

        # 普通文本块：太大就按句拆
        if b_tokens > max_tokens:
            if buf_blocks:
                flush()
            chunks.extend(_split_text(b_text, b.type, max_tokens))
            continue

        # 小块累加
        if buf_tokens + b_tokens > max_tokens and buf_blocks:
            flush()
        buf_blocks.append(b)
        buf_tokens += b_tokens

    flush()
    return chunks


def _extract_keywords(text: str, top_k: int = 6) -> list[str]:
    """
    轻量关键词：不引入 jieba 依赖，先用正则抽英文标识符 + 中文高频 2-3 字。
    阶段 1.2/1.4 再换成 jieba 或 LLM。
    """
    if not text:
        return []
    # 英文标识符：类名/函数名/错误码
    eng = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", text)
    # 中文 2-4 字片段（粗筛，不精准，够用）
    zh = re.findall(r"[\u4e00-\u9fff]{2,4}", text)

    seen, result = set(), []
    for w in eng + zh:
        w_l = w.lower()
        if w_l in seen or len(result) >= top_k:
            continue
        seen.add(w_l)
        result.append(w)
    return result


# ------------------------------------------------------------------
# 对外接口
# ------------------------------------------------------------------
def chunk_article(title: str, content_md: str,
                  doc_intent: str = "", difficulty: str = "") -> list[dict]:
    """
    返回子块列表。字段：
    - text:           带上下文头的 embedding 文本（"【文章】xxx【章节】xxx\n原文"）
    - raw_text:       原始块文本（拼 Prompt 时给模型看）
    - parent_text:    父块文本（同一 section 拼起来，供父子展开）
    - parent_id:      父块 hash，用于 retriever 去重
    - chunk_index:    自增序号
    - chunk_type:     paragraph/code/table/list/quote
    - heading_path:   "H1 > H2 > H3"
    - keywords:       关键词列表（阶段 1.2 sparse 用）
    - token_count:    估算 token
    - chunk_hash:     md5，去重用
    - doc_intent:     文档意图（阶段 1.4 用）
    - difficulty:     难度
    - chunker_version: 切片器版本
    """
    if not content_md:
        return []

    content_md = clean_html_to_markdown(content_md)

    blocks = parse_blocks(content_md)
    sections = build_sections(blocks)

    chunks: list[dict] = []
    seen_hashes = set()
    idx = 0

    for section in sections:
        # ---- 父块文本 ----
        parent_text = _join_blocks(section.blocks)
        if section.heading_path:
            parent_text = f"## {section.heading_path}\n{parent_text}"
        # 父块过大时截断（父块只用于给模型看上下文，不需要完整）
        if estimate_tokens(parent_text) > RAG_PARENT_TOKENS:
            # 按估算比例粗略截断
            ratio = RAG_PARENT_TOKENS / max(1, estimate_tokens(parent_text))
            cut = int(len(parent_text) * ratio)
            parent_text = parent_text[:cut] + "\n...(截断)"
        parent_id = hashlib.md5(parent_text.encode("utf-8")).hexdigest()[:16]

        # ---- 子块 ----
        sub_chunks = _split_section_blocks(section.blocks, RAG_CHUNK_TOKENS)
        for sub in sub_chunks:
            raw = sub["raw_text"].strip()
            # 过滤太碎的块
            if len(raw) < 30:
                continue
            h = hashlib.md5(raw.encode("utf-8")).hexdigest()
            if h in seen_hashes:
                continue
            seen_hashes.add(h)

            head = f"【文章】{title}\n"
            if section.heading_path:
                head += f"【章节】{section.heading_path}\n"
            embed_text = head + raw

            chunks.append({
                "text": embed_text,
                "raw_text": raw,
                "parent_text": parent_text,
                "parent_id": parent_id,
                "chunk_index": idx,
                "chunk_type": sub["chunk_type"],
                "heading_path": section.heading_path,
                "keywords": _extract_keywords(raw),
                "token_count": estimate_tokens(embed_text),
                "chunk_hash": h,
                "doc_intent": doc_intent,
                "difficulty": difficulty,
                "chunker_version": RAG_CHUNKER_VERSION,
            })
            idx += 1

    return chunks