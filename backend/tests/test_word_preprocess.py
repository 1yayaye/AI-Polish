"""文章预处理纵切（issue #35）：创建（503/422/403）→ 预留 → Worker 分块标记
→ 结果回读与失败释放的全链行为。

主接缝是 HTTP API 层（TestClient + X-Card-Key header）与 Worker 单步推进，
照 test_word_jobs 先例只测外部行为：LLM 依赖经 Worker 的 marking_ai 接缝注入
脚本化假客户端（test_word_preprocessor 的接缝，不 mock 内部函数）；模型接入
配置走 settings monkeypatch（生产 = .env，ADR-0005）。分块进度流的行为在
test_word_stream 以真 uvicorn 验证，本文件不重复。
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import billing, word_jobs
from app.config import settings
from app.db import short_tx
from app.models import FormatJob, LedgerEntry, User
from app.word_worker import WordJobWorker

from test_billing import assert_ledger_reconciles
from test_word_jobs import _usage_rows, usage_user, word_output_dir  # noqa: F401
from test_word_preprocessor import ScriptedMarkingLLM, _marking

SAMPLE_TEXT = "第一章 绪论\n\n这是正文段落。\n\n第二章 方法\n\n正文二。"
# 12 段正文 × 每块 10 段 = 2 个分块（分块参数护栏下限 10，老版同款）
TWO_CHUNK_TEXT = "\n\n".join(f"第{i}段正文内容。" for i in range(12))
TWO_CHUNK_MARKINGS = [
    _marking([(0, "heading_1")]),
    _marking([(11, "table_caption")]),
]


@pytest.fixture()
def configured_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型接入三项齐备（生产从 .env 读取，ADR-0005）；本仓库测试环境无
    .env，默认未配置——创建端点默认 503，需要真实创建的测试先挂本夹具。"""
    monkeypatch.setattr(settings, "llm_base_url", "https://llm.example.com/v1")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "test-model")


def _create_job(
    client: TestClient,
    user: User,
    *,
    text: str = TWO_CHUNK_TEXT,
    chunk_paragraphs: int = 10,
    chunk_chars: int = 8000,
) -> int:
    response = client.post(
        "/api/word-formatter/preprocess/text",
        json={
            "text": text,
            "chunk_paragraphs": chunk_paragraphs,
            "chunk_chars": chunk_chars,
        },
        headers={"X-Card-Key": user.card_key},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ---- API 全链：创建 → 预留 → Worker 分块标记 → 结果回读 ----


def test_full_chain_create_reserve_mark_result(
    client: TestClient,
    usage_user: Callable[..., User],
    configured_llm: None,
    word_output_dir: Path,
    db_session_factory: sessionmaker,
) -> None:
    user = usage_user(usage_limit=3)
    job_id = _create_job(client, user)

    # 创建即预留：账本 usage_reserve 行带 task_id
    rows = _usage_rows(db_session_factory, user.id)
    assert [(r.kind, r.task_id, r.usage_after) for r in rows] == [
        (billing.KIND_USAGE_RESERVE, job_id, 2)
    ]
    assert_ledger_reconciles(db_session_factory, user.id)

    # Worker 推进：假 LLM 按分块返回标记，任务完成、结果落盘 JSON
    llm = ScriptedMarkingLLM(list(TWO_CHUNK_MARKINGS))
    assert asyncio.run(WordJobWorker(db_session_factory, marking_ai=llm).process_next()) is True
    assert len(llm.calls) == 2  # 4 段 × 每块 2 段 = 2 个分块

    job = db_session_factory()
    try:
        row = job.get(FormatJob, job_id)
        assert row.status == "completed"
        assert row.output_path == f"{job_id}.json"
        assert row.output_filename is None  # 无交付文件名：结果经 result 端点回读
    finally:
        job.close()
    artifact = word_jobs.artifact_path(f"{job_id}.json")
    assert artifact.is_file()

    # 结果端点：标记文本、段落类型统计、一致性校验结论
    response = client.get(
        f"/api/word-formatter/preprocess/{job_id}/result",
        headers={"X-Card-Key": user.card_key},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["integrity_check_passed"] is True
    assert body["warnings"] == []
    assert body["type_statistics"] == {"heading_1": 1, "body": 10, "table_caption": 1}
    assert body["marked_text"].startswith("<!-- wf:type=heading_1 -->\n第0段正文内容。")
    assert [p["paragraph_type"] for p in body["paragraphs"]] == [
        "heading_1",
        *(["body"] * 10),
        "table_caption",
    ]

    # 成功不释放：次数消耗为 1（账本仍只有那行 reserve）
    assert [(r.kind, r.task_id) for r in _usage_rows(db_session_factory, user.id)] == [
        (billing.KIND_USAGE_RESERVE, job_id)
    ]


def test_create_is_503_without_model_config_with_zero_side_effects(
    client: TestClient,
    usage_user: Callable[..., User],
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: sessionmaker,
) -> None:
    """模型接入未配置（ADR-0005）：创建即 503 明确失败——零任务、零预留。"""
    monkeypatch.setattr(settings, "llm_base_url", "")
    user = usage_user(usage_limit=3)
    headers = {"X-Card-Key": user.card_key}

    response = client.post(
        "/api/word-formatter/preprocess/text",
        json={"text": SAMPLE_TEXT},
        headers=headers,
    )
    assert response.status_code == 503, response.text

    response = client.post(
        "/api/word-formatter/preprocess/file",
        files={"file": ("论文.txt", SAMPLE_TEXT.encode("utf-8"), "text/plain")},
        headers=headers,
    )
    assert response.status_code == 503

    session = db_session_factory()
    try:
        assert session.execute(select(FormatJob.id)).scalars().all() == []
        kinds = session.execute(select(LedgerEntry.kind)).scalars().all()
        assert kinds == [billing.KIND_ISSUE]  # 仅存发卡行：零预留
    finally:
        session.close()


def test_create_with_insufficient_usage_is_403_without_side_effects(
    client: TestClient,
    usage_user: Callable[..., User],
    configured_llm: None,
    db_session_factory: sessionmaker,
) -> None:
    user = usage_user(usage_limit=0)
    response = client.post(
        "/api/word-formatter/preprocess/text",
        json={"text": SAMPLE_TEXT},
        headers={"X-Card-Key": user.card_key},
    )
    assert response.status_code == 403
    session = db_session_factory()
    try:
        assert session.execute(select(FormatJob.id)).scalars().all() == []
        kinds = session.execute(select(LedgerEntry.kind)).scalars().all()
        assert kinds == [billing.KIND_ISSUE]
    finally:
        session.close()


def test_create_validates_text_and_chunk_params(
    client: TestClient,
    usage_user: Callable[..., User],
    configured_llm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = usage_user()
    headers = {"X-Card-Key": user.card_key}

    # 空文本 422（与格式化共用 validated_text 护栏）
    assert (
        client.post(
            "/api/word-formatter/preprocess/text",
            json={"text": "   \n "},
            headers=headers,
        ).status_code
        == 422
    )
    # 分块参数越界 422（挡在预留之前）
    for payload in (
        {"text": SAMPLE_TEXT, "chunk_paragraphs": 5},
        {"text": SAMPLE_TEXT, "chunk_paragraphs": 101},
        {"text": SAMPLE_TEXT, "chunk_chars": 1000},
        {"text": SAMPLE_TEXT, "chunk_chars": 20000},
    ):
        response = client.post(
            "/api/word-formatter/preprocess/text", json=payload, headers=headers
        )
        assert response.status_code == 422, payload

    # 超上限文本 413（M4-T7）：放最后——低上限对本测试其余断言是噪声
    monkeypatch.setattr(settings, "max_word_text_chars", 10)
    assert (
        client.post(
            "/api/word-formatter/preprocess/text",
            json={"text": "这一段正文的长度超过了十个字的上限"},
            headers=headers,
        ).status_code
        == 413
    )


def test_file_upload_routes_and_guards(
    client: TestClient,
    usage_user: Callable[..., User],
    configured_llm: None,
    db_session_factory: sessionmaker,
) -> None:
    """上传路径：txt/md/docx 解码/提文本与格式化共用同一护栏（422 挡在
    预留之前），分块参数走表单字段。"""
    user = usage_user()
    headers = {"X-Card-Key": user.card_key}

    response = client.post(
        "/api/word-formatter/preprocess/file",
        files={"file": ("论文.txt", SAMPLE_TEXT.encode("utf-8"), "text/plain")},
        data={"chunk_paragraphs": "20", "chunk_chars": "4000"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    created_id = response.json()["id"]
    job = db_session_factory()
    try:
        row = job.get(FormatJob, response.json()["id"])
        assert row.input_text == SAMPLE_TEXT
        assert row.input_file_name == "论文.txt"
        assert row.options == {"chunk_paragraphs": 20, "chunk_chars": 4000}
    finally:
        job.close()

    response = client.post(
        "/api/word-formatter/preprocess/file",
        files={"file": ("论文.pdf", b"%PDF-1.4 fake", "application/pdf")},
        headers=headers,
    )
    assert response.status_code == 422  # 不支持的后缀

    response = client.post(
        "/api/word-formatter/preprocess/file",
        files={"file": ("损坏.docx", b"not a zip", "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 422  # 损坏的 docx

    response = client.post(
        "/api/word-formatter/preprocess/file",
        files={"file": ("空白.txt", b"   \n ", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 422  # 解码后空文本

    assert (
        client.post(
            "/api/word-formatter/preprocess/file",
            files={"file": ("论文.txt", SAMPLE_TEXT.encode("utf-8"), "text/plain")},
        ).status_code
        == 401
    )
    # 后续守卫全部挡在预留之前：账本仍只有成功那一单的 reserve 行
    rows = _usage_rows(db_session_factory, user.id)
    assert [(r.kind, r.task_id) for r in rows] == [(billing.KIND_USAGE_RESERVE, created_id)]


# ---- 失败收场：预留次数释放 ----


def test_artifact_failure_fails_job_and_releases_usage(
    client: TestClient,
    usage_user: Callable[..., User],
    configured_llm: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: sessionmaker,
) -> None:
    """结果落盘失败（数据目录不可用）按内容性失败收场：failed + 释放预留。"""
    blocker = tmp_path / "word_jobs"
    blocker.write_text("占位：同路径的文件让产物目录 mkdir 失败")
    monkeypatch.setattr(settings, "word_output_dir", str(blocker))
    user = usage_user(usage_limit=2)
    job_id = _create_job(client, user)

    llm = ScriptedMarkingLLM(list(TWO_CHUNK_MARKINGS))
    assert asyncio.run(WordJobWorker(db_session_factory, marking_ai=llm).process_next()) is True

    job = db_session_factory()
    try:
        row = job.get(FormatJob, job_id)
        assert row.status == "failed"
        assert "落盘失败" in row.error
    finally:
        job.close()
    rows = _usage_rows(db_session_factory, user.id)
    assert [(r.kind, r.task_id) for r in rows] == [
        (billing.KIND_USAGE_RESERVE, job_id),
        (billing.KIND_USAGE_RELEASE, job_id),
    ]
    assert_ledger_reconciles(db_session_factory, user.id)


def test_empty_text_job_fails_and_releases_usage(
    usage_user: Callable[..., User],
    configured_llm: None,
    word_output_dir: Path,
    db_session_factory: sessionmaker,
) -> None:
    """无法分段的内容性失败（经任务服务直接建任务绕过 HTTP 校验的等价物：
    上一进程写入的脏数据）同样 failed + 释放。"""
    user = usage_user(usage_limit=2)
    with short_tx(db_session_factory) as session:
        job = word_jobs.create_format_job(
            session,
            user,
            job_type=word_jobs.JOB_PREPROCESS,
            text="  \n\n  ",  # 分段后为空：创建校验拦不住的历史脏数据
            options={"chunk_paragraphs": 40, "chunk_chars": 8000},
        )
        job_id = job.id

    assert asyncio.run(
        WordJobWorker(db_session_factory, marking_ai=ScriptedMarkingLLM([])).process_next()
    ) is True

    job = db_session_factory()
    try:
        row = job.get(FormatJob, job_id)
        assert row.status == "failed"
        assert "文章内容为空" in row.error
    finally:
        job.close()
    rows = _usage_rows(db_session_factory, user.id)
    assert [(r.kind, r.task_id) for r in rows] == [
        (billing.KIND_USAGE_RESERVE, job_id),
        (billing.KIND_USAGE_RELEASE, job_id),
    ]


# ---- 结果端点守卫：401/404/409 ----


def test_result_guards(
    client: TestClient,
    usage_user: Callable[..., User],
    make_user: Callable[..., User],
    configured_llm: None,
    word_output_dir: Path,
    db_session_factory: sessionmaker,
) -> None:
    alice = usage_user(card_key="card-alice", usage_limit=5)
    bob = usage_user(card_key="card-bob", usage_limit=5)
    job_id = _create_job(client, alice)
    other_headers = {"X-Card-Key": bob.card_key}
    alice_headers = {"X-Card-Key": alice.card_key}

    assert client.get(f"/api/word-formatter/preprocess/{job_id}/result").status_code == 401
    assert client.get(
        f"/api/word-formatter/preprocess/{job_id}/result", headers=other_headers
    ).status_code == 404  # 他人任务不泄露存在性
    assert client.get(
        f"/api/word-formatter/preprocess/{job_id}/result", headers=alice_headers
    ).status_code == 409  # 未完成

    # 格式化任务没有预处理结果：类型错位按不存在处理
    with short_tx(db_session_factory) as session:
        format_job = word_jobs.create_format_job(
            session, alice, job_type=word_jobs.JOB_FORMAT, text="正文。", options=None
        )
        format_job_id = format_job.id
    assert client.get(
        f"/api/word-formatter/preprocess/{format_job_id}/result", headers=alice_headers
    ).status_code == 404

    # 完成（真跑一遍）后结果可读；文件被清后 404
    assert asyncio.run(
        WordJobWorker(
            db_session_factory, marking_ai=ScriptedMarkingLLM(list(TWO_CHUNK_MARKINGS))
        ).process_next()
    ) is True
    assert client.get(
        f"/api/word-formatter/preprocess/{job_id}/result", headers=alice_headers
    ).status_code == 200
    word_jobs.artifact_path(f"{job_id}.json").unlink()
    assert client.get(
        f"/api/word-formatter/preprocess/{job_id}/result", headers=alice_headers
    ).status_code == 404


# ---- options → 配置（消费方词汇：键名即 PreprocessConfig 字段名） ----


def test_preprocess_config_for_options_defaults() -> None:
    from app.word_preprocess import preprocess_config_for

    config = preprocess_config_for({"chunk_paragraphs": 12, "chunk_chars": 4000})
    assert (config.chunk_paragraphs, config.chunk_chars) == (12, 4000)
    fallback = preprocess_config_for(None)
    assert (fallback.chunk_paragraphs, fallback.chunk_chars) == (40, 8000)


def test_result_artifact_round_trip(
    usage_user: Callable[..., User],
    word_output_dir: Path,
    db_session_factory: sessionmaker,
) -> None:
    """落盘 → 回读往返保真（结果 JSON 的结构即响应契约，不落库）。"""
    from app.word_services import PreprocessResult
    from app.word_preprocess import read_result_artifact, write_result_artifact

    result = PreprocessResult(
        success=True,
        marked_text="<!-- wf:type=body -->\n正文",
        type_statistics={"body": 1},
        integrity_check_passed=True,
        warnings=["分块 1 AI 标记失败: 注入，使用规则识别"],
    )
    write_result_artifact(42, result)
    payload = read_result_artifact(42)
    assert payload is not None
    assert payload["marked_text"] == "<!-- wf:type=body -->\n正文"
    assert payload["paragraphs"] == []
    assert payload["type_statistics"] == {"body": 1}
    assert payload["integrity_check_passed"] is True
    assert "AI 标记失败" in payload["warnings"][0]
