"""管理端会话监控查询（issue #28，HTTP API 接缝）。

覆盖验收：会话列表按状态过滤并分页（每行含处理模式/计费字数/预扣金额/
所属用户脱敏卡密）、详情返回只读 model_snapshot 与各段落阶段状态（含
失败段落）及该会话的账本行（预扣与退款，退款落账即可见）、无管理会话
cookie 一律 401、查询只读（会话/段落状态不迁移、账本不增行、余额不变）。

造数走真实写路径：会话经 optimization.create_session（含预扣费）创建，
失败收场经 optimization.finalize_with_refund（含自动退款），取消经
optimization.cancel_session——管理端查询看到的就是这些路径产出的状态。
running 态与 model_snapshot 由 Worker 认领时一次写入，测试直接置库等价
于"Worker 已认领"的既成事实（Worker 行为另有 test_worker 覆盖）。
"""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from app import billing, optimization
from app.models import LedgerEntry, OptimizationSession, Segment, User

# 三个自然段共 18 个非空白字符：polish 定价 ceil(18*300/10000)=1 分
THREE_PARAGRAPHS = "第一段正文。\n第二段正文。\n第三段正文。"
WORD_COUNT = 18
CHARGED_FEN = 1
CLAIMED_MODEL = "test-model-v1"
MASKED_CARD_KEY = "moni****aaaa"  # monitor-user-aaaa 脱敏后


def _create_session(
    db_session_factory: sessionmaker,
    user: User,
    *,
    mode: str = "paper_polish",
    text: str = THREE_PARAGRAPHS,
) -> int:
    session = db_session_factory()
    try:
        row = optimization.create_session(session, user, text=text, mode=mode)
        session.commit()
        return row.id
    finally:
        session.close()


def _claim(db_session_factory: sessionmaker, session_id: int) -> None:
    """置为 running + model_snapshot：等价于 Worker 已认领的既成事实。"""
    session = db_session_factory()
    try:
        session.execute(
            update(OptimizationSession)
            .where(OptimizationSession.id == session_id)
            .values(status=optimization.SESSION_RUNNING, model_snapshot=CLAIMED_MODEL)
        )
        session.commit()
    finally:
        session.close()


def _set_segment_status(
    db_session_factory: sessionmaker, session_id: int, seq: int, status: str
) -> None:
    session = db_session_factory()
    try:
        session.execute(
            update(Segment)
            .where(Segment.session_id == session_id, Segment.seq == seq)
            .values(status=status)
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture()
def monitor_user(
    make_user: Callable[..., User], db_session_factory: sessionmaker
) -> User:
    """经计费服务入账的用户：预扣与退款都有真实账本行可对。"""
    user = make_user(card_key="monitor-user-aaaa")
    session = db_session_factory()
    try:
        billing.grant_initial_quota(
            session, user.id, balance_fen=2000, usage_count=0, reason="发卡初始额度"
        )
        session.commit()
        return user
    finally:
        session.close()


@pytest.fixture()
def session_ids(
    monitor_user: User, db_session_factory: sessionmaker
) -> dict[str, int]:
    """四种状态各一个会话：queued / running / failed（含失败段落 + 退款）/
    cancelled（含退款）。"""
    queued = _create_session(db_session_factory, monitor_user)
    running = _create_session(db_session_factory, monitor_user)
    failed = _create_session(db_session_factory, monitor_user)
    cancelled = _create_session(db_session_factory, monitor_user)

    _claim(db_session_factory, running)
    _claim(db_session_factory, failed)  # 终态收场要求会话先处于 running
    _set_segment_status(db_session_factory, failed, seq=1, status="failed")

    session = db_session_factory()
    try:
        assert optimization.finalize_with_refund(
            session,
            failed,
            terminal_status=optimization.SESSION_FAILED,
            from_statuses=(optimization.SESSION_RUNNING,),
            reason="任务失败自动退款",
        )
        user = session.get(User, monitor_user.id)
        assert user is not None
        optimization.cancel_session(session, user, cancelled)
        session.commit()
    finally:
        session.close()

    return {
        "queued": queued,
        "running": running,
        "failed": failed,
        "cancelled": cancelled,
    }


# ---- 鉴权：无管理会话 cookie 一律 401 ----


def test_session_endpoints_require_admin_session(client: TestClient) -> None:
    assert client.get("/api/admin/sessions").status_code == 401
    assert client.get("/api/admin/sessions/1").status_code == 401


# ---- 列表：过滤、分页、行字段 ----


def test_session_list_newest_first_with_required_fields(
    logged_in_client: TestClient, session_ids: dict[str, int], monitor_user: User
) -> None:
    response = logged_in_client.get("/api/admin/sessions")

    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 4
    assert page["page"] == 1
    assert page["page_size"] == 20
    ids = [row["id"] for row in page["items"]]
    assert ids == sorted(ids, reverse=True)  # 新会话在前

    running = next(row for row in page["items"] if row["id"] == session_ids["running"])
    assert running["user_id"] == monitor_user.id
    assert running["card_key"] == MASKED_CARD_KEY  # 所属用户脱敏卡密
    assert running["status"] == "running"
    assert running["mode"] == "paper_polish"
    assert running["word_count"] == WORD_COUNT
    assert running["charged_fen"] == CHARGED_FEN
    assert running["model_snapshot"] == CLAIMED_MODEL

    queued = next(row for row in page["items"] if row["id"] == session_ids["queued"])
    assert queued["status"] == "queued"
    assert queued["model_snapshot"] is None  # 未认领就没有模型可看


def test_session_list_filters_by_each_status(
    logged_in_client: TestClient, session_ids: dict[str, int]
) -> None:
    for status_name in ("queued", "running", "failed", "cancelled"):
        response = logged_in_client.get("/api/admin/sessions", params={"status": status_name})

        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total"] == 1
        assert [row["id"] for row in page["items"]] == [session_ids[status_name]]


def test_session_list_rejects_status_outside_vocabulary(
    logged_in_client: TestClient, session_ids: dict[str, int]
) -> None:
    response = logged_in_client.get("/api/admin/sessions", params={"status": "bogus"})

    assert response.status_code == 422


def test_session_list_paginates(
    logged_in_client: TestClient, session_ids: dict[str, int]
) -> None:
    first = logged_in_client.get(
        "/api/admin/sessions", params={"page": 1, "page_size": 2}
    ).json()
    second = logged_in_client.get(
        "/api/admin/sessions", params={"page": 2, "page_size": 2}
    ).json()

    assert first["total"] == 4
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2
    assert {row["id"] for row in first["items"]}.isdisjoint(
        {row["id"] for row in second["items"]}
    )


def test_session_list_filters_by_user(
    logged_in_client: TestClient,
    session_ids: dict[str, int],
    monitor_user: User,
    make_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    """客诉排查闭环的最后一跳：凭卡密片段找到用户后，按 user_id 只看他的会话。"""
    other = make_user(card_key="monitor-user-bbbb")
    session = db_session_factory()
    try:
        billing.grant_initial_quota(
            session, other.id, balance_fen=2000, usage_count=0, reason="发卡初始额度"
        )
        session.commit()
    finally:
        session.close()
    other_session_id = _create_session(db_session_factory, other)

    mine = logged_in_client.get("/api/admin/sessions", params={"user_id": monitor_user.id})
    assert mine.status_code == 200
    page = mine.json()
    assert page["total"] == 4
    assert {row["user_id"] for row in page["items"]} == {monitor_user.id}
    assert other_session_id not in {row["id"] for row in page["items"]}

    theirs = logged_in_client.get("/api/admin/sessions", params={"user_id": other.id})
    assert theirs.status_code == 200
    assert [row["id"] for row in theirs.json()["items"]] == [other_session_id]

    # 两个过滤条件可叠加
    both = logged_in_client.get(
        "/api/admin/sessions", params={"user_id": monitor_user.id, "status": "failed"}
    )
    assert [row["id"] for row in both.json()["items"]] == [session_ids["failed"]]


# ---- 详情：model_snapshot、段落阶段状态、该会话账本行 ----


def test_session_detail_shows_snapshot_failed_segment_and_ledger(
    logged_in_client: TestClient, session_ids: dict[str, int]
) -> None:
    response = logged_in_client.get(f"/api/admin/sessions/{session_ids['failed']}")

    assert response.status_code == 200
    detail = response.json()
    assert detail["status"] == "failed"
    assert detail["model_snapshot"] == CLAIMED_MODEL  # 当时实际用的模型
    assert detail["charged_fen"] == CHARGED_FEN

    # 段落状态：失败段落原样可见，其余保持 pending
    statuses = {segment["seq"]: segment["status"] for segment in detail["segments"]}
    assert statuses == {0: "pending", 1: "failed", 2: "pending"}
    assert all(segment["stage"] == "polish" for segment in detail["segments"])

    # 该会话的账本行：先预扣后退款，全额退回（变动后余额回到预扣前水平）
    kinds = [row["kind"] for row in detail["ledger"]]
    assert kinds == ["charge", "refund"]
    charge, refund = detail["ledger"]
    assert charge["amount_fen"] == -CHARGED_FEN
    assert refund["amount_fen"] == CHARGED_FEN
    assert refund["balance_after_fen"] == charge["balance_after_fen"]
    assert refund["reason"] == "任务失败自动退款"


def test_session_detail_queued_has_no_snapshot_and_no_refund(
    logged_in_client: TestClient, session_ids: dict[str, int]
) -> None:
    response = logged_in_client.get(f"/api/admin/sessions/{session_ids['queued']}")

    assert response.status_code == 200
    detail = response.json()
    assert detail["model_snapshot"] is None
    assert [row["kind"] for row in detail["ledger"]] == ["charge"]  # 只有预扣行


def test_session_detail_unknown_session_404(logged_in_client: TestClient) -> None:
    assert logged_in_client.get("/api/admin/sessions/999").status_code == 404


# ---- 只读性：查询不迁移状态、不增账本行、不改余额 ----


def test_session_queries_are_read_only(
    logged_in_client: TestClient,
    session_ids: dict[str, int],
    db_session_factory: sessionmaker,
) -> None:
    def snapshot() -> tuple[
        list[tuple[int, str, str | None]], list[tuple[int, str]], list[tuple[int, int]], int
    ]:
        session = db_session_factory()
        try:
            sessions = [
                (s.id, s.status, s.model_snapshot)
                for s in session.query(OptimizationSession).order_by(OptimizationSession.id)
            ]
            segments = [(g.id, g.status) for g in session.query(Segment).order_by(Segment.id)]
            balances = [(u.id, u.balance_fen) for u in session.query(User).order_by(User.id)]
            return sessions, segments, balances, session.query(LedgerEntry).count()
        finally:
            session.close()

    before = snapshot()

    assert logged_in_client.get("/api/admin/sessions").status_code == 200
    assert logged_in_client.get(
        "/api/admin/sessions", params={"status": "failed"}
    ).status_code == 200
    for session_id in session_ids.values():
        assert logged_in_client.get(f"/api/admin/sessions/{session_id}").status_code == 200

    assert snapshot() == before
