"""docx 文本提取（issue #32，黑盒随迁自旧版）。

只从公开接口（app.word_services.extract_text_from_docx）驱动：用 python-docx
现场构造最小文档（段落 + 表格），断言段落按文档序、表格文本随后按行主序的
既有语义，以及字节与文件路径两种输入形态。
"""

import io
from pathlib import Path

from docx import Document

from app.word_services import extract_text_from_docx


def _docx_bytes(paragraphs: list[str], rows: list[list[str]] | None = None) -> bytes:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    if rows:
        table = doc.add_table(rows=len(rows), cols=len(rows[0]))
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                table.cell(r, c).text = cell
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def test_extracts_paragraphs_then_table_cells_in_order() -> None:
    docx_bytes = _docx_bytes(
        ["第一段", "第二段"],
        [["表头甲", "表头乙"], ["数据丙", "数据丁"]],
    )

    text = extract_text_from_docx(docx_bytes)

    # 既有语义：顶层段落按文档序在前，表格单元格按行主序随后
    assert text == "第一段\n第二段\n表头甲\n表头乙\n数据丙\n数据丁"


def test_accepts_file_path_and_skips_empty_content() -> None:
    docx_bytes = _docx_bytes(
        ["有内容的段落", "   "],  # 纯空白段落剔除
        [["", "非空单元格"]],  # 空单元格剔除
    )
    path = Path(__file__).parent / "_tmp_extract.docx"
    path.write_bytes(docx_bytes)
    try:
        text = extract_text_from_docx(str(path))
    finally:
        path.unlink()

    assert text == "有内容的段落\n非空单元格"
