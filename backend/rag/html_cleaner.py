# backend/rag/html_cleaner.py
import re


def clean_html_to_markdown(html_text: str) -> str:
    """把富文本 HTML 粗糙地转成 Markdown，保留代码和标题结构。"""
    if not html_text:
        return ""

    text = html_text

    # 1. 提取并保留代码块
    # 假设前端代码块是 <pre><code class="language-python">...</code></pre>
    def replace_code(match):
        lang = ""
        class_attr = match.group(1)
        if class_attr:
            lang_match = re.search(r'language-([a-zA-Z0-9]+)', class_attr)
            if lang_match:
                lang = lang_match.group(1)
        code_content = match.group(2)
        # 简单反转义
        code_content = code_content.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        return f"\n```{lang}\n{code_content}\n```\n"

    text = re.sub(r'<pre><code(?:\s+class="([^"]*)")?>(.*?)</code></pre>', replace_code, text, flags=re.DOTALL)

    # 2. 替换标题
    for i in range(1, 7):
        text = re.sub(rf'<h{i}[^>]*>(.*?)</h{i}>', rf'\n{"#" * i} \1\n', text, flags=re.DOTALL)

    # 3. 替换段落与换行
    text = re.sub(r'<p[^>]*>', '\n\n', text)
    text = re.sub(r'</p>', '\n', text)
    text = re.sub(r'<br\s*/?>', '\n', text)

    # 4. 替换列表
    text = re.sub(r'<li[^>]*>', '\n- ', text)
    text = re.sub(r'<[uo]l[^>]*>', '\n', text)
    text = re.sub(r'</[uo]l>', '\n', text)

    # 5. 去掉剩余的 HTML 标签（比如 <strong>, <span> 等）
    text = re.sub(r'<[^>]+>', '', text)

    # 6. 反转义常用的 HTML 实体
    text = text.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;",
                                                                                                               '"')

    # 7. 清理多余的空行
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()