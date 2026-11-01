"""段落切分规则（GLOSSARY：段落）——纯函数的边界行为。

覆盖 issue #16 验收的切分边界：空段丢弃、超长段在句读处切分（无句读
硬切）、切分结果任何单段不超上限、内容不丢失。只测 :func:`split_segments`
的公开契约，不触数据库。
"""

from app.optimization import MAX_SEGMENT_CHARS, split_segments


def test_empty_and_whitespace_only_text_yields_no_segments() -> None:
    assert split_segments("") == []
    assert split_segments("   \n\n \t\n  ") == []


def test_blank_lines_are_dropped_and_order_preserved() -> None:
    text = "\n\n第一段。\n\n   \n第二段。\n\n\n第三段。\n"
    assert split_segments(text) == ["第一段。", "第二段。", "第三段。"]


def test_paragraph_surrounding_whitespace_is_stripped() -> None:
    assert split_segments("  缩进段内容。  ") == ["缩进段内容。"]


def test_short_paragraph_stays_whole() -> None:
    paragraph = "短" * MAX_SEGMENT_CHARS
    assert split_segments(paragraph) == [paragraph]


def test_overlong_paragraph_splits_at_last_sentence_boundary() -> None:
    # 每句 10 字符（含句号），5 句共 50 字；上限 24 → 在句读处切成 ≤24 的块
    paragraph = "abcdefghij。" * 5
    chunks = split_segments(paragraph, max_chars=24)
    assert all(len(chunk) <= 24 for chunk in chunks)
    assert "".join(chunks) == paragraph
    # 每块都应在句号处结束（句读切分而非硬切）
    assert all(chunk.endswith("。") for chunk in chunks)


def test_overlong_paragraph_without_punctuation_is_hard_cut() -> None:
    paragraph = "字" * 100
    chunks = split_segments(paragraph, max_chars=30)
    assert chunks == ["字" * 30, "字" * 30, "字" * 30, "字" * 10]
    assert "".join(chunks) == paragraph


def test_default_limit_holds_for_any_input() -> None:
    paragraph = ("甲乙丙丁戊己庚辛壬癸。" * 300) + "尾"
    chunks = split_segments(paragraph)
    assert all(len(chunk) <= MAX_SEGMENT_CHARS for chunk in chunks)
    assert "".join(chunks) == paragraph
