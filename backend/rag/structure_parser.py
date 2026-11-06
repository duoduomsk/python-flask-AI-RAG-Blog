# backend/rag/structure_parser.py
"""
Markdown 结构解析：把 Markdown 文本解析为有类型的 Block 序列，
再按标题层级组装成 Section（父块）。

设计目标：
- 代码块、表格、列表、引用都不被破坏
- 标题路径（heading_path）与现有 chunker 保持兼容格式 "H1 > H2 > H3"
- 纯函数、无副作用，方便单测
"""
from dataclasses import dataclass, field
from typing import List
import re

# 块类型常量
BLOCK_HEADING = "heading"
BLOCK_CODE = "code"
BLOCK_TABLE = "table"
BLOCK_LIST = "list"
BLOCK_QUOTE = "quote"
BLOCK_PARAGRAPH = "paragraph"

# 列表起始检测：- * + 或 1. 2. ...
_LIST_RE = re.compile(r"^\s*([-*+]|\d+\.)\s+")
# 表格分隔行：|---|---|
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:\-]+\|[\s:\-|]*$")


@dataclass
class Block:
    type: str
    content: str
    level: int = 0            # 仅 heading 用
    meta: dict = field(default_factory=dict)


@dataclass
class Section:
    heading_path: str
    level: int
    blocks: List[Block]


def _is_block_start(line: str) -> bool:
    """判断一行是否是某个块的"起点"，用于段落收尾。"""
    s = line.strip()
    if not s:
        return True
    if s.startswith("```") or s.startswith("~~~"):
        return True
    if s.startswith("#"):
        return True
    if s.startswith(">"):
        return True
    if _LIST_RE.match(line):
        return True
    return False


def parse_blocks(md: str) -> List[Block]:
    """把 Markdown 文本按块类型切分，代码块、表格整体保留。"""
    if not md:
        return []
    lines = md.split("\n")
    blocks: List[Block] = []
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # ---- 代码块 ----
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            lang = stripped[3:].strip()
            code_lines = [line]
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                code_lines.append(lines[i])
                i += 1
            blocks.append(Block(
                type=BLOCK_CODE,
                content="\n".join(code_lines),
                meta={"lang": lang},
            ))
            continue

        # ---- 标题 ----
        if stripped.startswith("#"):
            level = 0
            for ch in stripped:
                if ch == "#":
                    level += 1
                else:
                    break
            if 1 <= level <= 6 and len(stripped) > level:
                title = stripped[level:].strip()
                blocks.append(Block(type=BLOCK_HEADING, content=title, level=level))
                i += 1
                continue

        # ---- 表格：当前行含 |，下一行是分隔行 ----
        if "|" in stripped and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            table_lines = [line]
            i += 1
            while i < n and "|" in lines[i] and lines[i].strip():
                table_lines.append(lines[i])
                i += 1
            blocks.append(Block(type=BLOCK_TABLE, content="\n".join(table_lines)))
            continue

        # ---- 引用 ----
        if stripped.startswith(">"):
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(Block(type=BLOCK_QUOTE, content="\n".join(quote_lines)))
            continue

        # ---- 列表 ----
        if _LIST_RE.match(line):
            list_lines = []
            while i < n and lines[i].strip() and (
                _LIST_RE.match(lines[i]) or lines[i].startswith(("  ", "\t"))
            ):
                list_lines.append(lines[i])
                i += 1
            blocks.append(Block(type=BLOCK_LIST, content="\n".join(list_lines)))
            continue

        # ---- 普通段落（连续非空行，直到遇到下一个块起点）----
        para_lines = []
        while i < n and lines[i].strip() and not _is_block_start(lines[i]):
            para_lines.append(lines[i])
            i += 1
        if para_lines:
            blocks.append(Block(type=BLOCK_PARAGRAPH, content="\n".join(para_lines)))
        else:
            # 保险：避免死循环
            i += 1

    return blocks


def build_sections(blocks: List[Block]) -> List[Section]:
    """把 Block 序列按标题层级组装成 Section。"""
    sections: List[Section] = []
    stack = {}  # level -> title
    current = Section(heading_path="", level=0, blocks=[])

    for b in blocks:
        if b.type == BLOCK_HEADING:
            # 收尾当前 section
            if current.blocks or current.heading_path:
                sections.append(current)
            # 维护标题栈
            stack[b.level] = b.content
            for lv in list(stack.keys()):
                if lv > b.level:
                    del stack[lv]
            path = " > ".join(stack[lv] for lv in sorted(stack.keys()))
            current = Section(heading_path=path, level=b.level, blocks=[])
        else:
            current.blocks.append(b)

    if current.blocks or current.heading_path:
        sections.append(current)
    return sections


def render_block(b: Block) -> str:
    """把 Block 还原成 Markdown 文本，用于拼接父块/子块。"""
    if b.type == BLOCK_HEADING:
        return "#" * b.level + " " + b.content
    return b.content


def estimate_tokens(text: str) -> int:
    """粗略估算 token：中文 ~0.6/字，其他 ~0.3/字符。"""
    if not text:
        return 0
    zh = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - zh
    return int(zh * 0.6 + other * 0.3) + 1