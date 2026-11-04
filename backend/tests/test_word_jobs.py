"""Word Formatter 任务地基（issue #33，spec #31）：文本格式化纵切的全链行为。

主接缝是 HTTP API 层（TestClient + X-Card-Key header）与 Worker 单步推进
（process_next），照 spec 测试决策只测外部行为、不 mock 内部函数：

- 创建即预留、编译失败自动释放、SUM(账本)==余额 与 usage_after 连续性
  （test_billing 先例的断言助手）；下载的鉴权与 404/409 语义。
- 编译接缝注入脚本化假编译器（成功/内容性失败/异常三路），真实渲染库
  编译只在全链用例走一次（docx 落盘与下载回读）。
- 启动恢复照恢复协议测试先例（test_recovery）：手工把任务拨成"上一进程
  死亡时停在中途"的状态，驱动 recover_interrupted 断言复位/失败释放。
"""

import asyncio
import io
import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

import app.main as app_main
import app.word_preprocess as word_preprocess
from app import billing, word_jobs
from app.config import settings
from app.db import short_tx
from app.models import FormatJob, LedgerEntry, User
from app.recovery import RecoveryReport
from app.word_formatter import CompileResult
from app.word_worker import WordJobWorker

from test_billing import assert_ledger_reconciles


@pytest.fixture()
def usage_user(
    make_user: Callable[..., User], db_session_factory: sessionmaker
) -> Callable[..., User]:
    """经计费服务入账次数轨的用户（余额轨为 0：本票全部行为不碰余额）。"""

    def _make(card_key: str = "card-word", usage_limit: int = 5) -> User:
        user = make_user(card_key=card_key)
        session = db_session_factory()
        try:
            billing.grant_initial_quota(
                session, user.id, balance_fen=0, usage_count=usage_limit, reason="发卡初始额度"
            )
            session.commit()
            return session.get(User, user.id)  # 入账后的额度字段回读（发卡前对象是零值）
        finally:
            session.close()

    return _make


@pytest.fixture()
def word_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """产物目录指向临时目录（生产从 .env 读取，ADR-0005；测试不写真实 data/）。"""
    target = tmp_path / "word_jobs"
    monkeypatch.setattr(settings, "word_output_dir", str(target))
    return target


class ScriptedCompiler:
    """脚本化假编译接缝：outcome 可为 CompileResult（返回）、Exception（抛出）
    或可调用（text/options → CompileResult）。每次调用的输入记入 calls。"""

    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, object]] = []

    def __call__(self, text: str, options: object) -> CompileResult:
        self.calls.append((text, options))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome(text, options)  # type: ignore[return-value]
        return self.outcome  # type: ignore[return-value]


def _ok_result(docx_bytes: bytes = b"fake-docx") -> CompileResult:
    return CompileResult(success=True, docx_bytes=docx_bytes)


# ---- 查询/造数助手：每个助手一个独立短连接，不与被测代码共享会话 ----


def _create_job(
    db_session_factory: sessionmaker,
    user: User,
    *,
    text: str = "第一段正文。\n\n第二段正文。",
    status: str = word_jobs.JOB_QUEUED,
    attempt_count: int = 0,
) -> int:
    """经任务服务创建任务（真实预留路径），再拨成指定状态（恢复测试的
    "上一进程死亡时停在中途"等价物）。"""
    with short_tx(db_session_factory) as session:
        job = word_jobs.create_format_job(
            session, user, job_type=word_jobs.JOB_FORMAT, text=text, options=None
        )
        job_id = job.id
    if status != word_jobs.JOB_QUEUED or attempt_count:
        _force_status(db_session_factory, job_id, status, attempt_count=attempt_count)
    return job_id


def _force_status(
    db_session_factory: sessionmaker,
    job_id: int,
    status: str,
    *,
    attempt_count: int | None = None,
) -> None:
    session = db_session_factory()
    try:
        values: dict[str, object] = {"status": status, "updated_at": func.current_timestamp()}
        if attempt_count is not None:
            values["attempt_count"] = attempt_count
        session.execute(update(FormatJob).where(FormatJob.id == job_id).values(**values))
        session.commit()
    finally:
        session.close()


def _force_updated_at(
    db_session_factory: sessionmaker, job_id: int, updated_at: datetime
) -> None:
    """把任务的 updated_at 拨到指定时刻（保留期测试的"终态早已落定"等价物）。"""
    session = db_session_factory()
    try:
        session.execute(
            update(FormatJob).where(FormatJob.id == job_id).values(updated_at=updated_at)
        )
        session.commit()
    finally:
        session.close()


def _job_row(db_session_factory: sessionmaker, job_id: int) -> FormatJob:
    session = db_session_factory()
    try:
        return session.get(FormatJob, job_id)
    finally:
        session.close()


def _usage_rows(db_session_factory: sessionmaker, user_id: int) -> list[LedgerEntry]:
    session = db_session_factory()
    try:
        return list(
            session.execute(
                select(LedgerEntry)
                .where(
                    LedgerEntry.user_id == user_id,
                    LedgerEntry.kind.in_(
                        [billing.KIND_USAGE_RESERVE, billing.KIND_USAGE_RELEASE]
                    ),
                )
                .order_by(LedgerEntry.id)
            ).scalars()
        )
    finally:
        session.close()


def _usage_remaining(db_session_factory: sessionmaker, user_id: int) -> int:
    session = db_session_factory()
    try:
        used, limit = session.execute(
            select(User.usage_used, User.usage_limit).where(User.id == user_id)
        ).one()
        return limit - used
    finally:
        session.close()


def _assert_usage_continuous(
    db_session_factory: sessionmaker, user: User
) -> None:
    """usage_after 连续性：剩余次数按账本行序 -1（预留）/+1（释放）严格衔接，
    且与 users 行的剩余次数一致——余额/次数任何时刻不为负的可审计形态。"""
    remaining = user.usage_limit
    for row in _usage_rows(db_session_factory, user.id):
        remaining += 1 if row.kind == billing.KIND_USAGE_RELEASE else -1
        assert row.usage_after == remaining, f"usage_after 断链于账本行 {row.id}"
        assert remaining >= 0
    assert remaining == _usage_remaining(db_session_factory, user.id)


# ---- API 全链：创建 → 预留 → Worker 推进 → 下载 ----


def test_full_chain_text_format_download(
    client: TestClient,
    usage_user: Callable[..., User],
    word_output_dir: Path,
    db_session_factory: sessionmaker,
) -> None:
    user = usage_user(usage_limit=3)
    headers = {"X-Card-Key": user.card_key}

    response = client.post(
        "/api/word-formatter/format/text",
        json={"text": "# 绪论\n\n第一段正文内容。\n\n第二段正文内容。\n"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    assert response.json()["status"] == "queued"

    # 创建即预留：账本出现带 task_id 的 usage_reserve 行，剩余次数 2
    rows = _usage_rows(db_session_factory, user.id)
    assert [(r.kind, r.task_id, r.usage_after) for r in rows] == [
        (billing.KIND_USAGE_RESERVE, job_id, 2)
    ]
    assert_ledger_reconciles(db_session_factory, user.id)
    _assert_usage_continuous(db_session_factory, user)

    # 未完成 409（先于下载验证，Worker 推进前）
    assert (
        client.get(f"/api/word-formatter/jobs/{job_id}/download", headers=headers).status_code
        == 409
    )

    # Worker 单步推进（真实渲染库编译 + 落盘）
    assert asyncio.run(WordJobWorker(db_session_factory).process_next()) is True
    job = _job_row(db_session_factory, job_id)
    assert job.status == "completed"
    assert job.output_path == f"{job_id}.docx"
    assert job.output_filename == f"formatted_{job_id}.docx"

    # 产物在数据目录、DB 只存文件名
    artifact = word_jobs.artifact_path(job.output_path)
    assert artifact.parent == word_output_dir
    assert artifact.is_file() and artifact.stat().st_size > 0

    # 下载：header 鉴权、docx 媒体类型、Content-Disposition 交付文件名
    response = client.get(f"/api/word-formatter/jobs/{job_id}/download", headers=headers)
    assert response.status_code == 200
    assert (
        response.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert f"formatted_{job_id}.docx" in response.headers["content-disposition"]
    doc = Document(io.BytesIO(response.content))
    assert any(p.text.strip() for p in doc.paragraphs)

    # 成功不释放：次数消耗为 1，账本无 release 行
    assert _usage_remaining(db_session_factory, user.id) == 2
    assert all(r.kind == billing.KIND_USAGE_RESERVE for r in _usage_rows(db_session_factory, user.id))
    assert_ledger_reconciles(db_session_factory, user.id)


def test_create_requires_card_key(client: TestClient, db_session_factory: sessionmaker) -> None:
    assert (
        client.post("/api/word-formatter/format/text", json={"text": "正文。"}).status_code == 401
    )
    session = db_session_factory()
    try:
        assert session.execute(select(func.count(FormatJob.id))).scalar_one() == 0
    finally:
        session.close()


def test_create_with_insufficient_usage_is_403_without_side_effects(
    client: TestClient, usage_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = usage_user(usage_limit=0)
    response = client.post(
        "/api/word-formatter/format/text",
        json={"text": "正文。"},
        headers={"X-Card-Key": user.card_key},
    )
    assert response.status_code == 403
    session = db_session_factory()
    try:
        assert session.execute(select(func.count(FormatJob.id))).scalar_one() == 0
        # 账本零变动：次数轨（与余额轨）都不因 403 出现任何行（仅存发卡的 issue 行）
        kinds = session.execute(select(LedgerEntry.kind)).scalars().all()
        assert kinds == [billing.KIND_ISSUE]
    finally:
        session.close()


def test_create_with_bad_custom_spec_is_422_before_reserve(
    client: TestClient, usage_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = usage_user()
    response = client.post(
        "/api/word-formatter/format/text",
        json={"text": "正文。", "custom_spec": {"margins_mm": "不是对象"}},
        headers={"X-Card-Key": user.card_key},
    )
    assert response.status_code == 422
    # 坏规格挡在预留之前：零任务、零账本变动
    assert _usage_rows(db_session_factory, user.id) == []
    assert _usage_remaining(db_session_factory, user.id) == user.usage_limit


def test_create_rejects_blank_and_oversized_text(
    client: TestClient,
    usage_user: Callable[..., User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空文本 422；超上限文本 413（M4-T7：与 Nginx 第二道防线同一状态码）。"""
    user = usage_user()
    headers = {"X-Card-Key": user.card_key}
    assert (
        client.post(
            "/api/word-formatter/format/text", json={"text": "   \n "}, headers=headers
        ).status_code
        == 422
    )
    monkeypatch.setattr(settings, "max_word_text_chars", 10)
    assert (
        client.post(
            "/api/word-formatter/format/text", json={"text": "这一段正文的长度超过了十个字的上限"}, headers=headers
        ).status_code
        == 413
    )


def test_file_upload_rejects_oversized_file(
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上传文件字节超上限 413，挡在解码/提文本之前，零副作用（M4-T7）。"""
    monkeypatch.setattr(settings, "max_word_upload_mb", 1)
    user = usage_user()
    response = client.post(
        "/api/word-formatter/format/file",
        files={"file": ("大文件.txt", b"x" * (1024 * 1024 + 1), "text/plain")},
        headers={"X-Card-Key": user.card_key},
    )
    assert response.status_code == 413
    session = db_session_factory()
    try:
        assert session.execute(select(func.count(FormatJob.id))).scalar_one() == 0
    finally:
        session.close()


# ---- 任务查询 / 列表 / 删除 ----


def test_job_list_and_detail_are_scoped_to_owner(
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    alice = usage_user(card_key="card-alice", usage_limit=5)
    bob = usage_user(card_key="card-bob", usage_limit=5)
    mine_1 = _create_job(db_session_factory, alice)
    mine_2 = _create_job(db_session_factory, alice)
    _create_job(db_session_factory, bob)

    listed = client.get(
        "/api/word-formatter/jobs", headers={"X-Card-Key": alice.card_key}
    ).json()
    assert [job["id"] for job in listed] == [mine_2, mine_1]  # 最新在前，且不含他人任务

    alice_headers = {"X-Card-Key": alice.card_key}
    assert client.get(f"/api/word-formatter/jobs/{mine_1}", headers=alice_headers).status_code == 200
    bob_headers = {"X-Card-Key": bob.card_key}
    assert client.get(f"/api/word-formatter/jobs/{mine_1}", headers=bob_headers).status_code == 404


def test_download_guards(
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    alice = usage_user(card_key="card-alice", usage_limit=5)
    bob = usage_user(card_key="card-bob", usage_limit=5)
    job_id = _create_job(db_session_factory, alice)

    assert (
        client.get(f"/api/word-formatter/jobs/{job_id}/download").status_code == 401
    )  # 卡密缺失
    assert (
        client.get(
            f"/api/word-formatter/jobs/{job_id}/download",
            headers={"X-Card-Key": bob.card_key},
        ).status_code
        == 404
    )  # 他人任务不泄露存在性
    assert (
        client.get(
            f"/api/word-formatter/jobs/{job_id + 999}/download",
            headers={"X-Card-Key": alice.card_key},
        ).status_code
        == 404
    )  # 不存在的任务


def test_delete_completed_job_removes_row_and_file(
    client: TestClient,
    usage_user: Callable[..., User],
    word_output_dir: Path,
    db_session_factory: sessionmaker,
) -> None:
    user = usage_user()
    headers = {"X-Card-Key": user.card_key}
    job_id = _create_job(db_session_factory, user)
    compiler = ScriptedCompiler(_ok_result())
    assert asyncio.run(WordJobWorker(db_session_factory, compiler=compiler).process_next()) is True
    artifact = word_jobs.artifact_path(f"{job_id}.docx")
    assert artifact.is_file()

    assert client.delete(f"/api/word-formatter/jobs/{job_id}", headers=headers).status_code == 204
    assert client.get(f"/api/word-formatter/jobs/{job_id}", headers=headers).status_code == 404
    assert not artifact.exists()  # 连落盘产物一并清理


def test_delete_preprocess_job_removes_result_json(
    client: TestClient,
    usage_user: Callable[..., User],
    word_output_dir: Path,
    db_session_factory: sessionmaker,
) -> None:
    """预处理任务的产物是结果 JSON（无 docx）：删除同样不留痕迹（M4-T7，
    清理共用 job_artifacts，不允许只删其一留下残留）。"""
    import app.word_preprocess as word_preprocess

    user = usage_user()
    headers = {"X-Card-Key": user.card_key}
    with short_tx(db_session_factory) as session:
        job = word_jobs.create_format_job(
            session, user, job_type=word_jobs.JOB_PREPROCESS, text="正文。", options=None
        )
        job_id = job.id
    _force_status(db_session_factory, job_id, word_jobs.JOB_COMPLETED)
    result = word_preprocess.result_path(job_id)
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text(json.dumps({"marked_text": "正文。"}), encoding="utf-8")

    assert client.delete(f"/api/word-formatter/jobs/{job_id}", headers=headers).status_code == 204
    assert _job_row(db_session_factory, job_id) is None
    assert not result.exists()


def test_delete_guards(
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    alice = usage_user(card_key="card-alice", usage_limit=5)
    bob = usage_user(card_key="card-bob", usage_limit=5)
    job_id = _create_job(db_session_factory, alice)

    # 未终态 409（删除会与 Worker/失败释放竞争，直接定义为非法）
    assert (
        client.delete(
            f"/api/word-formatter/jobs/{job_id}", headers={"X-Card-Key": alice.card_key}
        ).status_code
        == 409
    )
    # 他人任务 404，不泄露存在性
    assert (
        client.delete(
            f"/api/word-formatter/jobs/{job_id}", headers={"X-Card-Key": bob.card_key}
        ).status_code
        == 404
    )
    assert _job_row(db_session_factory, job_id) is not None


# ---- Worker 单步推进：认领竞争不双跑、失败路径响亮不吞 ----


def test_worker_claim_competition_has_single_owner(
    usage_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = usage_user()
    job_id = _create_job(db_session_factory, user)

    entered = threading.Event()
    release = threading.Event()

    def slow_compile(_text: str, _options: object) -> CompileResult:
        entered.set()
        release.wait(timeout=5)
        return _ok_result()

    slow_compiler = ScriptedCompiler(slow_compile)
    worker1 = WordJobWorker(db_session_factory, compiler=slow_compiler)
    never_called = ScriptedCompiler(_ok_result())
    worker2 = WordJobWorker(db_session_factory, compiler=never_called)

    async def scenario() -> None:
        task = asyncio.create_task(worker1.process_next())
        await asyncio.to_thread(entered.wait)
        # 任务已被 worker1 认领（queued → running）：worker2 拉不到 queued，
        # 编译器一次都不该被调——认领竞争恰好一方得手，不双跑
        assert await worker2.process_next() is False
        release.set()
        assert await task is True

    asyncio.run(scenario())

    assert len(slow_compiler.calls) == 1
    assert never_called.calls == []
    assert _job_row(db_session_factory, job_id).status == "completed"


def test_worker_content_failure_fails_job_and_releases_usage(
    usage_user: Callable[..., User], db_session_factory: sessionmaker, word_output_dir: Path
) -> None:
    user = usage_user(usage_limit=2)
    job_id = _create_job(db_session_factory, user)
    assert _usage_remaining(db_session_factory, user.id) == user.usage_limit - 1

    compiler = ScriptedCompiler(CompileResult(success=False, error="规格构建失败：缺页边距"))
    assert asyncio.run(WordJobWorker(db_session_factory, compiler=compiler).process_next()) is True

    job = _job_row(db_session_factory, job_id)
    assert job.status == "failed"
    assert "规格构建失败" in job.error
    assert not (word_output_dir / f"{job_id}.docx").exists()  # 失败无产物
    # 次数释放回满：usage_release 行带 task_id，任何时刻不为负
    rows = _usage_rows(db_session_factory, user.id)
    assert [(r.kind, r.task_id) for r in rows] == [
        (billing.KIND_USAGE_RESERVE, job_id),
        (billing.KIND_USAGE_RELEASE, job_id),
    ]
    assert _usage_remaining(db_session_factory, user.id) == user.usage_limit
    assert_ledger_reconciles(db_session_factory, user.id)
    _assert_usage_continuous(db_session_factory, user)


def test_worker_compile_exception_fails_job_releases_usage_and_logs_loud(
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
    word_output_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """编译线程抛出的编程错误不吞：ERROR 级带栈显性记录，任务 failed 且
    预留次数全额释放（与内容性失败同一收场，用户权益不受实现故障影响）。"""
    user = usage_user()
    job_id = _create_job(db_session_factory, user)
    compiler = ScriptedCompiler(RuntimeError("注入：编译器编程错误"))

    with caplog.at_level(logging.ERROR, logger="app.word_worker"):
        assert asyncio.run(WordJobWorker(db_session_factory, compiler=compiler).process_next()) is True

    assert "编译/落盘失败" in caplog.text  # 响亮不吞
    job = _job_row(db_session_factory, job_id)
    assert job.status == "failed" and "注入" in job.error
    assert _usage_remaining(db_session_factory, user.id) == user.usage_limit
    assert_ledger_reconciles(db_session_factory, user.id)
    _assert_usage_continuous(db_session_factory, user)


def test_worker_round_failure_is_loud_and_loop_survives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """数据库路径的编程故障（构造参数注入的坏会话工厂）不吞：单轮 ERROR
    级显性记录，常驻循环不死，退避后继续服务队列。"""
    def broken_factory() -> object:
        raise RuntimeError("注入：数据库故障")

    async def scenario() -> None:
        task = asyncio.create_task(WordJobWorker(broken_factory).run_forever())
        await asyncio.sleep(0.15)
        assert not task.done()  # 多轮异常后仍然存活
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.ERROR, logger="app.word_worker"):
        asyncio.run(scenario())

    assert "单轮处理失败" in caplog.text  # 响亮不吞


def _only_job_id(db_session_factory: sessionmaker) -> int:
    session = db_session_factory()
    try:
        return session.execute(select(FormatJob.id)).scalar_one()
    finally:
        session.close()


# ---- 启动恢复：遗留 running 复位续跑 / 超限失败释放 ----


def test_recovery_resumes_running_job_within_limit(
    usage_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = usage_user()
    job_id = _create_job(db_session_factory, user, status=word_jobs.JOB_RUNNING, attempt_count=0)

    assert word_jobs.recover_interrupted(db_session_factory) == RecoveryReport(resumed=1, abandoned=0)
    row = _job_row(db_session_factory, job_id)
    assert row.status == "queued" and row.attempt_count == 1  # 续跑前递增
    assert row.phase is None and row.progress is None  # 陈旧进度清空，重跑从头推导

    # 复位后新 Worker 接手：整体重跑至 completed，预留次数只消耗一次
    compiler = ScriptedCompiler(_ok_result())
    assert asyncio.run(WordJobWorker(db_session_factory, compiler=compiler).process_next()) is True
    assert _job_row(db_session_factory, job_id).status == "completed"
    kinds = [r.kind for r in _usage_rows(db_session_factory, user.id)]
    assert kinds == [billing.KIND_USAGE_RESERVE]
    assert_ledger_reconciles(db_session_factory, user.id)
    _assert_usage_continuous(db_session_factory, user)


def test_recovery_over_limit_fails_job_and_releases_usage(
    usage_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = usage_user()
    job_id = _create_job(db_session_factory, user, status=word_jobs.JOB_RUNNING, attempt_count=3)

    assert word_jobs.recover_interrupted(db_session_factory, max_attempts=3) == RecoveryReport(
        resumed=0, abandoned=1
    )
    row = _job_row(db_session_factory, job_id)
    assert row.status == "failed" and "超过上限" in row.error
    # 失败释放：usage_release 行带 task_id，剩余次数回到预留前
    rows = _usage_rows(db_session_factory, user.id)
    assert [(r.kind, r.task_id) for r in rows] == [
        (billing.KIND_USAGE_RESERVE, job_id),
        (billing.KIND_USAGE_RELEASE, job_id),
    ]
    assert _usage_remaining(db_session_factory, user.id) == user.usage_limit
    assert_ledger_reconciles(db_session_factory, user.id)
    _assert_usage_continuous(db_session_factory, user)


def test_recovery_ignores_queued_and_terminal_jobs(
    usage_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = usage_user()
    queued_id = _create_job(db_session_factory, user)

    assert word_jobs.recover_interrupted(db_session_factory) == RecoveryReport(resumed=0, abandoned=0)
    row = _job_row(db_session_factory, queued_id)
    assert row.status == "queued" and row.attempt_count == 0  # 不碰、不计数


# ---- 保留期清理：过期终态任务连库行与产物一并删除（M4-T7） ----


def test_purge_expired_jobs_removes_only_expired_terminal_jobs(
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
    word_output_dir: Path,
) -> None:
    user = usage_user()
    stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=25)

    completed_id = _create_job(db_session_factory, user)
    assert asyncio.run(WordJobWorker(db_session_factory, compiler=ScriptedCompiler(_ok_result())).process_next()) is True
    docx = word_jobs.artifact_path(f"{completed_id}.docx")
    assert docx.is_file()
    _force_updated_at(db_session_factory, completed_id, stale)

    # 过期 failed 预处理任务：产物是结果 JSON 而非 docx（清理不得漏掉这一种）
    with short_tx(db_session_factory) as session:
        job = word_jobs.create_format_job(
            session, user, job_type=word_jobs.JOB_PREPROCESS, text="正文。", options=None
        )
        failed_id = job.id
    _force_status(db_session_factory, failed_id, word_jobs.JOB_FAILED)
    result = word_preprocess.result_path(failed_id)
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text(json.dumps({"marked_text": ""}), encoding="utf-8")
    _force_updated_at(db_session_factory, failed_id, stale)

    fresh_id = _create_job(db_session_factory, user)
    assert asyncio.run(WordJobWorker(db_session_factory, compiler=ScriptedCompiler(_ok_result())).process_next()) is True
    fresh_docx = word_jobs.artifact_path(f"{fresh_id}.docx")
    assert fresh_docx.is_file()

    queued_id = _create_job(db_session_factory, user)
    _force_updated_at(db_session_factory, queued_id, stale)

    assert word_jobs.purge_expired_jobs(db_session_factory, retention_seconds=3600) == 2

    assert _job_row(db_session_factory, completed_id) is None
    assert _job_row(db_session_factory, failed_id) is None
    assert not docx.exists() and not result.exists()  # 库行与产物一并消失
    assert _job_row(db_session_factory, fresh_id).status == "completed"  # 未过期不碰
    assert fresh_docx.exists()
    assert _job_row(db_session_factory, queued_id).status == "queued"  # 排队中不碰


def test_purge_forever_reaps_expired_job_and_stays_cancellable(
    usage_user: Callable[..., User], db_session_factory: sessionmaker, word_output_dir: Path
) -> None:
    user = usage_user()
    job_id = _create_job(db_session_factory, user, status=word_jobs.JOB_FAILED)
    _force_updated_at(db_session_factory, job_id, datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=25))

    async def scenario() -> None:
        task = asyncio.create_task(
            word_jobs.purge_forever(db_session_factory, interval_seconds=0.01, retention_seconds=3600)
        )
        for _ in range(500):
            if _job_row(db_session_factory, job_id) is None:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert _job_row(db_session_factory, job_id) is None  # 巡检协程清掉了过期任务


def test_purge_forever_survives_round_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """清理协程不死：单轮异常记日志后下一轮继续（与润色巡检同一纪律）。"""

    def boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("注入：清理数据库故障")

    monkeypatch.setattr(word_jobs, "purge_expired_jobs", boom)

    async def scenario() -> None:
        task = asyncio.create_task(
            word_jobs.purge_forever(None, interval_seconds=0.01)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.1)
        assert not task.done()  # 多轮异常后仍然存活
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.ERROR, logger="app.word_jobs"):
        asyncio.run(scenario())

    assert "保留期清理轮次失败" in caplog.text  # 响亮不吞


# ---- lifespan 装配：启动恢复先于 Worker，Worker 与清理巡检随 lifespan 生死 ----


def test_lifespan_recovers_word_jobs_and_starts_worker(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    user = usage_user()
    job_id = _create_job(db_session_factory, user, status=word_jobs.JOB_RUNNING)

    constructed: list[object] = []

    class RecordingWorker:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            constructed.append(self)

        async def run_forever(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(app_main, "WordJobWorker", RecordingWorker)

    purge_mounted: list[bool] = []

    async def parked_purge(_factory: object, **_kwargs: object) -> None:
        purge_mounted.append(True)
        await asyncio.Event().wait()

    monkeypatch.setattr(word_jobs, "purge_forever", parked_purge)

    with client:
        pass

    row = _job_row(db_session_factory, job_id)
    assert row.status == "queued" and row.attempt_count == 1  # 启动恢复已执行
    assert len(constructed) == 1  # 任务 Worker 已挂载
    assert purge_mounted == [True]  # 保留期清理巡检已挂载


# ---- POST /format/file：上传解码（UTF-8/GBK 回落）与 docx 提文本（issue #34） ----


def test_file_upload_decodes_txt_and_gbk_fallback(
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    """txt 上传按 UTF-8 解码；GBK（Windows 记事本默认编码）回落解码不乱码；
    原文件名随任务保留，选项字段进入 options JSON。"""
    user = usage_user()
    headers = {"X-Card-Key": user.card_key}

    response = client.post(
        "/api/word-formatter/format/file",
        files={"file": ("论文.txt", "第一段正文。\n\n第二段正文。".encode("utf-8"), "text/plain")},
        data={"spec_name": "通用论文（无缩进）", "include_cover": "false"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["input_file_name"] == "论文.txt"
    job = _job_row(db_session_factory, body["id"])
    assert job.input_text == "第一段正文。\n\n第二段正文。"
    assert job.options["spec_name"] == "通用论文（无缩进）"
    assert job.options["include_cover"] is False
    assert job.options["input_format"] == "auto"

    response = client.post(
        "/api/word-formatter/format/file",
        files={"file": ("记事本.txt", "中文正文内容".encode("gbk"), "text/plain")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    job = _job_row(db_session_factory, response.json()["id"])
    assert job.input_text == "中文正文内容"


def test_file_upload_md_and_docx(
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    """.md 显式按 Markdown；.docx 走 M4-T1 提文本工具自动提取正文。"""
    user = usage_user()
    headers = {"X-Card-Key": user.card_key}

    response = client.post(
        "/api/word-formatter/format/file",
        files={"file": ("笔记.md", "# 标题\n\n正文段落。".encode("utf-8"), "text/markdown")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert _job_row(db_session_factory, response.json()["id"]).options["input_format"] == "markdown"

    buffer = io.BytesIO()
    doc = Document()
    doc.add_paragraph("docx 里的第一段")
    doc.add_paragraph("docx 里的第二段")
    doc.save(buffer)
    response = client.post(
        "/api/word-formatter/format/file",
        files={"file": ("毕业论文.docx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    job = _job_row(db_session_factory, response.json()["id"])
    assert job.input_text == "docx 里的第一段\ndocx 里的第二段"
    assert job.input_file_name == "毕业论文.docx"


def test_file_upload_guards(
    client: TestClient,
    usage_user: Callable[..., User],
) -> None:
    """不支持的扩展名、损坏的 docx、空文本一律 422（挡在预留之前，零副作用）。"""
    user = usage_user()
    headers = {"X-Card-Key": user.card_key}

    response = client.post(
        "/api/word-formatter/format/file",
        files={"file": ("论文.pdf", b"%PDF-1.4 fake", "application/pdf")},
        headers=headers,
    )
    assert response.status_code == 422

    response = client.post(
        "/api/word-formatter/format/file",
        files={"file": ("损坏.docx", b"not a zip", "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 422

    response = client.post(
        "/api/word-formatter/format/file",
        files={"file": ("空白.txt", b"   \n ", "text/plain")},
        headers=headers,
    )
    assert response.status_code == 422

    assert (
        client.post(
            "/api/word-formatter/format/file",
            files={"file": ("论文.txt", "正文".encode("utf-8"), "text/plain")},
        ).status_code
        == 401
    )


def test_file_upload_output_filename_keeps_original_name(
    client: TestClient,
    usage_user: Callable[..., User],
    word_output_dir: Path,
    db_session_factory: sessionmaker,
) -> None:
    """交付文件名保留上传原文件名加后缀（spec #31 用户故事 5）。"""
    user = usage_user()
    headers = {"X-Card-Key": user.card_key}
    response = client.post(
        "/api/word-formatter/format/file",
        files={"file": ("毕业论文.txt", "正文段落。".encode("utf-8"), "text/plain")},
        headers=headers,
    )
    job_id = response.json()["id"]

    assert asyncio.run(WordJobWorker(db_session_factory).process_next()) is True

    response = client.get(f"/api/word-formatter/jobs/{job_id}/download", headers=headers)
    assert response.status_code == 200
    # 非 ASCII 交付名走 RFC 5987 的 filename*=utf-8'' 形态
    assert "毕业论文_formatted.docx" in unquote(response.headers["content-disposition"])


# ---- GET /specs：内置规格列表（M4-T3 格式化页选择器数据源） ----


def test_specs_lists_builtin_specs(
    client: TestClient, make_user: Callable[..., User]
) -> None:
    user = make_user(card_key="card-specs")
    response = client.get("/api/word-formatter/specs", headers={"X-Card-Key": user.card_key})
    assert response.status_code == 200
    assert response.json()["specs"] == ["通用论文（首行缩进）", "通用论文（无缩进）"]
    assert client.get("/api/word-formatter/specs").status_code == 401
