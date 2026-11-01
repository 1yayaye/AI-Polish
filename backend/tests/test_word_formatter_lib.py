"""word_formatter 渲染库冒烟（issue #21，ADR-0004 黑盒移植）。

只从库的公开接口（app.word_formatter.compile_document）驱动：markdown /
纯文本进、docx 字节流出，用 python-docx 回读校验段落内容与样式——不探测
内部函数。渲染细节（封面、目录、样式映射）由移植前的旧测试体系拥有，这里
守的是"移植后管线仍然端到端可用"。
"""

import io
import sys
from pathlib import Path

from docx import Document

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.word_formatter import (  # noqa: E402
    CompileOptions,
    InputFormat,
    compile_document,
    detect_input_format,
)


def _nonempty_paragraphs(docx_bytes: bytes) -> list[str]:
    doc = Document(io.BytesIO(docx_bytes))
    return [p.text for p in doc.paragraphs if p.text.strip()]


def _compile(text: str, input_format: InputFormat) -> bytes:
    result = compile_document(text, CompileOptions(input_format=input_format))
    assert result.success, result.error
    assert result.docx_bytes
    return result.docx_bytes


def test_markdown_compile_headings_and_body_reachable() -> None:
    docx_bytes = _compile(
        "# 绪论\n\n第一段正文内容。\n\n第二段正文内容。\n",
        InputFormat.MARKDOWN,
    )
    doc = Document(io.BytesIO(docx_bytes))
    styles = {p.text: p.style.name for p in doc.paragraphs if p.text.strip()}
    assert styles["绪论"] == "H1"
    assert styles["第一段正文内容。"] == "Body"
    assert styles["第二段正文内容。"] == "Body"
    # 注：ATX 多级标题（## → H2）在 mistune 3.x AST 下塌缩为 H1，属移植
    # 原实现的既有行为（ADR-0004 内部逻辑不改），冒烟不钉死该怪癖；
    # 编号标题的多级映射走纯文本路径，见下一条测试。


def test_plaintext_compile_paragraphs_in_order() -> None:
    docx_bytes = _compile(
        "第一段正文内容。\n\n第二段正文内容。\n\n第三段正文内容。\n",
        InputFormat.PLAINTEXT,
    )
    # 纯文本按空行分段：除目录占位两行外，段落内容与顺序和输入一致
    assert _nonempty_paragraphs(docx_bytes) == [
        "目 录",
        "（在 Word 中右键目录 → 更新域）",
        "第一段正文内容。",
        "第二段正文内容。",
        "第三段正文内容。",
    ]


def test_plaintext_numbered_lines_become_headings() -> None:
    docx_bytes = _compile(
        "1 绪论\n\n研究背景与意义正文。\n\n1.1 研究背景\n\n背景正文。\n",
        InputFormat.PLAINTEXT,
    )
    doc = Document(io.BytesIO(docx_bytes))
    styles = {p.text: p.style.name for p in doc.paragraphs if p.text.strip()}
    assert styles["绪论"] == "H1"
    assert styles["研究背景"] == "H2"


def test_detect_input_format() -> None:
    # 两个 Markdown 指示符（"# " 与 "## "）才判 Markdown，单一 "#" 不够
    assert detect_input_format("# 标题\n\n## 小节\n\n正文") is InputFormat.MARKDOWN
    assert detect_input_format("普通论文正文段落。") is InputFormat.PLAINTEXT
