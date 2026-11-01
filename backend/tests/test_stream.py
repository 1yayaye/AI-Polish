"""SSE 进度流（issue #19）：认证、快照→增量顺序、Last-Event-ID 续传不丢不重、
LLM 慢调用期间不阻塞（心跳 + 并发请求）、变更记录端点。

为什么起真 uvicorn：httpx 的 ASGITransport 会把响应体整体缓冲到应用结束，
无法测无限流；SSE 的验收（先快照后增量、断线续传、心跳并发）必须走真实
HTTP 流。夹具在后台线程起 uvicorn（127.0.0.1 随机端口），再把测试场景
（AsyncClient + Worker）调度到服务器的同一个事件循环上——与生产同构的单
进程 asyncio，通知总线与流生成器天然同循环。

Worker 仍由脚本化假 LLM 驱动（复用 test_worker 的接缝），假 LLM 之上全是
真实代码；假 LLM 的门控（gate）事件用于编排"慢调用"与断线时序。
"""

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import Future
from typing import NamedTuple

import pytest
import uvicorn
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import app.db as app_db
import app.main as app_main
from app import billing, optimization, stream as stream_mod
from app.models import OptimizationSession, Segment, User
from app.worker import PipelineWorker
from test_worker import ScriptedLLM

CARD_KEY = "card-stream"
OTHER_CARD_KEY = "card-other"
TEXT_TWO_PARAGRAPHS = "第一段原文。\n\n第二段原文。"
MODE_TWO_STAGE = "paper_polish_enhance"

HEADERS = {"X-Card-Key": CARD_KEY}


def _stream_url(session_id: int) -> str:
    return f"/api/optimization/sessions/{session_id}/stream"


class _Server(uvicorn.Server):
    """记录事件循环：测试场景要与 app + Worker 同循环（生产同构）。"""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)  # type: ignore[arg-type]
        self.test_loop: asyncio.AbstractEventLoop | None = None

    async def serve(self, sockets=None) -> None:  # type: ignore[no-untyped-def]
        self.test_loop = asyncio.get_running_loop()
        await super().serve(sockets)


@pytest.fixture()
def server(
    db_session_factory: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Server]:
    monkeypatch.setattr(app_db, "SessionLocal", db_session_factory)
    srv = _Server(
        uvicorn.Config(
            app_main.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
        )
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise AssertionError("uvicorn 测试服务未能在 10s 内启动")
        time.sleep(0.01)
    yield srv
    srv.should_exit = True
    thread.join(timeout=5)


def _base_url(server: _Server) -> str:
    _, port = server.servers[0].sockets[0].getsockname()[:2]
    return f"http://127.0.0.1:{port}"


def _run_on_server(server: _Server, coro: object) -> object:
    """把测试场景调度到服务器事件循环并等待结果（断言异常原样上抛）。"""
    assert server.test_loop is not None
    future: Future = asyncio.run_coroutine_threadsafe(coro, server.test_loop)  # type: ignore[arg-type]
    return future.result(timeout=60)


# ---- SSE 解析与读取 ----


class Event(NamedTuple):
    id: int | None
    event: str
    data: dict | None  # 注释行（心跳）data 为 None


def _flush(raw: dict[str, str]) -> Event:
    return Event(
        id=int(raw["id"]) if "id" in raw else None,
        event=raw.get("event", "message"),
        data=json.loads(raw["data"]) if "data" in raw else None,
    )


def _line_reader(resp) -> AsyncIterator[str]:
    """SSE 行迭代器：httpx 流式迭代器是一次性的，同一响应的多次 _collect
    必须共享同一个迭代器（先读快照、再继续读到 done）。"""
    return resp.aiter_lines().__aiter__()


async def _collect(
    lines: AsyncIterator[str], stop: Callable[[Event], bool], timeout: float = 5.0
) -> list[Event]:
    """从行迭代器逐事件读取 SSE 流直到 stop 命中（停止点必然事件对齐）。"""
    events: list[Event] = []
    raw: dict[str, str] = {}
    while True:
        try:
            line = await asyncio.wait_for(lines.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return events
        except asyncio.TimeoutError as exc:  # pragma: no cover - 防挂死护栏
            raise AssertionError(f"读 SSE 事件超时（已收 {len(events)} 个）") from exc
        if line == "":
            if raw:
                event = _flush(raw)
                events.append(event)
                raw = {}
                if stop(event):
                    return events
            continue
        if line.startswith(":"):
            event = Event(id=None, event="comment", data=None)
            events.append(event)
            if stop(event):
                return events
            continue
        field, _, value = line.partition(":")
        raw[field.strip()] = value.strip()


async def _create_session(
    client: AsyncClient,
    db_session_factory: sessionmaker,
    text: str = TEXT_TWO_PARAGRAPHS,
    mode: str = MODE_TWO_STAGE,
) -> int:
    resp = await client.post(
        "/api/optimization/sessions", json={"text": text, "mode": mode}, headers=HEADERS
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    # FastAPI 在响应发出后才跑 get_session 的提交收尾：201 返回与提交落库间
    # 存在微秒级竞态（生产 Worker 按秒轮询无感）。测试紧随其后就要读库/认领，
    # 在此等会话行对独立连接可见，消除时序抖动。
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        session = db_session_factory()
        try:
            if session.get(OptimizationSession, session_id) is not None:
                return session_id
        finally:
            session.close()
        await asyncio.sleep(0.01)
    raise AssertionError(f"会话 {session_id} 创建后 2s 内对独立连接不可见")


def _segment_done(
    db_session_factory: sessionmaker, session_id: int, stage: str, seq: int
) -> bool:
    session = db_session_factory()
    try:
        status = session.execute(
            select(Segment.status).where(
                Segment.session_id == session_id, Segment.stage == stage, Segment.seq == seq
            )
        ).scalar_one()
        return status == "completed"
    finally:
        session.close()


@pytest.fixture()
def funded_user(
    make_user: Callable[..., User], db_session_factory: sessionmaker
) -> Callable[..., User]:
    def _make(card_key: str = CARD_KEY, balance_fen: int = 5000) -> User:
        user = make_user(card_key=card_key)
        session = db_session_factory()
        try:
            billing.grant_initial_quota(
                session, user.id, balance_fen=balance_fen, usage_count=0, reason="发卡初始额度"
            )
            session.commit()
            return user
        finally:
            session.close()

    return _make


def _make_worker(db_session_factory: sessionmaker, llm: ScriptedLLM) -> PipelineWorker:
    return PipelineWorker(
        db_session_factory, llm, poll_interval_seconds=0.0, prompt_for=lambda stage: stage
    )


# ---- 认证：无卡密/错卡密 401；卡密不出现在 URL ----


def test_stream_rejects_missing_wrong_or_url_passed_card_key(
    server: _Server, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    funded_user()

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            session_id = await _create_session(client, db_session_factory)

            for headers in (
                {},  # 缺失
                {"X-Card-Key": "not-a-valid-key"},  # 错误
                {"X-Card-Key": ""},  # 空值
            ):
                resp = await client.get(_stream_url(session_id), headers=headers)
                assert resp.status_code == 401, headers

            # 卡密只认 header：放进 URL（query 参数）不认证——"卡密不进 URL"
            # 不是约定而是实现事实（query 会落 Nginx 日志，rewrite-plan §5）
            resp = await client.get(f"{_stream_url(session_id)}?card_key={CARD_KEY}")
            assert resp.status_code == 401

            async with client.stream(
                "GET", _stream_url(session_id), headers=HEADERS
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                events = await _collect(
                    _line_reader(resp), stop=lambda e: e.event == "snapshot"
                )
            assert events[0].event == "snapshot"

    _run_on_server(server, scenario())


def test_stream_rejects_other_users_session_with_404(
    server: _Server,
    funded_user: Callable[..., User],
    make_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    funded_user()
    make_user(card_key=OTHER_CARD_KEY)

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            session_id = await _create_session(client, db_session_factory)
            resp = await client.get(
                _stream_url(session_id), headers={"X-Card-Key": OTHER_CARD_KEY}
            )
            assert resp.status_code == 404

    _run_on_server(server, scenario())


# ---- 快照 → 增量：顺序与 id ----


def test_stream_pushes_snapshot_then_incremental_segments_in_processing_order(
    server: _Server, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    funded_user()
    llm = ScriptedLLM(
        {
            ("polish", 0): "润色0",
            ("polish", 1): "润色1",
            ("enhance", 0): "增强0",
            ("enhance", 1): "增强1",
        }
    )
    worker = _make_worker(db_session_factory, llm)

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            session_id = await _create_session(client, db_session_factory)  # 两段 × 两阶段 = 4 个段落事件

            async with client.stream(
                "GET", _stream_url(session_id), headers=HEADERS
            ) as resp:
                lines = _line_reader(resp)
                head = await _collect(lines, stop=lambda e: e.event == "snapshot")
                worker_task = asyncio.create_task(worker.process_next())
                tail = await _collect(lines, stop=lambda e: e.event == "done")
                assert await worker_task is True

        events = head + tail
        assert events[0].event == "snapshot"
        snapshot = events[0].data
        assert snapshot["status"] == "queued"
        assert [(s["stage"], s["seq"], s["status"]) for s in snapshot["segments"]] == [
            ("polish", 0, "pending"),
            ("polish", 1, "pending"),
            ("enhance", 0, "pending"),
            ("enhance", 1, "pending"),
        ]

        segments = [e for e in events if e.event == "segment"]
        assert [e.id for e in segments] == [1, 2, 3, 4]  # 处理序（阶段序 × 段序）单调
        assert [(e.data["stage"], e.data["seq"]) for e in segments] == [
            ("polish", 0),
            ("polish", 1),
            ("enhance", 0),
            ("enhance", 1),
        ]

        assert events[-1].event == "done"
        assert events[-1].data["status"] == "completed"
        assert all(s["status"] == "completed" for s in events[-1].data["segments"])

    _run_on_server(server, scenario())


def test_completed_session_stream_is_snapshot_then_done_without_replay(
    server: _Server, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    """已终态会话：连接只收 snapshot + done，不重放任何 segment 事件（不重）。"""
    funded_user()
    llm = ScriptedLLM(
        {
            ("polish", 0): "润色0",
            ("polish", 1): "润色1",
            ("enhance", 0): "增强0",
            ("enhance", 1): "增强1",
        }
    )
    worker = _make_worker(db_session_factory, llm)

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            session_id = await _create_session(client, db_session_factory)
            assert await worker.process_next() is True

            async with client.stream(
                "GET", _stream_url(session_id), headers=HEADERS
            ) as resp:
                events = await _collect(
                    _line_reader(resp), stop=lambda e: e.event == "done"
                )
        assert [e.event for e in events] == ["snapshot", "done"]
        assert events[0].data["status"] == "completed"
        assert all(s["status"] == "completed" for s in events[0].data["segments"])

    _run_on_server(server, scenario())


# ---- Last-Event-ID 续传：不丢不重 ----


def test_resume_with_last_event_id_neither_loses_nor_duplicates(
    server: _Server, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    """连接 A 收到 id=1 后断线；重连（Last-Event-ID: 1）后：缺口状态由 snapshot
    补齐（不丢），后续增量只推 id > 1（不重），done 收尾。"""
    funded_user()

    polish_gate = asyncio.Event()
    enhance_gate = asyncio.Event()

    async def _wait_then(_text: str, gate: asyncio.Event, output: str) -> str:
        await gate.wait()
        return output

    llm = ScriptedLLM(
        {
            ("polish", 0): "润色0",
            # 段 1（polish1）与段 3（enhance1）用门控制造两次"慢调用"
            ("polish", 1): lambda _t: _wait_then(_t, polish_gate, "润色1"),
            ("enhance", 0): "增强0",
            ("enhance", 1): lambda _t: _wait_then(_t, enhance_gate, "增强1"),
        }
    )
    worker = _make_worker(db_session_factory, llm)

    async def scenario() -> None:
        session_id: int
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            session_id = await _create_session(client, db_session_factory)

            # 连接 A：snapshot + segment id=1 后主动断开
            async with client.stream(
                "GET", _stream_url(session_id), headers=HEADERS
            ) as resp_a:
                lines_a = _line_reader(resp_a)
                await _collect(lines_a, stop=lambda e: e.event == "snapshot")
                worker_task = asyncio.create_task(worker.process_next())
                events_a = await _collect(lines_a, stop=lambda e: e.id == 1)

            assert [e.event for e in events_a] == ["segment"]  # snapshot 已被上一次 _collect 消费
            assert events_a[-1].id == 1
            assert (events_a[-1].data["stage"], events_a[-1].data["seq"]) == ("polish", 0)

            # 放行段 1：Worker 连续完成 polish1、enhance0 后卡在 enhance1 的门控上
            polish_gate.set()
            while not _segment_done(db_session_factory, session_id, "enhance", 0):
                await asyncio.sleep(0.01)

            # 连接 B：带 Last-Event-ID: 1 续传
            async with client.stream(
                "GET",
                _stream_url(session_id),
                headers={**HEADERS, "Last-Event-ID": "1"},
            ) as resp_b:
                lines_b = _line_reader(resp_b)
                head_b = await _collect(lines_b, stop=lambda e: e.event == "snapshot")
                enhance_gate.set()
                tail_b = await _collect(lines_b, stop=lambda e: e.event == "done")
                await worker_task

        snapshot_b = head_b[-1]
        assert snapshot_b.data["status"] == "running"
        assert [(s["stage"], s["seq"], s["status"]) for s in snapshot_b.data["segments"]] == [
            ("polish", 0, "completed"),
            ("polish", 1, "completed"),
            ("enhance", 0, "completed"),
            ("enhance", 1, "pending"),
        ]

        events_b = head_b + tail_b
        segments_b = [e for e in events_b if e.event == "segment"]
        ids_a = [e.id for e in events_a if e.event == "segment"]
        # 不重：A 收过的事件（id=1）绝不重发——B 里没有 id 1 正是 Last-Event-ID
        # 跳过逻辑的证明；不丢：断线期间完成的事件（2、3）与之后的事件（4）全部补发
        assert [e.id for e in segments_b] == [2, 3, 4]
        assert sorted(ids_a + [e.id for e in segments_b]) == [1, 2, 3, 4]

        done_b = tail_b[-1]
        assert done_b.data["status"] == "completed"
        merged = {
            (s["stage"], s["seq"]): s["status"] for s in done_b.data["segments"]
        }
        assert set(merged.values()) == {"completed"}

    _run_on_server(server, scenario())


# ---- LLM 慢调用期间不阻塞：心跳 + 并发请求 ----


def test_stream_heartbeats_and_other_requests_unblocked_during_slow_llm(
    server: _Server,
    funded_user: Callable[..., User],
    db_session_factory: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stream_mod, "_HEARTBEAT_SECONDS", 0.05)
    funded_user()

    hold = asyncio.Event()  # 第一段 LLM 调用挂起不放行（在服务器循环上等待）

    async def _hold(_text: str) -> str:
        await hold.wait()
        return "润色0"

    llm = ScriptedLLM({("polish", 0): _hold})
    worker = _make_worker(db_session_factory, llm)

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            session_id = await _create_session(client, db_session_factory)

            async with client.stream(
                "GET", _stream_url(session_id), headers=HEADERS
            ) as resp:
                lines = _line_reader(resp)
                await _collect(lines, stop=lambda e: e.event == "snapshot")
                worker_task = asyncio.create_task(worker.process_next())
                # LLM 挂起期间：心跳注释行持续到达（流未被慢调用冻住）
                events = await _collect(
                    lines, stop=lambda e: e.event == "comment", timeout=2.0
                )
                assert events[-1].event == "comment"

                # 同一事件循环内并发发起其他请求：立即得到响应（SSE 不占线程、
                # 不持事务、不阻塞循环）
                started = time.monotonic()
                quota = await client.get("/api/me/quota", headers=HEADERS)
                detail = await client.get(
                    f"/api/optimization/sessions/{session_id}", headers=HEADERS
                )
                elapsed = time.monotonic() - started

            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

        expected_cost = optimization.estimate_cost_fen(
            MODE_TWO_STAGE, optimization.count_billable_chars(TEXT_TWO_PARAGRAPHS)
        )
        assert quota.status_code == 200
        assert quota.json()["balance_fen"] == 5000 - expected_cost
        assert detail.status_code == 200 and detail.json()["status"] == "running"
        assert elapsed < 2.0, f"并发请求耗时 {elapsed:.2f}s，事件循环被阻塞"
        hold.set()  # 语义收尾：假 LLM 不再被等待（worker 已取消）

    _run_on_server(server, scenario())


# ---- 变更记录端点（工作台"每段 before/after 可查"） ----


def test_changes_endpoint_returns_before_after_per_segment(
    server: _Server,
    funded_user: Callable[..., User],
    make_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    funded_user()
    make_user(card_key=OTHER_CARD_KEY)
    llm = ScriptedLLM(
        {
            ("polish", 0): "润色0",
            ("polish", 1): "润色1",
            ("enhance", 0): "增强0",
            ("enhance", 1): "增强1",
        }
    )
    worker = _make_worker(db_session_factory, llm)

    async def scenario() -> None:
        async with AsyncClient(base_url=_base_url(server), trust_env=False) as client:
            session_id = await _create_session(client, db_session_factory)
            assert await worker.process_next() is True

            resp = await client.get(
                f"/api/optimization/sessions/{session_id}/changes", headers=HEADERS
            )
            assert resp.status_code == 200
            assert [
                (c["stage"], c["seq"], c["before_text"], c["after_text"])
                for c in resp.json()
            ] == [
                ("polish", 0, "第一段原文。", "润色0"),
                ("polish", 1, "第二段原文。", "润色1"),
                ("enhance", 0, "润色0", "增强0"),
                ("enhance", 1, "润色1", "增强1"),
            ]

            resp = await client.get(
                f"/api/optimization/sessions/{session_id}/changes",
                headers={"X-Card-Key": OTHER_CARD_KEY},
            )
            assert resp.status_code == 404

    _run_on_server(server, scenario())
