"""管理端账本流水查询（issue #27，HTTP API 接缝）。

覆盖验收：按用户查询返回该用户全部账本行（分页，字段含类型/带符号金额/
变动后余额/原因/时间）、全局最近流水按时间倒序分页、无管理会话 cookie
一律 401、查询接口只读（调用前后账本行数与用户余额不变）。

造数走计费服务（billing）而非直接插账本行：生产中账本行只由计费服务产生，
测试数据经同一条写路径生成，行为与真实流水一致。
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import billing
from app.models import LedgerEntry, User


@pytest.fixture()
def ledger_user_ids(db_session_factory: sessionmaker) -> tuple[int, int]:
    """两个用户各产生真实双轨流水：u1 五行（issue/adjust/charge/refund/
    usage_reserve），u2 一行（usage_adjust）。全部经计费服务写入。"""
    session = db_session_factory()
    try:
        u1 = User(card_key="ledger-user-aaaa")
        u2 = User(card_key="ledger-user-bbbb")
        session.add_all([u1, u2])
        session.flush()
        billing.grant_initial_quota(
            session, u1.id, balance_fen=1000, usage_count=5, reason="发卡初始额度"
        )
        billing.adjust_balance(session, u1.id, 500, reason="线下收款充值")
        billing.charge(session, u1.id, 300, task_id=11, reason="润色预扣费")
        billing.refund(session, u1.id, 300, task_id=11, reason="任务失败自动退款")
        billing.reserve_usage(session, u1.id, 2, task_id=None, reason="排版预留次数")
        billing.adjust_usage_limit(session, u2.id, 10, reason="次数额度充值")
        session.commit()
        return u1.id, u2.id
    finally:
        session.close()


# ---- 鉴权：无管理会话 cookie 一律 401 ----


def test_ledger_endpoints_require_admin_session(client: TestClient) -> None:
    assert client.get("/api/admin/ledger").status_code == 401
    assert client.get("/api/admin/users/1/ledger").status_code == 401


# ---- 按用户查询 ----


def test_user_ledger_returns_all_rows_newest_first(
    logged_in_client: TestClient, ledger_user_ids: tuple[int, int]
) -> None:
    user_id = ledger_user_ids[0]

    response = logged_in_client.get(f"/api/admin/users/{user_id}/ledger")

    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 5
    assert page["page"] == 1
    assert page["page_size"] == 20
    assert page["user_id"] == user_id
    assert page["card_key"] == "ledg****aaaa"  # 脱敏卡密，供流水页标题展示
    # 新流水在前：与写入顺序相反
    assert [row["kind"] for row in page["items"]] == [
        "usage_reserve",
        "refund",
        "charge",
        "adjust",
        "issue",
    ]
    charge_row = page["items"][2]
    assert charge_row["amount_fen"] == -300  # 带符号增量
    assert charge_row["balance_after_fen"] == 1200
    assert charge_row["task_id"] == 11
    assert charge_row["reason"] == "润色预扣费"
    assert charge_row["created_at"]
    usage_row = page["items"][0]
    assert usage_row["amount_fen"] == 0  # 次数轨不碰余额
    assert usage_row["usage_after"] == 3
    assert usage_row["balance_after_fen"] == 1500


def test_user_ledger_paginates(
    logged_in_client: TestClient, ledger_user_ids: tuple[int, int]
) -> None:
    user_id = ledger_user_ids[0]

    first = logged_in_client.get(
        f"/api/admin/users/{user_id}/ledger", params={"page": 1, "page_size": 2}
    ).json()
    second = logged_in_client.get(
        f"/api/admin/users/{user_id}/ledger", params={"page": 2, "page_size": 2}
    ).json()
    third = logged_in_client.get(
        f"/api/admin/users/{user_id}/ledger", params={"page": 3, "page_size": 2}
    ).json()

    assert first["total"] == 5
    assert [row["kind"] for row in first["items"]] == ["usage_reserve", "refund"]
    assert [row["kind"] for row in second["items"]] == ["charge", "adjust"]
    assert [row["kind"] for row in third["items"]] == ["issue"]


def test_user_ledger_unknown_user_404(logged_in_client: TestClient) -> None:
    assert logged_in_client.get("/api/admin/users/999/ledger").status_code == 404


def test_user_ledger_rejects_out_of_range_paging(
    logged_in_client: TestClient, ledger_user_ids: tuple[int, int]
) -> None:
    user_id = ledger_user_ids[0]
    assert (
        logged_in_client.get(f"/api/admin/users/{user_id}/ledger", params={"page": 0}).status_code
        == 422
    )
    assert (
        logged_in_client.get(
            f"/api/admin/users/{user_id}/ledger", params={"page_size": 101}
        ).status_code
        == 422
    )


# ---- 全局最近流水 ----


def test_global_ledger_lists_all_users_newest_first(
    logged_in_client: TestClient, ledger_user_ids: tuple[int, int], make_user: Callable[..., User]
) -> None:
    """无流水用户不出现；全局页按时间倒序覆盖全部用户。"""
    make_user(card_key="ledger-user-empty")

    response = logged_in_client.get("/api/admin/ledger")

    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 6
    assert page["card_key"] is None  # 全局页不携带任何单用户信息
    items = page["items"]
    assert [row["id"] for row in items] == sorted(
        (row["id"] for row in items), reverse=True
    )
    assert {row["user_id"] for row in items} == set(ledger_user_ids)
    # 最新一行是 u2 的次数调整
    assert items[0]["kind"] == "usage_adjust"
    assert items[0]["reason"] == "次数额度充值"


def test_global_ledger_paginates(
    logged_in_client: TestClient, ledger_user_ids: tuple[int, int]
) -> None:
    page = logged_in_client.get(
        "/api/admin/ledger", params={"page": 2, "page_size": 4}
    ).json()

    assert page["total"] == 6
    assert page["page"] == 2
    assert len(page["items"]) == 2


# ---- 只读性：查询不产生账本行、不改变余额 ----


def test_ledger_queries_are_read_only(
    logged_in_client: TestClient, ledger_user_ids: tuple[int, int], db_session_factory: sessionmaker
) -> None:
    user_id = ledger_user_ids[0]

    def snapshot() -> tuple[int, list[tuple[int, int]]]:
        session = db_session_factory()
        try:
            balances = [
                (u.id, u.balance_fen) for u in session.query(User).order_by(User.id).all()
            ]
            return session.query(LedgerEntry).count(), balances
        finally:
            session.close()

    before = snapshot()

    assert logged_in_client.get("/api/admin/ledger").status_code == 200
    assert logged_in_client.get("/api/admin/ledger", params={"page": 2}).status_code == 200
    assert logged_in_client.get(f"/api/admin/users/{user_id}/ledger").status_code == 200

    after: Any = snapshot()
    assert after == before
