# backend/rag/chunker.py
import re

from backend.config import RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_IMAGE_RE      = re.compile(r"!\[[^\]]*\]\([^\)]*\)")
_HTML_TAG_RE   = re.compile(r"<[^>]+>")
_LINK_RE       = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_HEADING_RE    = re.compile(r"^#{1,4}\s+(.+?)\s*$", re.MULTILINE)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _clean_md(text: str) -> str:
    text = _CODE_BLOCK_RE.sub("", text)
    text = _IMAGE_RE.sub("", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)          # [文字](url) -> 文字
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text


def _heading_level(line: str) -> int:
    level = 0
    for ch in line:
        if ch == "#":
            level += 1
        else:
            break
    return level


def _split_by_headings(md: str):
    """返回 [(heading_path, body), ...]，heading_path 形如 'H1 > H2 > H3'。"""
    matches = list(_HEADING_RE.finditer(md))
    if not matches:
        return [("", md)]

    sections = []
    stack = {1: "", 2: "", 3: "", 4: ""}

    first_start = matches[0].start()
    if first_start > 0:
        pre = md[:first_start].strip()
        if pre:
            sections.append(("", pre))

    for idx, m in enumerate(matches):
        line = m.group(0).strip()
        level = _heading_level(line)
        title = m.group(1).strip()

        body_start = m.end()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md)
        body = md[body_start:body_end].strip()

        stack[level] = title
        for lv in range(level + 1, 5):
            stack[lv] = ""

        heading_path = " > ".join(stack[lv] for lv in range(1, 5) if stack.get(lv))
        sections.append((heading_path, body))

    return sections


def _split_body(body: str) -> list[str]:
    """把一节切成 <= RAG_CHUNK_SIZE 的块；超长段硬切带 overlap。"""
    if not body:
        return []
    if len(body) <= RAG_CHUNK_SIZE:
        return [body]

    paragraphs = re.split(r"\n\s*\n", body)
    chunks, buf = [], ""
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if not buf:
            buf = p
        elif len(buf) + 2 + len(p) <= RAG_CHUNK_SIZE:
            buf = buf + "\n\n" + p
        else:
            chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)

    result = []
    step = max(1, RAG_CHUNK_SIZE - RAG_CHUNK_OVERLAP)
    for c in chunks:
        if len(c) <= RAG_CHUNK_SIZE:
            result.append(c)
            continue
        i = 0
        while i < len(c):
            result.append(c[i:i + RAG_CHUNK_SIZE])
            if i + RAG_CHUNK_SIZE >= len(c):
                break
            i += step
    return result


def chunk_article(title: str, content_md: str) -> list[dict]:
    """
    返回 [{"text": embedding 用的文本（带上下文头）,
           "raw_text": 原始块文本,
           "chunk_index": i,
           "heading_path": path}]
    """
    if not content_md:
        return []

    cleaned = _clean_md(content_md)
    chunks = []
    idx = 0
    for heading_path, body in _split_by_headings(cleaned):
        for b in _split_body(body):
            if len(b) < 20:
                continue
            head = f"【文章】{title}\n"
            if heading_path:
                head += f"【章节】{heading_path}\n"
            chunks.append({
                "text": head + b,
                "raw_text": b,
                "chunk_index": idx,
                "heading_path": heading_path,
            })
            idx += 1
    return chunks