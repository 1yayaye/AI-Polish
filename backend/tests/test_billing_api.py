"""计费 API 行为（HTTP 接缝）。

覆盖 issue #7 验收的 API 侧：管理端充值/调整落账本并带原因、余额不足
冲突、用户不存在、参数校验、未认证拦截；发卡初始额度经计费服务入账；
用户额度接口在充值后立即可见新值。只测状态码与返回体，不测内部调用。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import LedgerEntry, User


def _ledger_rows(db_session_factory: sessionmaker, user_id: int) -> list[LedgerEntry]:
    session = db_session_factory()
    try:
        return list(
            session.execute(
                select(LedgerEntry)
                .where(LedgerEntry.user_id == user_id)
                .order_by(LedgerEntry.id)
            ).scalars()
        )
    finally:
        session.close()


def test_admin_recharge_writes_ledger_and_user_sees_new_quota_immediately(
    logged_in_client: TestClient, make_user, db_session_factory: sessionmaker
) -> None:
    user = make_user(card_key="card-topup")

    response = logged_in_client.post(
        f"/api/admin/users/{user.id}/balance",
        json={"delta_fen": 5000, "reason": "客户补偿"},
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": user.id, "balance_fen": 5000}

    # 用户端下一次读取即见新值（spec #3 用户故事 13）
    quota_response = logged_in_client.get("/api/me/quota", headers={"X-Card-Key": "card-topup"})
    assert quota_response.status_code == 200
    assert quota_response.json()["balance_fen"] == 5000

    (row,) = _ledger_rows(db_session_factory, user.id)
    assert (row.kind, row.amount_fen, row.balance_after_fen) == ("adjust", 5000, 5000)
    assert row.reason == "客户补偿"


def test_admin_negative_adjust_reflected_to_user(
    logged_in_client: TestClient, make_user, db_session_factory: sessionmaker
) -> None:
    user = make_user(card_key="card-deduct", balance_fen=1000)

    response = logged_in_client.post(
        f"/api/admin/users/{user.id}/balance",
        json={"delta_fen": -400, "reason": "误充扣回"},
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": user.id, "balance_fen": 600}

    rows = _ledger_rows(db_session_factory, user.id)
    assert len(rows) == 1
    assert (rows[0].amount_fen, rows[0].balance_after_fen) == (-400, 600)
    assert rows[0].reason == "误充扣回"


def test_admin_adjust_beyond_balance_conflicts_without_side_effects(
    logged_in_client: TestClient, make_user, db_session_factory: sessionmaker
) -> None:
    user = make_user(card_key="card-poor", balance_fen=100)

    response = logged_in_client.post(
        f"/api/admin/users/{user.id}/balance",
        json={"delta_fen": -200, "reason": "超扣"},
    )

    assert response.status_code == 409
    assert _ledger_rows(db_session_factory, user.id) == []


def test_admin_adjust_unknown_user_not_found(logged_in_client: TestClient) -> None:
    response = logged_in_client.post(
        "/api/admin/users/999/balance",
        json={"delta_fen": 100, "reason": "查无此人"},
    )

    assert response.status_code == 404


def test_admin_adjust_rejects_zero_delta_and_empty_reason(
    logged_in_client: TestClient, make_user
) -> None:
    user = make_user(card_key="card-zero")

    zero_delta = logged_in_client.post(
        f"/api/admin/users/{user.id}/balance", json={"delta_fen": 0, "reason": "x"}
    )
    empty_reason = logged_in_client.post(
        f"/api/admin/users/{user.id}/balance", json={"delta_fen": 100, "reason": ""}
    )

    assert zero_delta.status_code == 422
    assert empty_reason.status_code == 422


def test_admin_adjust_requires_admin(client: TestClient, make_user) -> None:
    user = make_user(card_key="card-noauth")

    response = client.post(
        f"/api/admin/users/{user.id}/balance",
        json={"delta_fen": 100, "reason": "未登录"},
    )

    assert response.status_code == 401


def test_card_issuance_initial_quota_lands_in_ledger(
    logged_in_client: TestClient, db_session_factory: sessionmaker
) -> None:
    response = logged_in_client.post(
        "/api/admin/card-keys",
        json={"count": 3, "balance_fen": 12500, "usage_limit": 30},
    )

    assert response.status_code == 201
    card_keys = [item["card_key"] for item in response.json()["card_keys"]]

    # 每张新卡的 SUM(账本)==余额 恒成立，发卡即入账
    session = db_session_factory()
    try:
        users = session.execute(select(User).where(User.card_key.in_(card_keys))).scalars().all()
        assert len(users) == 3
        for user in users:
            ledger_sum = session.execute(
                select(func.sum(LedgerEntry.amount_fen)).where(LedgerEntry.user_id == user.id)
            ).scalar_one()
            assert ledger_sum == user.balance_fen == 12500
            issue_rows = (
                session.execute(
                    select(LedgerEntry).where(
                        LedgerEntry.user_id == user.id, LedgerEntry.kind == "issue"
                    )
                )
                .scalars()
                .all()
            )
            assert len(issue_rows) == 1
            assert issue_rows[0].usage_after == 30
    finally:
        session.close()
