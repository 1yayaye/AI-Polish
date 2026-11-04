"""格式检查器（issue #32，黑盒随迁自旧版）。

只从公开接口（app.word_services.check_format）驱动：纯规则、同步，宽松/严格
两档的差异与代表性规则命中从检查结果断言。同时验证检查器接受 docx 提取的
纯文本（与 :func:`extract_text_from_docx` 的衔接）。
"""

import io

from docx import Document

from app.word_services import IssueSeverity, IssueType, check_format, extract_text_from_docx

# 覆盖代表性规则的样例：标题跳跃 / 缺摘要 / 缺关键词 / 段落过长 / 参考文献编号不连续
SAMPLE_TEXT = (
    "# 一级标题\n"
    "\n"
    "正文一段。\n"
    "\n"
    "### 三级标题\n"
    "\n"
    f"{'字' * 1001}\n"
    "\n"
    "## 参考文献\n"
    "\n"
    "[1] 张三. 某论文[J]. 某期刊.\n"
    "\n"
    "[3] 李四. 另一篇[J]. 另一期刊.\n"
)

NON_ERROR_ISSUES = {
    IssueType.HEADING_SKIP,      # WARNING：宽松档只留 ERROR，全部被过滤
    IssueType.MISSING_ABSTRACT,  # INFO
    IssueType.MISSING_KEYWORDS,  # INFO
    IssueType.LONG_PARAGRAPH,    # INFO
    IssueType.REFERENCE_FORMAT,  # WARNING
}


def test_strict_mode_reports_all_representative_rule_hits() -> None:
    result = check_format(SAMPLE_TEXT, mode="strict")

    assert result.success is True
    assert result.mode.value == "strict"
    hit_types = {i.issue_type for i in result.issues}
    assert NON_ERROR_ISSUES <= hit_types
    # 明细可定位：标题跳跃报在后一个标题的行号上
    skip = next(i for i in result.issues if i.issue_type is IssueType.HEADING_SKIP)
    assert skip.severity is IssueSeverity.WARNING
    assert "从1级直接到3级" in skip.message
    assert skip.line == 5
    # 段落类型字典统计：Markdown 标题规则先于中文章节规则，"## 参考文献"归二级
    assert result.type_statistics["heading_1"] == 1
    assert result.type_statistics["heading_2"] == 1
    assert result.type_statistics["body"] == 2
    assert result.type_statistics["reference"] == 2


def test_loose_mode_filters_non_error_issues() -> None:
    strict = check_format(SAMPLE_TEXT, mode="strict")
    loose = check_format(SAMPLE_TEXT, mode="loose")

    assert loose.success is True
    assert loose.mode.value == "loose"
    # 现行规则无 ERROR 级：宽松档不报任何问题，严格档全报
    assert loose.issues == []
    assert strict.issues != []


def test_existing_marker_is_reported_and_preserved() -> None:
    text = "<!-- wf:type=body -->\n已带标记的正文。\n\n关键词：重构；测试\n"

    result = check_format(text, mode="strict")  # 已有标记属 INFO，宽松档不报

    markers = [i for i in result.issues if i.issue_type is IssueType.EXISTING_MARKER]
    assert len(markers) == 1
    assert markers[0].line == 1
    # 已有标记被识别并保留，不再按规则重判
    body = next(p for p in result.paragraphs if p.text == "已带标记的正文。")
    assert body.paragraph_type == "body"
    assert body.is_auto_detected is False
    assert result.type_statistics.get("keywords_cn") == 1


def test_missing_abstract_and_keywords_are_reported() -> None:
    result = check_format("正文而已。\n", mode="strict")

    hit = {i.issue_type for i in result.issues}
    assert IssueType.MISSING_ABSTRACT in hit
    assert IssueType.MISSING_KEYWORDS in hit


def test_empty_text_is_rejected_without_issues() -> None:
    result = check_format("   \n\n  ")

    assert result.success is False
    assert result.is_valid is False
    assert result.error == "文章内容为空"
    assert result.issues == []


def test_accepts_text_extracted_from_docx() -> None:
    doc = Document()
    doc.add_paragraph("摘要：本文验证检查器接受 docx 提取文本。")
    doc.add_paragraph("第一章 绪论")
    doc.add_paragraph("正文内容。")
    buffer = io.BytesIO()
    doc.save(buffer)

    text = extract_text_from_docx(buffer.getvalue())
    result = check_format(text, mode="strict")

    assert result.success is True
    # 提取文本以单换行连接（无空行）：整体成一段，按首行规则识别类型
    assert result.type_statistics == {"abstract_cn": 1}
    assert result.paragraphs[0].text.count("\n") == 2
