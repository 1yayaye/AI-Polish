"""格式检查纵切（issue #37）：types 字典、text/file × 宽松/严格两档的检查
行为，以及免费语义的钉子（零任务行、零账本行）。

主接缝是 HTTP API 层（TestClient + X-Card-Key header），照 test_word_jobs /
test_word_preprocess 先例只测外部行为；检查是纯规则同步调用，LLM 接缝与
Worker 都不在其路径上（这正是"免费"的结构保证，测试从账本与任务表侧钉住）。
"""

import io
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import billing
from app.models import FormatJob, LedgerEntry, User
from app.word_services import PARAGRAPH_TYPES

from test_word_jobs import usage_user  # noqa: F401

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


def _headers(user: User) -> dict[str, str]:
    return {"X-Card-Key": user.card_key}


def _check_text(client: TestClient, user: User, text: str, mode: str = "loose"):
    return client.post(
        "/api/word-formatter/format-check/text",
        json={"text": text, "mode": mode},
        headers=_headers(user),
    )


def _check_file(client: TestClient, user: User, name: str, content: bytes, mode: str = "loose"):
    return client.post(
        "/api/word-formatter/format-check/file",
        files={"file": (name, content, "application/octet-stream")},
        data={"mode": mode},
        headers=_headers(user),
    )


# ---- 免费语义钉子：检查全程零任务行、零账本行 ----


def _assert_zero_side_effects(
    db_session_factory: sessionmaker, user: User
) -> None:
    """任务表为空、账本只有发卡行、次数轨余量原封不动（对照：格式化/预处理
    创建即 reserve——检查刻意不这样做）。"""
    session = db_session_factory()
    try:
        assert session.execute(select(FormatJob.id)).scalars().all() == []
        assert (
            session.execute(select(LedgerEntry.kind).where(LedgerEntry.user_id == user.id))
            .scalars()
            .all()
            == [billing.KIND_ISSUE]  # 仅存发卡行
        )
        used, limit = session.execute(
            select(User.usage_used, User.usage_limit).where(User.id == user.id)
        ).one()
        assert (used, limit) == (0, 5)
    finally:
        session.close()


def test_check_is_free_zero_jobs_zero_ledger(
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    """text/file 两路 × 两档全部走完，账本与任务表仍零变化（免费被测试钉住）。"""
    user = usage_user(usage_limit=5)
    gbk_bytes = SAMPLE_TEXT.encode("gbk")

    for mode in ("loose", "strict"):
        assert _check_text(client, user, SAMPLE_TEXT, mode).status_code == 200
        assert _check_file(client, user, "论文.txt", gbk_bytes, mode).status_code == 200

    _assert_zero_side_effects(db_session_factory, user)


# ---- types 字典 ----


def test_types_returns_paragraph_type_dictionary(
    client: TestClient, usage_user: Callable[..., User]
) -> None:
    user = usage_user()
    response = client.get("/api/word-formatter/format-check/types", headers=_headers(user))
    assert response.status_code == 200, response.text

    types = response.json()["types"]
    # 字典即检查器词表（不是复制的第二份词汇）：条目数与正文/标题条目对齐
    assert len(types) == len(PARAGRAPH_TYPES)
    by_type = {t["type"]: t["label"] for t in types}
    for name, label in PARAGRAPH_TYPES.items():
        assert by_type[name] == label
    assert by_type["body"] == "正文"

    assert (
        client.get("/api/word-formatter/format-check/types").status_code == 401
    )  # 无卡密


# ---- text 路：两档差异与响应结构（行号/严重度/问题类型） ----


def test_text_check_strict_reports_all_with_line_severity_type(
    client: TestClient, usage_user: Callable[..., User]
) -> None:
    user = usage_user()
    response = _check_text(client, user, SAMPLE_TEXT, mode="strict")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["is_valid"] is True  # 现行规则全为 WARNING/INFO，无 ERROR 级
    assert body["mode"] == "strict"
    hit_types = {i["issue_type"] for i in body["issues"]}
    assert {
        "heading_skip",
        "missing_abstract",
        "missing_keywords",
        "long_paragraph",
        "reference_format",
    } <= hit_types
    # 响应结构含行号/严重度/问题类型：标题跳跃报在后一个标题的行号上
    skip = next(i for i in body["issues"] if i["issue_type"] == "heading_skip")
    assert skip["severity"] == "warning"
    assert skip["line"] == 5
    assert "从1级直接到3级" in skip["message"]
    assert skip["suggestion"] != ""
    # 段落类型统计随响应返回（页面的类型分布展示）
    assert body["type_statistics"]["reference"] == 2


def test_text_check_loose_filters_non_errors(
    client: TestClient, usage_user: Callable[..., User]
) -> None:
    """两档差异可见：宽松档只留 ERROR 级（现行规则无），严格档全报。"""
    user = usage_user()
    loose = _check_text(client, user, SAMPLE_TEXT, mode="loose").json()
    strict = _check_text(client, user, SAMPLE_TEXT, mode="strict").json()

    assert loose["mode"] == "loose"
    assert loose["issues"] == []
    assert strict["issues"] != []

    # 带 ERROR 级问题的构造：已有 wf:type 标记之外，现有规则集无 ERROR——
    # 宽松档非空的等价证据：严格档含非 ERROR 明细且宽松档不含
    assert all(i["severity"] != "error" for i in strict["issues"])


# ---- file 路：上传护栏同格式化 ----


def test_file_check_routes_and_guards(
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    user = usage_user()
    headers = _headers(user)

    # GBK 编码的 txt 正确解码（Windows 记事本同款）
    response = _check_file(client, user, "论文.txt", SAMPLE_TEXT.encode("gbk"), mode="strict")
    assert response.status_code == 200, response.text
    assert response.json()["issues"] != []

    # docx 提文本后检查（缺摘要/缺关键词的 INFO 级）
    from docx import Document

    doc = Document()
    doc.add_paragraph("第一章 绪论")
    doc.add_paragraph("正文内容。")
    buffer = io.BytesIO()
    doc.save(buffer)
    response = _check_file(client, user, "论文.docx", buffer.getvalue(), mode="strict")
    assert response.status_code == 200, response.text
    hit_types = {i["issue_type"] for i in response.json()["issues"]}
    assert {"missing_abstract", "missing_keywords"} <= hit_types

    # 不支持的后缀 / 损坏的 docx / 解码后空文本：422 挡在检查之前
    assert _check_file(client, user, "论文.pdf", b"%PDF-1.4 fake").status_code == 422
    assert _check_file(client, user, "损坏.docx", b"not a zip").status_code == 422
    assert _check_file(client, user, "空白.txt", b"   \n ").status_code == 422

    assert (
        client.post(
            "/api/word-formatter/format-check/file",
            files={"file": ("论文.txt", SAMPLE_TEXT.encode("utf-8"), "text/plain")},
        ).status_code
        == 401
    )
    _assert_zero_side_effects(db_session_factory, user)


# ---- 输入护栏与参数校验 ----


def test_check_input_guards(
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "max_word_text_chars", 100)
    user = usage_user()
    headers = _headers(user)

    # 空文本 / 非法档位：422；超上限：413（M4-T7，与 Nginx 第二道防线同码）
    assert _check_text(client, user, "   \n ").status_code == 422
    assert _check_text(client, user, "字" * 101).status_code == 413
    assert (
        client.post(
            "/api/word-formatter/format-check/text",
            json={"text": "正文。", "mode": "pedantic"},
            headers=headers,
        ).status_code
        == 422
    )
    _assert_zero_side_effects(db_session_factory, user)
