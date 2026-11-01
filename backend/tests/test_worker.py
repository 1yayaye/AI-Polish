"""Worker 流水线行为（issue #17）：全链路、失败退款、短事务纪律、崩溃语义。

全部经脚本化假 LLM 接缝（spec #15 测试决策"接缝二"），不 mock 内部函数；
假 LLM 钩子内断言"LLM 等待期间零数据库连接检出"（NullPool 下检出数即
打开的会话数，>0 即 LLM 等待期间有事务未关）验证短事务纪律。
"""

import asyncio
import inspect
from collections.abc import Callable
from datetime import datetime

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import sessionmaker

from app import billing, optimization, worker as worker_mod
from app.config import settings
from app.llm import LLMContentError, LLMTransientError
import app.main as app_main
from app.models import (
    LedgerEntry,
    OptimizationSession,
    Segment,
    SegmentChangeLog,
    User,
)

from test_billing import assert_ledger_reconciles

INITIAL_BALANCE_FEN = 5000


class ScriptedLLM:
    """脚本化假 LLM 接缝：按 (stage, seq) 返回预设输出。

    动作可为 str（输出）、Exception 实例（抛出）或可调用（输入 → 输出，
    可返回可等待对象）。stage 由测试注入的恒等 prompt_for 透传得到，seq
    按同阶段调用次数推得——与 Worker 的处理序（阶段序 × 段序）一致。
    每次调用的 (stage, seq, 输入) 记入 calls 供断言。
    """

    model_name = "scripted-model"

    def __init__(
        self,
        script: dict[tuple[str, int], object],
        on_call: Callable[[str, int, str], None] | None = None,
    ) -> None:
        self.script = script
        self.on_call = on_call
        self.calls: list[tuple[str, int, str]] = []
        self._stage_calls: dict[str, int] = {}

    async def polish(self, text: str, *, system_prompt: str) -> str:
        stage = system_prompt  # 恒等 prompt_for：system_prompt 即阶段名
        seq = self._stage_calls.get(stage, 0)
        self._stage_calls[stage] = seq + 1
        self.calls.append((stage, seq, text))
        if self.on_call is not None:
            self.on_call(stage, seq, text)
        action = self.script[(stage, seq)]
        if isinstance(action, Exception):
            raise action
        if callable(action):
            outcome = action(text)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            return str(outcome)
        return str(action)  # type: ignore[arg-type]


@pytest.fixture()
def funded_user(
    make_user: Callable[..., User], db_session_factory: sessionmaker
) -> Callable[..., User]:
    def _make(card_key: str = "card-worker", balance_fen: int = INITIAL_BALANCE_FEN) -> User:
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


@pytest.fixture()
def make_worker(db_session_factory: sessionmaker) -> Callable[..., worker_mod.PipelineWorker]:
    """prompt_for 恒等映射（阶段名即提示词），ScriptedLLM 据此分辨阶段。"""

    def _make(
        llm: ScriptedLLM, *, poll_interval_seconds: float = 0.0
    ) -> worker_mod.PipelineWorker:
        return worker_mod.PipelineWorker(
            db_session_factory,
            llm,
            poll_interval_seconds=poll_interval_seconds,
            prompt_for=lambda stage: stage,
        )

    return _make


@pytest.fixture()
def open_connections(db_engine) -> dict[str, int]:
    """当前检出的连接数（NullPool：>0 即有会话/事务未关）。"""
    counter = {"n": 0}

    def on_checkout(*_args: object) -> None:
        counter["n"] += 1

    def on_checkin(*_args: object) -> None:
        counter["n"] -= 1

    event.listen(db_engine, "checkout", on_checkout)
    event.listen(db_engine, "checkin", on_checkin)
    yield counter
    event.remove(db_engine, "checkout", on_checkout)
    event.remove(db_engine, "checkin", on_checkin)


# ---- 查询助手：每个助手一个独立短连接，不与 Worker 共享会话 ----


def _create_queued(
    db_session_factory: sessionmaker, user: User, text: str, mode: str
) -> int:
    session = db_session_factory()
    try:
        row = optimization.create_session(session, user, text=text, mode=mode)
        session.commit()
        return row.id
    finally:
        session.close()


def _session_row(db_session_factory: sessionmaker, session_id: int) -> OptimizationSession:
    session = db_session_factory()
    try:
        return session.get(OptimizationSession, session_id)
    finally:
        session.close()


def _segments(db_session_factory: sessionmaker, session_id: int) -> list[Segment]:
    session = db_session_factory()
    try:
        return list(
            session.execute(
                select(Segment)
                .where(Segment.session_id == session_id)
                .order_by(Segment.stage.desc(), Segment.seq)
            ).scalars()
        )
    finally:
        session.close()


def _change_logs(db_session_factory: sessionmaker, session_id: int) -> list[SegmentChangeLog]:
    session = db_session_factory()
    try:
        return list(
            session.execute(
                select(SegmentChangeLog)
                .where(SegmentChangeLog.session_id == session_id)
                .order_by(SegmentChangeLog.stage.desc(), SegmentChangeLog.seq)
            ).scalars()
        )
    finally:
        session.close()


def _balance_fen(db_session_factory: sessionmaker, user_id: int) -> int:
    session = db_session_factory()
    try:
        return session.execute(
            select(User.balance_fen).where(User.id == user_id)
        ).scalar_one()
    finally:
        session.close()


def _refund_rows(db_session_factory: sessionmaker, task_id: int) -> list[LedgerEntry]:
    session = db_session_factory()
    try:
        return list(
            session.execute(
                select(LedgerEntry).where(
                    LedgerEntry.task_id == task_id, LedgerEntry.kind == billing.KIND_REFUND
                )
            ).scalars()
        )
    finally:
        session.close()


# ---- 全链路 ----


def test_full_pipeline_completes_two_stage_session_with_results_and_change_logs(
    funded_user: Callable[..., User],
    make_worker: Callable[..., worker_mod.PipelineWorker],
    db_session_factory: sessionmaker,
) -> None:
    user = funded_user()
    session_id = _create_queued(
        db_session_factory, user, "第一段原文。\n\n第二段原文。", "paper_polish_enhance"
    )
    llm = ScriptedLLM(
        {
            ("polish", 0): "润色0",
            ("polish", 1): "润色1",
            ("enhance", 0): "增强0",
            ("enhance", 1): "增强1",
        }
    )
    worker = make_worker(llm)

    assert asyncio.run(worker.process_next()) is True

    row = _session_row(db_session_factory, session_id)
    assert row.status == "completed"
    assert row.model_snapshot == "scripted-model"  # 只读快照，无 key/base_url

    # 段落结果：润色段在前；增强段输入被润色产物覆盖（增强在润色产物上进行）
    all_segments = _segments(db_session_factory, session_id)
    polish = [s for s in all_segments if s.stage == "polish"]
    enhance = [s for s in all_segments if s.stage == "enhance"]
    assert [(s.stage, s.seq, s.status) for s in polish] == [
        ("polish", 0, "completed"),
        ("polish", 1, "completed"),
    ]
    assert [s.result_text for s in polish] == ["润色0", "润色1"]
    assert [(s.stage, s.seq, s.status) for s in enhance] == [
        ("enhance", 0, "completed"),
        ("enhance", 1, "completed"),
    ]
    assert [s.source_text for s in enhance] == ["润色0", "润色1"]
    assert [s.result_text for s in enhance] == ["增强0", "增强1"]

    # 增强段收到的输入 = 润色产物
    assert llm.calls[2][0] == "enhance" and llm.calls[2][2] == "润色0"

    # 变更记录：每段每阶段一行 before/after，before 是该次调用的实际输入
    logs = _change_logs(db_session_factory, session_id)
    assert [(l.stage, l.seq, l.before_text, l.after_text) for l in logs] == [
        ("polish", 0, "第一段原文。", "润色0"),
        ("polish", 1, "第二段原文。", "润色1"),
        ("enhance", 0, "润色0", "增强0"),
        ("enhance", 1, "润色1", "增强1"),
    ]

    # 资金：预扣费保持在账（成功不退款），账本对账成立
    assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN - 1
    assert _refund_rows(db_session_factory, session_id) == []
    assert_ledger_reconciles(db_session_factory, user.id)


def test_enhance_only_mode_processes_original_text(
    funded_user: Callable[..., User],
    make_worker: Callable[..., worker_mod.PipelineWorker],
    db_session_factory: sessionmaker,
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "第一段。\n\n第二段。", "paper_enhance")
    llm = ScriptedLLM({("enhance", 0): "增强0", ("enhance", 1): "增强1"})
    worker = make_worker(llm)

    assert asyncio.run(worker.process_next()) is True

    row = _session_row(db_session_factory, session_id)
    assert row.status == "completed"
    segments = _segments(db_session_factory, session_id)
    assert [(s.stage, s.seq, s.status) for s in segments] == [
        ("enhance", 0, "completed"),
        ("enhance", 1, "completed"),
    ]
    assert [s.source_text for s in segments] == ["第一段。", "第二段。"]  # 无润色覆盖
    assert_ledger_reconciles(db_session_factory, user.id)


def test_no_queued_session_is_reported_idle(
    make_worker: Callable[..., worker_mod.PipelineWorker],
) -> None:
    worker = make_worker(ScriptedLLM({}))
    assert asyncio.run(worker.process_next()) is False


def test_claims_oldest_session_first_by_created_at(
    funded_user: Callable[..., User],
    make_worker: Callable[..., worker_mod.PipelineWorker],
    db_session_factory: sessionmaker,
) -> None:
    user = funded_user()
    newer = _create_queued(db_session_factory, user, "新会话。", "paper_polish")
    older = _create_queued(db_session_factory, user, "旧会话。", "paper_polish")
    # created_at 是服务端时间戳，测试显式拨钟：older 造得晚但时间更早
    session = db_session_factory()
    try:
        session.execute(
            update(OptimizationSession)
            .where(OptimizationSession.id == older)
            .values(created_at=datetime(2026, 1, 1))
        )
        session.commit()
    finally:
        session.close()
    # 两个会话各一段：假 LLM 的阶段内计数跨会话累计，第二个会话命中键 1
    llm = ScriptedLLM({("polish", 0): lambda text: f"过：{text}", ("polish", 1): lambda text: f"过：{text}"})
    worker = make_worker(llm)

    assert asyncio.run(worker.process_next()) is True
    assert _session_row(db_session_factory, older).status == "completed"
    assert _session_row(db_session_factory, newer).status == "queued"

    assert asyncio.run(worker.process_next()) is True
    assert _session_row(db_session_factory, newer).status == "completed"


# ---- 失败语义：failed + 自动退款 ----


@pytest.mark.parametrize(
    "failure",
    [
        LLMContentError("LLM 请求被拒绝（HTTP 401）：bad key"),
        LLMTransientError("LLM 服务瞬时错误（HTTP 503）：unavailable"),  # 重试归客户端，Worker 只认结果
    ],
)
def test_llm_failure_fails_session_and_refunds_full_charge(
    failure: Exception,
    funded_user: Callable[..., User],
    make_worker: Callable[..., worker_mod.PipelineWorker],
    db_session_factory: sessionmaker,
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "第一段。\n\n第二段。", "paper_polish")
    llm = ScriptedLLM({("polish", 0): failure})
    worker = make_worker(llm)

    assert asyncio.run(worker.process_next()) is True

    row = _session_row(db_session_factory, session_id)
    assert row.status == "failed"
    segments = _segments(db_session_factory, session_id)
    assert segments[0].status == "failed"  # 失败段落可见
    assert segments[1].status == "pending"  # 未处理段落保持 pending
    # 退款到账：余额回到预扣前，恰好一条等额 refund 行
    assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN
    refunds = _refund_rows(db_session_factory, session_id)
    assert len(refunds) == 1 and refunds[0].amount_fen == row.charged_fen
    assert_ledger_reconciles(db_session_factory, user.id)


def test_failure_stops_processing_remaining_segments(
    funded_user: Callable[..., User],
    make_worker: Callable[..., worker_mod.PipelineWorker],
    db_session_factory: sessionmaker,
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "第一段。\n\n第二段。", "paper_polish")
    llm = ScriptedLLM({("polish", 0): "润色0", ("polish", 1): LLMContentError("炸")})
    worker = make_worker(llm)

    assert asyncio.run(worker.process_next()) is True

    row = _session_row(db_session_factory, session_id)
    assert row.status == "failed"
    polish = _segments(db_session_factory, session_id)
    by_seq = {s.seq: s for s in polish}
    assert by_seq[0].status == "completed" and by_seq[0].result_text == "润色0"
    assert by_seq[1].status == "failed"
    assert len(llm.calls) == 2  # 失败后不再继续后续段落
    assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN
    assert_ledger_reconciles(db_session_factory, user.id)


# ---- 短事务纪律：LLM 等待期间零写事务（假 LLM 钩子内断言） ----


def test_no_db_transaction_is_held_while_awaiting_llm(
    funded_user: Callable[..., User],
    make_worker: Callable[..., worker_mod.PipelineWorker],
    db_session_factory: sessionmaker,
    open_connections: dict[str, int],
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "第一段。\n\n第二段。", "paper_polish_enhance")
    seen: dict[tuple[str, int], int] = {}

    def on_call(stage: str, seq: int, _text: str) -> None:
        # 纪律断言一：LLM 等待期间连接零检出（零会话即零写事务）
        assert open_connections["n"] == 0, (
            f"LLM 等待期间持有 {open_connections['n']} 个数据库连接（stage={stage} seq={seq}）"
        )
        seen[(stage, seq)] = sum(
            1 for s in _segments(db_session_factory, session_id) if s.status == "completed"
        )

    llm = ScriptedLLM(
        {
            ("polish", 0): "润色0",
            ("polish", 1): "润色1",
            ("enhance", 0): "增强0",
            ("enhance", 1): "增强1",
        },
        on_call=on_call,
    )
    worker = make_worker(llm)

    assert asyncio.run(worker.process_next()) is True

    # 纪律断言二：结果写在 await 之后——第一次调用时零段落完成，
    # 增强阶段开跑时两个润色段均已完成（读-提交-等待-写的阶段序推进）
    assert seen[("polish", 0)] == 0
    assert seen[("enhance", 0)] == 2


# ---- 崩溃语义：进程内中断停在中途，资金不丢（T5 恢复供料） ----


def test_worker_crash_midway_keeps_running_state_and_holds_funds(
    funded_user: Callable[..., User],
    make_worker: Callable[..., worker_mod.PipelineWorker],
    db_session_factory: sessionmaker,
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "第一段。\n\n第二段。", "paper_polish")
    inside_call = asyncio.Event()

    def hang(_text: str) -> object:
        inside_call.set()
        return asyncio.sleep(3600)

    llm = ScriptedLLM({("polish", 0): "润色0", ("polish", 1): hang})
    worker = make_worker(llm, poll_interval_seconds=0.01)

    async def scenario() -> None:
        task = asyncio.create_task(worker.run_forever())
        await inside_call.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    # 停在中途：会话 running（不落失败/完成等脏终态），已完成段落保留
    row = _session_row(db_session_factory, session_id)
    assert row.status == "running"
    by_seq = {s.seq: s for s in _segments(db_session_factory, session_id)}
    assert by_seq[0].status == "completed" and by_seq[0].result_text == "润色0"
    assert by_seq[1].status == "pending" and by_seq[1].result_text is None
    assert len(_change_logs(db_session_factory, session_id)) == 1
    # 资金不丢：预扣费原样在账，无退款（退款归 T5 兜底协议）
    assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN - 1
    assert _refund_rows(db_session_factory, session_id) == []
    assert_ledger_reconciles(db_session_factory, user.id)


# ---- 并发取消：Worker 让位，不重复退款 ----


def test_cancel_during_running_stops_worker_without_double_refund(
    funded_user: Callable[..., User],
    make_worker: Callable[..., worker_mod.PipelineWorker],
    db_session_factory: sessionmaker,
) -> None:
    user = funded_user()
    session_id = _create_queued(db_session_factory, user, "第一段。\n\n第二段。", "paper_polish_enhance")

    def cancel_on_first_call(stage: str, seq: int, _text: str) -> None:
        if (stage, seq) == ("polish", 0):
            session = db_session_factory()
            try:
                optimization.cancel_session(session, user, session_id)
                session.commit()
            finally:
                session.close()

    llm = ScriptedLLM(
        {("polish", 0): "润色0", ("polish", 1): "润色1", ("enhance", 0): "增强0", ("enhance", 1): "增强1"},
        on_call=cancel_on_first_call,
    )
    worker = make_worker(llm)

    assert asyncio.run(worker.process_next()) is True

    row = _session_row(db_session_factory, session_id)
    assert row.status == "cancelled"  # 取消方赢，Worker 不覆盖终态
    # Worker 在下一段前让位：只处理了被取消前那一段
    assert [(s.seq, s.status) for s in _segments(db_session_factory, session_id) if s.stage == "polish"] == [
        (0, "completed"),
        (1, "pending"),
    ]
    assert _balance_fen(db_session_factory, user.id) == INITIAL_BALANCE_FEN  # 取消退款到账
    assert len(_refund_rows(db_session_factory, session_id)) == 1  # 不双倍退款
    assert_ledger_reconciles(db_session_factory, user.id)


# ---- lifespan 装配：模型接入齐备才启动 Worker，关停即取消 ----


def test_lifespan_starts_worker_only_when_llm_configured(
    monkeypatch: pytest.MonkeyPatch,
    client,
) -> None:
    lifecycle: list[str] = []

    class StubWorker:
        def __init__(self, _factory: object, _llm: object) -> None:
            lifecycle.append("constructed")

        async def run_forever(self) -> None:
            lifecycle.append("running")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                lifecycle.append("cancelled")
                raise

    monkeypatch.setattr(app_main, "PipelineWorker", StubWorker)
    for attr, value in (
        ("llm_base_url", "https://llm.example/v1"),
        ("llm_api_key", "key"),
        ("llm_model", "m"),
    ):
        monkeypatch.setattr(settings, attr, value)

    with client:
        assert lifecycle == ["constructed", "running"]
    assert lifecycle == ["constructed", "running", "cancelled"]  # 关停即取消


def test_lifespan_skips_worker_when_llm_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    client,
) -> None:
    for attr in ("llm_base_url", "llm_api_key", "llm_model"):
        monkeypatch.setattr(settings, attr, "")
    constructed: list[object] = []
    monkeypatch.setattr(
        app_main,
        "PipelineWorker",
        lambda *_args: constructed.append(_args),  # type: ignore[arg-type,return-value]
    )

    with client:
        pass

    assert constructed == []
