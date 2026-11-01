"""Word Formatter 任务进度流（issue #34）：认证与归属、snapshot → progress 增量 →
终态事件序、Last-Event-ID 续传不丢不重、失败原因与次数释放在流上可见。

为什么起真 uvicorn：与 test_stream 同一理由——SSE 的验收必须走真实 HTTP 流。
夹具与 SSE 解析助手照 test_worker.ScriptedLLM 先例跨测试模块复用 test_stream
的同构设施；任务 Worker 由脚本化假编译器驱动（test_word_jobs 的接缝），Worker
内的通知接线（app.word_stream.notify）是真实代码，通知总线与流生成器天然
同循环。

事件确定性的边界：认领（0.0）与进度写（0.1）之间无等待点，单循环内两次
通知必然合并为一次唤醒——progress 事件只推最新状态（id=千分进度+1），中间
展示态合并是协议的固有语义（进度只是展示态，快照与终态兜底全量），测试按
此断言。
"""

import asyncio
import threading
import time
from collections.abc import Callable

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import sessionmaker

from app.models import FormatJob, User
from app.word_formatter import CompileResult
from app.word_worker import WordJobWorker
from test_stream import (  # noqa: F401 - server 夹具与 SSE 解析助手跨测试模块复用
    _base_url,
    _collect,
    _line_reader,
    _run_on_server,
    server,
)
from test_word_jobs import (  # noqa: F401 - 用户与编译接缝夹具跨测试模块复用
    ScriptedCompiler,
    _usage_remaining,
    usage_user,
    word_output_dir,
)

_TERMINAL_EVENTS = ("completed", "failed")


def _stream_url(job_id: int) -> str:
    return f"/api/word-formatter/jobs/{job_id}/stream"


async def _create_job(
    client: AsyncClient, headers: dict[str, str], db_session_factory: sessionmaker
) -> int:
    """经 HTTP API 创建一个 queued 任务，并等它对独立连接可见——201 返回与
    get_session 提交收尾间存在微秒级竞态（test_stream 同款注释），流连接与
    Worker 认领都紧随其后，必须先消除时序抖动。"""
    resp = await client.post(
        "/api/word-formatter/format/text",
        json={"text": "# 绪论\n\n第一段正文内容。\n\n第二段正文内容。"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["id"]
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        session = db_session_factory()
        try:
            if session.get(FormatJob, job_id) is not None:
                return job_id
        finally:
            session.close()
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务 {job_id} 创建后 2s 内对独立连接不可见")


def test_stream_rejects_missing_wrong_or_url_passed_card_key(
    server: object,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    """无卡密/错卡密/卡密进 URL 一律 401：流与下载同走 X-Card-Key header。"""
    user = usage_user(usage_limit=2)
    headers = {"X-Card-Key": user.card_key}

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            job_id = await _create_job(client, headers, db_session_factory)

            for bad_headers in (
                {},  # 缺失
                {"X-Card-Key": "not-a-valid-key"},  # 错误
            ):
                resp = await client.get(_stream_url(job_id), headers=bad_headers)
                assert resp.status_code == 401, bad_headers

            # 卡密只认 header：放进 URL（query 参数）不认证——query 会落 Nginx 日志
            resp = await client.get(f"{_stream_url(job_id)}?card_key={user.card_key}")
            assert resp.status_code == 401

    _run_on_server(server, scenario())


def test_stream_rejects_other_users_job_with_404(
    server: object,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    user = usage_user(usage_limit=2)
    other = usage_user(card_key="card-word-other", usage_limit=2)
    headers = {"X-Card-Key": user.card_key}

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            job_id = await _create_job(client, headers, db_session_factory)
            resp = await client.get(
                _stream_url(job_id), headers={"X-Card-Key": other.card_key}
            )
            assert resp.status_code == 404  # 他人任务不泄露存在性

    _run_on_server(server, scenario())


def test_stream_pushes_snapshot_then_progress_then_completed(
    server: object,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
    word_output_dir: object,
) -> None:
    """事件序：snapshot（queued 全量）→ progress 增量（id=千分进度+1）→
    completed 终态（全量载荷带交付文件名，不带 id）。"""
    user = usage_user(usage_limit=2)
    headers = {"X-Card-Key": user.card_key}

    release = threading.Event()

    def gated_compile(_text: str, _options: object) -> CompileResult:
        release.wait(timeout=5)
        return CompileResult(success=True, docx_bytes=b"fake-docx")

    worker = WordJobWorker(db_session_factory, compiler=gated_compile)

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            job_id = await _create_job(client, headers, db_session_factory)

            async with client.stream("GET", _stream_url(job_id), headers=headers) as resp:
                lines = _line_reader(resp)
                head = await _collect(lines, stop=lambda e: e.event == "snapshot")
                worker_task = asyncio.create_task(worker.process_next())
                # 编译被门控挂起期间：认领/进度写已提交，progress 事件到达
                tail = await _collect(lines, stop=lambda e: e.id == 101)
                release.set()
                end = await _collect(lines, stop=lambda e: e.event in _TERMINAL_EVENTS)
            assert await worker_task is True

        assert head[-1].event == "snapshot"
        snapshot = head[-1].data
        assert snapshot["status"] == "queued"
        assert snapshot["progress"] is None

        progress = [e for e in tail if e.event == "progress"]
        assert [e.id for e in progress] == [101]
        assert progress[0].data == {
            "status": "running",
            "phase": "compile",
            "progress": 0.1,
            "message": "正在编译文档",
            "error": None,
            "output_filename": None,
        }

        assert [e.event for e in end] == ["completed"]
        assert end[-1].id is None  # 终态不带 id：重连随 snapshot 重发不算重复
        assert end[-1].data["status"] == "completed"
        assert end[-1].data["output_filename"] == f"formatted_{job_id}.docx"
        assert end[-1].data["progress"] == 1.0

    _run_on_server(server, scenario())


def test_stream_failed_job_reports_reason_and_released_usage(
    server: object,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
    word_output_dir: object,
) -> None:
    """失败终态：failed 事件全量载荷带失败原因；预留次数已释放（页面展示的
    原因与账本一致——次数回到预留前）。"""
    user = usage_user(usage_limit=2)
    headers = {"X-Card-Key": user.card_key}
    worker = WordJobWorker(
        db_session_factory,
        compiler=ScriptedCompiler(CompileResult(success=False, error="规格构建失败：缺页边距")),
    )

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            job_id = await _create_job(client, headers, db_session_factory)

            async with client.stream("GET", _stream_url(job_id), headers=headers) as resp:
                lines = _line_reader(resp)
                await _collect(lines, stop=lambda e: e.event == "snapshot")
                worker_task = asyncio.create_task(worker.process_next())
                end = await _collect(lines, stop=lambda e: e.event in _TERMINAL_EVENTS)
            assert await worker_task is True

        assert [e.event for e in end] == ["failed"]
        assert end[-1].data["status"] == "failed"
        assert "规格构建失败" in end[-1].data["error"]

    _run_on_server(server, scenario())

    # 次数已退：与流上任务失败同账本事实
    assert _usage_remaining(db_session_factory, user.id) == user.usage_limit


def test_resume_with_last_event_id_neither_loses_nor_duplicates(
    server: object,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
    word_output_dir: object,
) -> None:
    """连接 A 收到 progress id=101 后断线；编译落定后重连（Last-Event-ID: 101）：
    缺口终态由 snapshot 补齐（不丢），断线前收过的事件不重发（不重）。"""
    user = usage_user(usage_limit=2)
    headers = {"X-Card-Key": user.card_key}

    release = threading.Event()

    def gated_compile(_text: str, _options: object) -> CompileResult:
        release.wait(timeout=5)
        return CompileResult(success=True, docx_bytes=b"fake-docx")

    worker = WordJobWorker(db_session_factory, compiler=gated_compile)

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            job_id = await _create_job(client, headers, db_session_factory)

            # 连接 A：snapshot + progress id=101（编译挂起中）后主动断开
            async with client.stream("GET", _stream_url(job_id), headers=headers) as resp_a:
                lines_a = _line_reader(resp_a)
                await _collect(lines_a, stop=lambda e: e.event == "snapshot")
                worker_task = asyncio.create_task(worker.process_next())
                events_a = await _collect(lines_a, stop=lambda e: e.id == 101)
            assert [e.event for e in events_a] == ["progress"]

            # 放行编译，等任务在库里落定终态
            release.set()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                session = db_session_factory()
                try:
                    row = session.get(FormatJob, job_id)
                    if row is not None and row.status == "completed":
                        break
                finally:
                    session.close()
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("任务 5s 内未落定终态")
            await worker_task

            # 连接 B：带 Last-Event-ID: 101 续传
            async with client.stream(
                "GET", _stream_url(job_id), headers={**headers, "Last-Event-ID": "101"}
            ) as resp_b:
                events_b = await _collect(
                    _line_reader(resp_b), stop=lambda e: e.event in _TERMINAL_EVENTS
                )

        # 不丢：断线期间落定的终态由必发 snapshot 补齐；不重：A 收过的
        # progress（id=101）不重发，B 只剩 snapshot + 终态
        assert [e.event for e in events_b] == ["snapshot", "completed"]
        assert events_b[0].data["status"] == "completed"
        assert events_b[-1].data["status"] == "completed"

    _run_on_server(server, scenario())


def test_completed_job_stream_is_snapshot_then_terminal_without_replay(
    server: object,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
    word_output_dir: object,
) -> None:
    """已终态任务：连接只收 snapshot + 终态，不重放任何 progress 事件（不重）。"""
    user = usage_user(usage_limit=2)
    headers = {"X-Card-Key": user.card_key}
    worker = WordJobWorker(db_session_factory, compiler=ScriptedCompiler(CompileResult(success=True, docx_bytes=b"fake-docx")))

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            job_id = await _create_job(client, headers, db_session_factory)
            assert await worker.process_next() is True

            async with client.stream("GET", _stream_url(job_id), headers=headers) as resp:
                events = await _collect(
                    _line_reader(resp), stop=lambda e: e.event in _TERMINAL_EVENTS
                )
        assert [e.event for e in events] == ["snapshot", "completed"]
        assert events[0].data["status"] == "completed"

    _run_on_server(server, scenario())
