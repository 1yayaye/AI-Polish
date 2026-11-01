"""优化会话 API 行为（HTTP 接缝）：任务创建与预扣费闭环（issue #16）。

覆盖验收的 API 侧：提交即预扣费并切分 queued、余额不足 402 零副作用、
本人查询会话与段落状态、取消未完成会话自动退款。每个资金场景以
SUM(账本)==余额 收尾断言。只测外部行为，不测内部调用结构。
"""

from collections.abc import Callable
from pathlib import Path

import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import LedgerEntry, OptimizationSession, Segment, User

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import billing, optimization  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import short_tx  # noqa: E402
from test_billing import assert_ledger_reconciles  # noqa: E402

# 10000 个非空白字符：恰好一个"万字"，polish 定价 300 分（见 _PRICE_FEN_PER_10K）
FULL_TEN_THOUSAND = "字" * 10_000
POLISH_TEN_K_FEN = 300

# 默认文本上限（settings.max_session_text_chars）；发卡额度须覆盖满额预扣
DEFAULT_TEXT_CAP = 50_000


@pytest.fixture()
def funded_user(
    make_user: Callable[..., User], db_session_factory: sessionmaker
) -> Callable[..., User]:
    """经计费服务入账的发卡用户工厂：账本含 issue 行，SUM(账本)==余额 成立。"""

    def _make(card_key: str = "card-opt", balance_fen: int = 5000) -> User:
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


def _session_rows(db_session_factory: sessionmaker) -> list[OptimizationSession]:
    session = db_session_factory()
    try:
        return list(session.execute(select(OptimizationSession)).scalars())
    finally:
        session.close()


def _segments_of(db_session_factory: sessionmaker, session_id: int) -> list[Segment]:
    session = db_session_factory()
    try:
        return list(
            session.execute(
                select(Segment).where(Segment.session_id == session_id).order_by(Segment.id)
            ).scalars()
        )
    finally:
        session.close()


def _balance_fen(db_session_factory: sessionmaker, user_id: int) -> int:
    session = db_session_factory()
    try:
        return session.execute(select(User.balance_fen).where(User.id == user_id)).scalar_one()
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


def test_create_session_precharges_splits_and_queues(
    client: TestClient, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user(card_key="card-create", balance_fen=5000)

    response = client.post(
        "/api/optimization/sessions",
        json={"text": FULL_TEN_THOUSAND, "mode": "paper_polish"},
        headers={"X-Card-Key": user.card_key},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["mode"] == "paper_polish"
    assert body["word_count"] == 10_000
    assert body["charged_fen"] == POLISH_TEN_K_FEN

    # 段落已切分：无句读超长段硬切成 7 段（1500×6 + 1000），全部 pending
    segments = _segments_of(db_session_factory, body["id"])
    assert [s.seq for s in segments] == list(range(7))
    assert all(len(s.source_text) <= optimization.MAX_SEGMENT_CHARS for s in segments)
    assert "".join(s.source_text for s in segments) == FULL_TEN_THOUSAND
    assert all(s.status == "pending" and s.stage == "polish" for s in segments)

    # 资金：余额减少、账本出现 charge 行且追溯到会话，SUM(账本)==余额
    assert _balance_fen(db_session_factory, user.id) == 5000 - POLISH_TEN_K_FEN
    session = db_session_factory()
    try:
        charge_rows = (
            session.execute(
                select(LedgerEntry).where(
                    LedgerEntry.user_id == user.id, LedgerEntry.kind == "charge"
                )
            )
            .scalars()
            .all()
        )
        assert len(charge_rows) == 1
        assert charge_rows[0].amount_fen == -POLISH_TEN_K_FEN
        assert charge_rows[0].task_id == body["id"]
    finally:
        session.close()
    assert_ledger_reconciles(db_session_factory, user.id)


@pytest.mark.parametrize(
    ("mode", "stages"),
    [
        ("paper_polish", ["polish"]),
        ("paper_enhance", ["enhance"]),
        ("paper_polish_enhance", ["polish", "enhance"]),
    ],
)
def test_create_session_stage_layout_follows_mode(
    client: TestClient,
    funded_user: Callable[..., User],
    db_session_factory: sessionmaker,
    mode: str,
    stages: list[str],
) -> None:
    user = funded_user(card_key="card-mode", balance_fen=5000)
    response = client.post(
        "/api/optimization/sessions",
        json={"text": "第一段。\n\n第二段。", "mode": mode},
        headers={"X-Card-Key": user.card_key},
    )
    assert response.status_code == 201, response.text
    segments = _segments_of(db_session_factory, response.json()["id"])
    assert [(s.stage, s.seq) for s in segments] == [
        (stage, seq) for stage in stages for seq in range(2)
    ]
    assert_ledger_reconciles(db_session_factory, user.id)


def test_create_session_insufficient_balance_402_with_zero_side_effects(
    client: TestClient, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user(card_key="card-poor", balance_fen=100)

    response = client.post(
        "/api/optimization/sessions",
        json={"text": FULL_TEN_THOUSAND, "mode": "paper_polish"},
        headers={"X-Card-Key": user.card_key},
    )

    assert response.status_code == 402
    # 零副作用：无会话、无段落、账本不变、余额不变，SUM(账本)==余额 恒成立
    assert _session_rows(db_session_factory) == []
    assert _balance_fen(db_session_factory, user.id) == 100
    session = db_session_factory()
    try:
        kinds = [
            k
            for k in session.execute(
                select(LedgerEntry.kind).where(LedgerEntry.user_id == user.id)
            ).scalars()
        ]
        ledger_sum = session.execute(
            select(func.sum(LedgerEntry.amount_fen)).where(LedgerEntry.user_id == user.id)
        ).scalar_one()
    finally:
        session.close()
    assert kinds == ["issue"]
    assert ledger_sum == 100
    assert_ledger_reconciles(db_session_factory, user.id)


def test_create_session_rejects_blank_text_and_unknown_modes(
    client: TestClient, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user(card_key="card-blank", balance_fen=5000)
    headers = {"X-Card-Key": user.card_key}

    blank = client.post(
        "/api/optimization/sessions", json={"text": " \n\t ", "mode": "paper_polish"}, headers=headers
    )
    emotion = client.post(
        "/api/optimization/sessions", json={"text": "第一段。", "mode": "emotion_polish"}, headers=headers
    )
    unknown = client.post(
        "/api/optimization/sessions", json={"text": "第一段。", "mode": "no_such_mode"}, headers=headers
    )

    assert blank.status_code == 422
    assert emotion.status_code == 422  # emotion_polish 明确拒绝（计划 §8）
    assert unknown.status_code == 422
    assert _session_rows(db_session_factory) == []
    assert_ledger_reconciles(db_session_factory, user.id)


def test_owner_can_list_and_view_own_sessions_with_segment_status(
    client: TestClient, funded_user: Callable[..., User]
) -> None:
    user = funded_user(card_key="card-view", balance_fen=5000)
    headers = {"X-Card-Key": user.card_key}
    created = client.post(
        "/api/optimization/sessions",
        json={"text": "第一段。\n\n第二段。", "mode": "paper_polish"},
        headers=headers,
    ).json()

    listing = client.get("/api/optimization/sessions", headers=headers)
    detail = client.get(f"/api/optimization/sessions/{created['id']}", headers=headers)

    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()] == [created["id"]]
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "queued"
    assert [(s["stage"], s["seq"], s["status"]) for s in body["segments"]] == [
        ("polish", 0, "pending"),
        ("polish", 1, "pending"),
    ]


def test_other_users_session_is_invisible(
    client: TestClient, funded_user: Callable[..., User]
) -> None:
    owner = funded_user(card_key="card-owner", balance_fen=5000)
    stranger = funded_user(card_key="card-stranger", balance_fen=5000)
    created = client.post(
        "/api/optimization/sessions",
        json={"text": "第一段。", "mode": "paper_polish"},
        headers={"X-Card-Key": owner.card_key},
    ).json()

    response = client.get(
        f"/api/optimization/sessions/{created['id']}",
        headers={"X-Card-Key": stranger.card_key},
    )

    assert response.status_code == 404


def test_cancel_refunds_full_charge_and_writes_refund_row(
    client: TestClient, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user(card_key="card-cancel", balance_fen=5000)
    headers = {"X-Card-Key": user.card_key}
    created = client.post(
        "/api/optimization/sessions",
        json={"text": FULL_TEN_THOUSAND, "mode": "paper_polish"},
        headers=headers,
    ).json()

    response = client.post(f"/api/optimization/sessions/{created['id']}/cancel", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    # 退款到账：余额回到预扣前，账本出现等额 refund 行
    assert _balance_fen(db_session_factory, user.id) == 5000
    session = db_session_factory()
    try:
        refund_rows = (
            session.execute(
                select(LedgerEntry).where(
                    LedgerEntry.user_id == user.id, LedgerEntry.kind == "refund"
                )
            )
            .scalars()
            .all()
        )
        assert len(refund_rows) == 1
        assert refund_rows[0].amount_fen == POLISH_TEN_K_FEN
        assert refund_rows[0].task_id == created["id"]
    finally:
        session.close()
    assert_ledger_reconciles(db_session_factory, user.id)


def test_cancel_twice_conflicts_and_does_not_refund_twice(
    client: TestClient, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user(card_key="card-cancel-twice", balance_fen=5000)
    headers = {"X-Card-Key": user.card_key}
    created = client.post(
        "/api/optimization/sessions",
        json={"text": "第一段。", "mode": "paper_polish"},
        headers=headers,
    ).json()
    assert client.post(f"/api/optimization/sessions/{created['id']}/cancel", headers=headers).status_code == 200

    response = client.post(f"/api/optimization/sessions/{created['id']}/cancel", headers=headers)

    assert response.status_code == 409
    assert _balance_fen(db_session_factory, user.id) == 5000
    assert_ledger_reconciles(db_session_factory, user.id)


def test_cancel_completed_session_conflicts_without_refund(
    client: TestClient, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user(card_key="card-done", balance_fen=5000)
    headers = {"X-Card-Key": user.card_key}
    created = client.post(
        "/api/optimization/sessions",
        json={"text": "第一段。", "mode": "paper_polish"},
        headers=headers,
    ).json()
    # 测试直接置终态（模拟 Worker 已完成；终态迁移归 Worker 拥有）
    session = db_session_factory()
    try:
        target = session.get(OptimizationSession, created["id"])
        target.status = "completed"
        session.commit()
    finally:
        session.close()

    response = client.post(f"/api/optimization/sessions/{created['id']}/cancel", headers=headers)

    assert response.status_code == 409
    assert _balance_fen(db_session_factory, user.id) == 5000 - 1  # 预扣（4 字 ceil 折算 1 分）未退
    assert_ledger_reconciles(db_session_factory, user.id)


def test_cancel_other_users_session_is_invisible_and_unrefunded(
    client: TestClient, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    owner = funded_user(card_key="card-cancel-owner", balance_fen=5000)
    stranger = funded_user(card_key="card-cancel-stranger", balance_fen=5000)
    created = client.post(
        "/api/optimization/sessions",
        json={"text": "第一段。", "mode": "paper_polish"},
        headers={"X-Card-Key": owner.card_key},
    ).json()

    response = client.post(
        f"/api/optimization/sessions/{created['id']}/cancel",
        headers={"X-Card-Key": stranger.card_key},
    )

    assert response.status_code == 404
    assert _balance_fen(db_session_factory, stranger.id) == 5000
    assert _balance_fen(db_session_factory, owner.id) == 5000 - 1  # 预扣未退
    assert_ledger_reconciles(db_session_factory, owner.id)


def test_create_session_over_text_cap_rejected_422_with_zero_side_effects(
    client: TestClient, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user(card_key="card-cap", balance_fen=5000)

    response = client.post(
        "/api/optimization/sessions",
        json={"text": "字" * (DEFAULT_TEXT_CAP + 1), "mode": "paper_polish"},
        headers={"X-Card-Key": user.card_key},
    )

    assert response.status_code == 422
    # 零副作用：无会话行、无段落行、余额不变、账本零变动，SUM(账本)==余额
    assert _session_rows(db_session_factory) == []
    assert _balance_fen(db_session_factory, user.id) == 5000
    session = db_session_factory()
    try:
        assert (
            session.execute(
                select(func.count(LedgerEntry.id)).where(LedgerEntry.user_id == user.id)
            ).scalar_one()
            == 1  # 仅发卡 issue 行
        )
    finally:
        session.close()
    assert_ledger_reconciles(db_session_factory, user.id)


def test_create_session_text_exactly_at_cap_is_accepted(
    client: TestClient, funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    user = funded_user(card_key="card-cap-edge", balance_fen=5000)

    response = client.post(
        "/api/optimization/sessions",
        json={"text": "字" * DEFAULT_TEXT_CAP, "mode": "paper_polish"},
        headers={"X-Card-Key": user.card_key},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["word_count"] == DEFAULT_TEXT_CAP
    assert body["charged_fen"] == 1500  # ceil(50000 × 300 / 10000)
    assert_ledger_reconciles(db_session_factory, user.id)


def test_text_cap_reads_settings_at_runtime(
    client: TestClient,
    funded_user: Callable[..., User],
    db_session_factory: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上限唯一来源是 settings（.env + 重启）：运行时生效，可被 monkeypatch。"""
    monkeypatch.setattr(settings, "max_session_text_chars", 100)
    user = funded_user(card_key="card-cap-runtime", balance_fen=5000)

    response = client.post(
        "/api/optimization/sessions",
        json={"text": "字" * 101, "mode": "paper_polish"},
        headers={"X-Card-Key": user.card_key},
    )

    assert response.status_code == 422
    assert _session_rows(db_session_factory) == []


def test_cancel_refund_conflict_maps_409_and_rolls_back_to_cancellable(
    client: TestClient,
    funded_user: Callable[..., User],
    db_session_factory: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退款竞争输掉（RefundConflict）≠ 500：对方已并发落终态并退款，本取消
    输掉——事务整体回滚（cancelled 不提交），对外与"已不可取消"同型 409，
    会话回到 cancellable 态，资金状态与对方退款后的事实一致（钱只退一次）。
    """
    user = funded_user(card_key="card-cancel-conflict", balance_fen=5000)
    headers = {"X-Card-Key": user.card_key}
    created = client.post(
        "/api/optimization/sessions",
        json={"text": "第一段。", "mode": "paper_polish"},
        headers=headers,
    ).json()
    balance_after_charge = _balance_fen(db_session_factory, user.id)

    def conflict(*_args: object, **_kwargs: object) -> bool:
        raise billing.RefundConflict("注入：并发退款竞争输掉")

    monkeypatch.setattr(billing, "refund", conflict)

    response = client.post(f"/api/optimization/sessions/{created['id']}/cancel", headers=headers)

    assert response.status_code == 409
    # 事务回滚：会话行仍是 queued（cancellable），零退款行，余额未被本路径触碰
    assert _session_rows(db_session_factory)[0].status == "queued"
    assert _refund_rows(db_session_factory, created["id"]) == []
    assert _balance_fen(db_session_factory, user.id) == balance_after_charge
    assert_ledger_reconciles(db_session_factory, user.id)


def test_finalize_with_refund_loses_ownership_without_touching_ledger(
    funded_user: Callable[..., User], db_session_factory: sessionmaker
) -> None:
    """终态收场原语的归属守卫：状态已被并发落定为 completed 时返回 False
    且账本零写入（输家无权再动资金）。"""
    user = funded_user(card_key="card-finalize", balance_fen=5000)
    session = db_session_factory()
    try:
        row = optimization.create_session(session, user, text="第一段。", mode="paper_polish")
        session.commit()
        session.refresh(row)
        row.status = optimization.SESSION_COMPLETED  # 模拟对方（Worker）已落终态
        session.commit()
        session_id = row.id
    finally:
        session.close()

    with short_tx(db_session_factory) as tx:
        owned = optimization.finalize_with_refund(
            tx,
            session_id,
            terminal_status=optimization.SESSION_FAILED,
            from_statuses=(optimization.SESSION_RUNNING,),
            reason="注入：归属输掉路径",
        )

    assert owned is False
    assert _refund_rows(db_session_factory, session_id) == []
    assert _balance_fen(db_session_factory, user.id) == 5000 - 1  # 预扣原样在账
    assert_ledger_reconciles(db_session_factory, user.id)
