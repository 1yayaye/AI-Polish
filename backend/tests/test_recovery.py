"""僵尸恢复协议（issue #20）：断点续跑、attempt 上限兜底、巡检兜底、
RefundConflict 不崩溃、重启期间新任务不丢。

kill -9 等价物（spec #15 测试决策）：worker 协程在 LLM 等待中被 cancel
（与进程被杀同样的效果——没有收尾代码执行，会话停在中途状态），随后
复用同一数据库文件走启动恢复 + 新 Worker 实例（重启 app 实例），全程
真实代码，不 mock 内部函数；唯一例外是 RefundConflict 测试在计费接缝
注入竞争异常（账本契约要求的调用方行为，真实并发下 SQLite 单写者不可
达，见 app.recovery._abandon docstring）。
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

import app.main as app_main
from app import billing, optimization, recovery as recovery_mod
from app.config import settings
from app.models import OptimizationSession, User
from app.recovery import RecoveryReport, recover_interrupted
from app.worker import PipelineWorker

from test_billing import assert_ledger_reconciles
from test_worker import (
    ScriptedLLM,
    _balance_fen,
    _change_logs,
    _create_queued,
    _refund_rows,
    _segments,
    _session_row,
    funded_user,
    make_worker,
)

INITIAL_BALANCE_FEN = 5000


def _crash_running(
    db_session_factory: sessionmaker, session_id: int, *, attempt_count: int = 0
) -> None:
    """把会话拨成"上一进程死亡时停在中途"的状态（running + attempt 计数）。"""
    session = db_session_factory()
    try:
        session.execute(
            update(OptimizationSession)
            .where(OptimizationSession.id == session_id)
            .values(
                status=optimization.SESSION_RUNNING,
                model_snapshot="scripted-model",
                attempt_count=attempt_count,
            )
        )
        session.commit()
    finally:
        session.close()


def _set_updated_at(db_session_factory: sessionmaker, session_id: int, at: datetime) -> None:
    session = db_session_factory()
    try:
        session.execute(
            update(OptimizationSession)
            .where(OptimizationSession.id == session_id)
            .values(updated_at=at)
        )
        session.commit()
    finally:
        session.close()


# ---- kill -9 → 同库重启 → 断点续跑 ----


def test_kill9_midway_resumes_from_first_incomplete_segment_without_recompute(
    funded_user: Callable[..., User],
    make_worker: Callable[..., PipelineWorker],
    db_session_factory: sessionmaker,
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "第一段原文。\n\n第二段原文。", "paper_polish")

    # 第一次运行：完成第 0 段后在第 1 段的 LLM 等待中被杀（kill -9 等价物）
    inside_call = asyncio.Event()

    def hang(_text: str) -> object:
        inside_call.set()
        return asyncio.sleep(3600)

    llm1 = ScriptedLLM({("polish", 0): "润色0", ("polish", 1): hang})
    worker1 = make_worker(llm1, poll_interval_seconds=0.01)

    async def crash() -> None:
        task = asyncio.create_task(worker1.run_forever())
        await inside_call.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(crash())

    row = _session_row(db_session_factory, session_id)
    assert row.status == "running" and row.attempt_count == 0
    assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN - 1  # 预扣在账

    # 同一数据库文件重启 app 实例：启动恢复复位续跑，新 Worker 从断点段落继续
    assert recover_interrupted(db_session_factory) == RecoveryReport(resumed=1, abandoned=0)
    row = _session_row(db_session_factory, session_id)
    assert row.status == "queued" and row.attempt_count == 1  # 续跑前递增

    llm2 = ScriptedLLM({("polish", 0): lambda text: f"新:{text}"})
    assert asyncio.run(make_worker(llm2).process_next()) is True

    row = _session_row(db_session_factory, session_id)
    assert row.status == "completed" and row.attempt_count == 1
    segments = _segments(db_session_factory, session_id)
    assert [(s.seq, s.status, s.result_text) for s in segments] == [
        (0, "completed", "润色0"),  # 已完成段落不重算
        (1, "completed", "新:第二段原文。"),  # 从首个未完成段落续跑
    ]
    # 新 Worker 只调用了第 1 段（输入是原文第 2 段），第 0 段零重算
    assert llm2.calls == [("polish", 0, "第二段原文。")]
    # 变更记录不重复：第 0 段仍只有崩溃前那一条
    logs = _change_logs(db_session_factory, session_id)
    assert [(l.stage, l.seq, l.before_text, l.after_text) for l in logs] == [
        ("polish", 0, "第一段原文。", "润色0"),
        ("polish", 1, "第二段原文。", "新:第二段原文。"),
    ]
    # 资金：只预扣一次、无任何退款，账本对账成立
    assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN - 1
    assert _refund_rows(db_session_factory, session_id) == []
    assert_ledger_reconciles(db_session_factory, user.id)


# ---- attempt 上限：超限兜底退款 ----


def test_attempt_within_limit_is_resumed_with_incremented_count(
    funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "第一段。", "paper_polish")
    _crash_running(db_session_factory, session_id, attempt_count=2)

    assert recover_interrupted(db_session_factory, max_attempts=3) == RecoveryReport(
        resumed=1, abandoned=0
    )
    row = _session_row(db_session_factory, session_id)
    assert row.status == "queued" and row.attempt_count == 3
    assert _refund_rows(db_session_factory, session_id) == []


def test_attempt_over_limit_abandons_with_full_refund(
    funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "第一段。\n\n第二段。", "paper_polish")
    charged = _session_row(db_session_factory, session_id).charged_fen
    _crash_running(db_session_factory, session_id, attempt_count=3)

    assert recover_interrupted(db_session_factory, max_attempts=3) == RecoveryReport(
        resumed=0, abandoned=1
    )
    row = _session_row(db_session_factory, session_id)
    assert row.status == "failed"
    # 全额退款到账，恰好一条等额 refund 行，账本对账成立
    assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN
    refunds = _refund_rows(db_session_factory, session_id)
    assert len(refunds) == 1 and refunds[0].amount_fen == charged
    assert_ledger_reconciles(db_session_factory, user.id)


# ---- 重启期间新提交的任务不丢 ----


def test_queued_sessions_are_untouched_by_recovery_and_picked_by_new_worker(
    funded_user: Callable[..., User],
    make_worker: Callable[..., PipelineWorker],
    db_session_factory: sessionmaker,
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "重启期间提交。", "paper_polish")

    assert recover_interrupted(db_session_factory) == RecoveryReport(resumed=0, abandoned=0)
    row = _session_row(db_session_factory, session_id)
    assert row.status == "queued" and row.attempt_count == 0  # 不碰、不计数

    llm = ScriptedLLM({("polish", 0): lambda text: f"过:{text}"})
    assert asyncio.run(make_worker(llm).process_next()) is True
    assert _session_row(db_session_factory, session_id).status == "completed"
    assert_ledger_reconciles(db_session_factory, user.id)


# ---- 巡检协程：滞留超阈值兜底退款 ----


def test_patrol_refunds_only_sessions_stuck_past_threshold(
    funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user()
    stuck_id = _create_queued(db_session_factory, user, "滞留会话。", "paper_polish")
    fresh_id = _create_queued(db_session_factory, user, "在跑会话。", "paper_polish")
    charged = _session_row(db_session_factory, stuck_id).charged_fen
    for session_id in (stuck_id, fresh_id):
        _crash_running(db_session_factory, session_id)
    _set_updated_at(
        db_session_factory, stuck_id, datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=3)
    )

    assert recovery_mod.patrol_once(db_session_factory, stuck_after_seconds=7200) == 1

    assert _session_row(db_session_factory, stuck_id).status == "failed"  # 滞留者兜底退款
    assert _session_row(db_session_factory, fresh_id).status == "running"  # 在跑者不动
    assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN - charged
    refunds = _refund_rows(db_session_factory, stuck_id)
    assert len(refunds) == 1 and refunds[0].amount_fen == charged
    assert_ledger_reconciles(db_session_factory, user.id)


def test_patrol_forever_reaps_stuck_session_and_stays_cancellable(
    funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "滞留会话。", "paper_polish")
    _crash_running(db_session_factory, session_id)

    async def scenario() -> None:
        task = asyncio.create_task(
            recovery_mod.patrol_forever(
                db_session_factory, stuck_after_seconds=0.05, interval_seconds=0.01
            )
        )
        for _ in range(500):
            if _session_row(db_session_factory, session_id).status == "failed":
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert _session_row(db_session_factory, session_id).status == "failed"
    assert len(_refund_rows(db_session_factory, session_id)) == 1
    assert_ledger_reconciles(db_session_factory, user.id)


def test_patrol_forever_survives_round_errors(
    db_session_factory: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """看门狗不死：单轮巡检异常记日志后下一轮继续，协程不退出。"""

    def boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("巡检数据库故障")

    monkeypatch.setattr(recovery_mod, "patrol_once", boom)

    async def scenario() -> None:
        task = asyncio.create_task(
            recovery_mod.patrol_forever(db_session_factory, interval_seconds=0.01)
        )
        await asyncio.sleep(0.1)
        assert not task.done()  # 多轮异常后仍然存活
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


# ---- 孤儿 queued 盲区：只告警不退款（方案 B） ----


def test_patrol_warns_on_stale_queued_without_touching_it(
    funded_user: Callable[..., User], db_session_factory: sessionmaker, caplog: pytest.LogCaptureFixture
) -> None:
    """滞留 queued 是合法等待态：巡检只告警，不退款、不改状态、不计数。"""
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "滞留排队。", "paper_polish")
    _set_updated_at(
        db_session_factory, session_id, datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=3)
    )

    with caplog.at_level(logging.WARNING, logger="app.recovery"):
        assert recovery_mod.patrol_once(db_session_factory, stuck_after_seconds=7200) == 0

    assert "queued 会话滞留" in caplog.text  # 告警可见
    row = _session_row(db_session_factory, session_id)
    assert row.status == "queued" and row.attempt_count == 0  # 会话原样
    assert _refund_rows(db_session_factory, session_id) == []
    assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN - row.charged_fen
    assert_ledger_reconciles(db_session_factory, user.id)


def test_patrol_does_not_warn_on_fresh_queued(
    funded_user: Callable[..., User], db_session_factory: sessionmaker, caplog: pytest.LogCaptureFixture
) -> None:
    user = funded_user()
    _create_queued(db_session_factory, user, "正常排队。", "paper_polish")

    with caplog.at_level(logging.WARNING, logger="app.recovery"):
        assert recovery_mod.patrol_once(db_session_factory, stuck_after_seconds=7200) == 0

    assert "queued" not in caplog.text  # 新鲜 queued 不告警


def test_report_orphan_queued_counts_and_warns(
    funded_user: Callable[..., User], db_session_factory: sessionmaker, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.recovery"):
        assert recovery_mod.report_orphan_queued(db_session_factory) == 0
    assert "queued" not in caplog.text  # 空库不告警

    caplog.clear()
    user = funded_user()
    _create_queued(db_session_factory, user, "排队一。", "paper_polish")
    _create_queued(db_session_factory, user, "排队二。", "paper_enhance")

    with caplog.at_level(logging.WARNING, logger="app.recovery"):
        assert recovery_mod.report_orphan_queued(db_session_factory) == 2

    assert "queued 会话无人认领" in caplog.text
    assert_ledger_reconciles(db_session_factory, user.id)


def test_lifespan_warns_on_orphan_queued_when_llm_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    client,
    funded_user: Callable[..., User],
    db_session_factory: sessionmaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """流水线未配置启动：库内 queued 会话经启动清点告警，会话不被改动。"""
    for attr in ("llm_base_url", "llm_api_key", "llm_model"):
        monkeypatch.setattr(settings, attr, "")
    constructed: list[object] = []
    monkeypatch.setattr(
        app_main,
        "PipelineWorker",
        lambda *_args: constructed.append(_args),  # type: ignore[arg-type,return-value]
    )
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "孤儿排队。", "paper_polish")

    with caplog.at_level(logging.WARNING, logger="app.recovery"):
        with client:
            pass

    assert "queued 会话无人认领" in caplog.text  # 盲区显性化
    assert constructed == []  # Worker 依旧不启动
    row = _session_row(db_session_factory, session_id)
    assert row.status == "queued" and row.attempt_count == 0  # 只告警，不碰会话


# ---- RefundConflict：兜底路径不崩溃，显性记录后留待重试 ----


def test_refund_conflict_in_fallback_path_is_alarmed_not_crashed(
    funded_user: Callable[..., User], db_session_factory: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "第一段。", "paper_polish")
    charged = _session_row(db_session_factory, session_id).charged_fen
    _crash_running(db_session_factory, session_id)
    _set_updated_at(
        db_session_factory, session_id, datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=3)
    )

    def conflict(*_args: object, **_kwargs: object) -> bool:
        raise billing.RefundConflict("注入：并发退款竞争输掉")

    monkeypatch.setattr(billing, "refund", conflict)
    try:
        assert recovery_mod.patrol_once(db_session_factory) == 0  # 不崩溃，本轮放弃
        row = _session_row(db_session_factory, session_id)
        assert row.status == "running"  # 事务整体回滚，留给下一轮重试
        assert _refund_rows(db_session_factory, session_id) == []
        assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN - charged
    finally:
        monkeypatch.undo()

    # 重试落定：refund 幂等守卫下正常退款，failed 收场
    assert recovery_mod.patrol_once(db_session_factory) == 1
    assert _session_row(db_session_factory, session_id).status == "failed"
    assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN
    assert_ledger_reconciles(db_session_factory, user.id)


# ---- lifespan 装配：先恢复后启动，Worker 与巡检同生共死 ----


class _StubRecovery:
    """recovery 接缝桩：记录调用序，patrol_forever 挂起直到被取消。"""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def recover_interrupted(self, _factory: object) -> RecoveryReport:
        self._events.append("recovered")
        return RecoveryReport(resumed=0, abandoned=0)

    def report_orphan_queued(self, _factory: object) -> int:
        # 未配置分支的启动清点：桩只承接调用，不入 events（该测试断言
        # "无流水线即无恢复动作"，queued 清点是只读告警不算动作）
        return 0

    async def patrol_forever(self, _factory: object, **_kwargs: object) -> None:
        self._events.append("patrol-started")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self._events.append("patrol-cancelled")
            raise


class _StubWorker:
    events: list[str] = []

    def __init__(self, _factory: object, _llm: object) -> None:
        self._events = _StubWorker.events

    async def run_forever(self) -> None:
        self._events.append("worker-running")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self._events.append("worker-cancelled")
            raise


def test_lifespan_recovers_then_starts_worker_and_patrol(
    monkeypatch: pytest.MonkeyPatch, client
) -> None:
    events: list[str] = []
    stub = _StubRecovery(events)
    _StubWorker.events = events
    monkeypatch.setattr(app_main, "app_recovery", stub)
    monkeypatch.setattr(app_main, "PipelineWorker", _StubWorker)
    for attr, value in (
        ("llm_base_url", "https://llm.example/v1"),
        ("llm_api_key", "key"),
        ("llm_model", "m"),
    ):
        monkeypatch.setattr(settings, attr, value)

    with client:
        time.sleep(0.05)  # 让后台任务在独立事件循环线程里起跑

    # 启动恢复先于 Worker；关停时 Worker 与巡检一起被取消
    assert events == [
        "recovered",
        "worker-running",
        "patrol-started",
        "worker-cancelled",
        "patrol-cancelled",
    ]


def test_lifespan_skips_recovery_when_llm_unconfigured(
    monkeypatch: pytest.MonkeyPatch, client
) -> None:
    for attr in ("llm_base_url", "llm_api_key", "llm_model"):
        monkeypatch.setattr(settings, attr, "")
    events: list[str] = []
    monkeypatch.setattr(app_main, "app_recovery", _StubRecovery(events))
    monkeypatch.setattr(
        app_main,
        "PipelineWorker",
        lambda *_args: events.append("constructed"),  # type: ignore[arg-type,return-value]
    )

    with client:
        pass

    assert events == []  # 无流水线即无恢复协议
