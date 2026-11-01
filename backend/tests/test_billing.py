"""计费服务资金不变量（服务接缝，真实 SQLite 文件库）。

覆盖 issue #7 验收的资金侧：条件原子 UPDATE + rowcount 校验使并发扣减
不透支；退款幂等（含并发竞争）；次数预留/释放独立于余额轨；
SUM(账本)==用户余额 恒成立。只测计费服务的公开行为，不测内部调用结构。

并发测试用多线程各持独立连接（生产同款 WAL + busy_timeout 配置），
SQLite 单写者串行化写事务——这正是计费服务依赖的并发模型。
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import billing
from app.billing import BillingError, InsufficientBalance, InsufficientUsage, RefundConflict
from app.models import LedgerEntry, User


@pytest.fixture()
def funded_user(make_user: Callable[..., User], db_session_factory: sessionmaker) -> User:
    """经计费服务入账的用户：余额 12500 分 + 30 次额度。"""
    user = make_user(card_key="card-funded")
    session = db_session_factory()
    try:
        billing.grant_initial_quota(
            session, user.id, balance_fen=12500, usage_count=30, reason="发卡初始额度"
        )
        session.commit()
        return user
    finally:
        session.close()


def _balance(factory: sessionmaker, user_id: int) -> int:
    session = factory()
    try:
        return session.execute(select(User.balance_fen).where(User.id == user_id)).scalar_one()
    finally:
        session.close()


def _ledger(factory: sessionmaker, user_id: int) -> list[LedgerEntry]:
    session = factory()
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


def assert_ledger_reconciles(factory: sessionmaker, user_id: int) -> None:
    """恒成立式验收：SUM(账本)==余额，且每行 balance_after 与流水累计一致。"""
    session = factory()
    try:
        balance = session.execute(
            select(User.balance_fen).where(User.id == user_id)
        ).scalar_one()
        # 计费服务自带的账本口径查询与用户行余额一致
        assert billing.ledger_balance_fen(session, user_id) == balance
    finally:
        session.close()
    rows = _ledger(factory, user_id)
    running = 0
    for row in rows:
        running += row.amount_fen
        assert row.balance_after_fen == running, f"balance_after 断链于行 {row.id}"
    assert running == balance, f"SUM(账本)={running} != 余额={balance}"


# ---- 余额轨：扣减 ----


def test_charge_deducts_balance_and_writes_ledger_row(
    db_session_factory: sessionmaker, funded_user: User
) -> None:
    session = db_session_factory()
    try:
        balance_after = billing.charge(session, funded_user.id, 500, task_id=101, reason="预扣费")
        session.commit()
    finally:
        session.close()

    assert balance_after == 12000
    assert _balance(db_session_factory, funded_user.id) == 12000
    (row,) = [r for r in _ledger(db_session_factory, funded_user.id) if r.kind == billing.KIND_CHARGE]
    assert row.amount_fen == -500
    assert row.balance_after_fen == 12000
    assert row.task_id == 101
    assert row.reason == "预扣费"
    assert_ledger_reconciles(db_session_factory, funded_user.id)


def test_charge_rejects_insufficient_balance_without_side_effects(
    db_session_factory: sessionmaker, funded_user: User
) -> None:
    session = db_session_factory()
    try:
        with pytest.raises(InsufficientBalance):
            billing.charge(session, funded_user.id, 12501, task_id=102, reason="预扣费")
        session.rollback()
    finally:
        session.close()

    assert _balance(db_session_factory, funded_user.id) == 12500
    assert all(r.kind == billing.KIND_ISSUE for r in _ledger(db_session_factory, funded_user.id))
    assert_ledger_reconciles(db_session_factory, funded_user.id)


def test_concurrent_charges_do_not_overdraft(
    db_session_factory: sessionmaker, funded_user: User
) -> None:
    """8 线程并发 70 笔各扣 200：余额 12500 恰好放行 62 笔，绝不透支。"""
    amount = 200
    expected_ok = 12500 // amount  # 62
    expected_left = 12500 - expected_ok * amount  # 100

    def attempt(n: int) -> bool:
        session = db_session_factory()
        try:
            billing.charge(session, funded_user.id, amount, task_id=1000 + n, reason="并发预扣费")
            session.commit()
            return True
        except InsufficientBalance:
            session.rollback()
            return False
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(70)))

    assert sum(results) == expected_ok
    assert _balance(db_session_factory, funded_user.id) == expected_left
    charge_rows = [
        r for r in _ledger(db_session_factory, funded_user.id) if r.kind == billing.KIND_CHARGE
    ]
    assert len(charge_rows) == expected_ok
    assert_ledger_reconciles(db_session_factory, funded_user.id)


# ---- 余额轨：退款 ----


def test_refund_is_idempotent_per_task(
    db_session_factory: sessionmaker, funded_user: User
) -> None:
    session = db_session_factory()
    try:
        billing.charge(session, funded_user.id, 500, task_id=201, reason="预扣费")
        session.commit()

        first = billing.refund(session, funded_user.id, 500, task_id=201, reason="任务失败退款")
        session.commit()
        second = billing.refund(session, funded_user.id, 500, task_id=201, reason="任务失败退款")
        session.commit()
    finally:
        session.close()

    assert first is True
    assert second is False
    assert _balance(db_session_factory, funded_user.id) == 12500  # 只退回一次
    refund_rows = [
        r for r in _ledger(db_session_factory, funded_user.id) if r.kind == billing.KIND_REFUND
    ]
    assert len(refund_rows) == 1
    assert_ledger_reconciles(db_session_factory, funded_user.id)


def test_concurrent_refunds_take_effect_once(
    db_session_factory: sessionmaker, funded_user: User
) -> None:
    session = db_session_factory()
    try:
        billing.charge(session, funded_user.id, 500, task_id=202, reason="预扣费")
        session.commit()
    finally:
        session.close()

    def attempt() -> bool:
        s = db_session_factory()
        try:
            ok = billing.refund(s, funded_user.id, 500, task_id=202, reason="并发退款")
            s.commit()
            return ok
        except RefundConflict:
            # 竞争输掉（守卫检查后对方先提交）：响亮异常，事务整体放弃
            s.rollback()
            return False
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))

    assert sorted(results) == [False, True]  # 恰好一方退款成功
    assert _balance(db_session_factory, funded_user.id) == 12500
    refund_rows = [
        r for r in _ledger(db_session_factory, funded_user.id) if r.kind == billing.KIND_REFUND
    ]
    assert len(refund_rows) == 1
    assert_ledger_reconciles(db_session_factory, funded_user.id)


def test_refund_race_after_prior_changes_raises_refund_conflict(
    db_session_factory: sessionmaker, funded_user: User
) -> None:
    """同事务先 adjust 再 refund，refund 在唯一索引上竞争输掉时抛
    RefundConflict——调用方被明确告知事务已失败，而不是像旧契约那样
    静默回滚把先做的 adjust 一并吞掉。

    竞争注入：SQLite 单写者下，带前置写的事务不可能输掉 flush 竞争（对手
    拿不到写锁），故往同一未决事务塞一行守卫看不见的同 task 未冲账 refund
    （autoflush=False 使其不进守卫查询），等价于对手在守卫之后、flush 之前
    提交了退款，让 flush 撞上 ux_ledger_refund_once_per_task。
    """
    session = db_session_factory()
    try:
        billing.adjust_balance(session, funded_user.id, 1000, reason="客户补偿")
        session.add(
            LedgerEntry(
                user_id=funded_user.id,
                task_id=203,
                kind=billing.KIND_REFUND,
                amount_fen=500,
                balance_after_fen=13000,
                reason="对手并发退款",
            )
        )
        with pytest.raises(RefundConflict):
            billing.refund(session, funded_user.id, 500, task_id=203, reason="任务失败退款")
        session.rollback()
    finally:
        session.close()

    # 调用方回滚后：竞争双方的 refund 行都没有落库，余额回到起点
    assert _balance(db_session_factory, funded_user.id) == 12500
    assert [r for r in _ledger(db_session_factory, funded_user.id) if r.kind == billing.KIND_REFUND] == []
    assert_ledger_reconciles(db_session_factory, funded_user.id)


# ---- 余额轨：管理员调整 ----


def test_adjust_balance_positive_and_negative(
    db_session_factory: sessionmaker, funded_user: User
) -> None:
    session = db_session_factory()
    try:
        after_topup = billing.adjust_balance(session, funded_user.id, 1000, reason="客户补偿")
        session.commit()
        after_deduct = billing.adjust_balance(session, funded_user.id, -300, reason="误充扣回")
        session.commit()
    finally:
        session.close()

    assert (after_topup, after_deduct) == (13500, 13200)
    adjust_rows = [
        r for r in _ledger(db_session_factory, funded_user.id) if r.kind == billing.KIND_ADJUST
    ]
    assert [(r.amount_fen, r.reason) for r in adjust_rows] == [
        (1000, "客户补偿"),
        (-300, "误充扣回"),
    ]
    assert_ledger_reconciles(db_session_factory, funded_user.id)


def test_adjust_balance_rejects_deduction_beyond_balance(
    db_session_factory: sessionmaker, funded_user: User
) -> None:
    session = db_session_factory()
    try:
        with pytest.raises(InsufficientBalance):
            billing.adjust_balance(session, funded_user.id, -12501, reason="超扣")
        session.rollback()
        with pytest.raises(ValueError):
            billing.adjust_balance(session, funded_user.id, 0, reason="零调整")
        session.rollback()
    finally:
        session.close()

    assert _balance(db_session_factory, funded_user.id) == 12500
    assert_ledger_reconciles(db_session_factory, funded_user.id)


# ---- 次数轨：预留/释放，独立于余额轨 ----


def test_reserve_and_release_usage_without_touching_balance(
    db_session_factory: sessionmaker, funded_user: User
) -> None:
    session = db_session_factory()
    try:
        after_reserve = billing.reserve_usage(session, funded_user.id, 3, task_id=None, reason="排版任务预留")
        session.commit()
        after_release = billing.release_usage(session, funded_user.id, 3, task_id=None, reason="任务失败释放")
        session.commit()
    finally:
        session.close()

    assert (after_reserve, after_release) == (27, 30)
    usage_rows = [
        r
        for r in _ledger(db_session_factory, funded_user.id)
        if r.kind in (billing.KIND_USAGE_RESERVE, billing.KIND_USAGE_RELEASE)
    ]
    assert [(r.amount_fen, r.usage_after) for r in usage_rows] == [(0, 27), (0, 30)]
    assert _balance(db_session_factory, funded_user.id) == 12500  # 次数轨分毫不碰余额
    assert_ledger_reconciles(db_session_factory, funded_user.id)


def test_reserve_rejects_insufficient_usage(
    db_session_factory: sessionmaker, funded_user: User
) -> None:
    session = db_session_factory()
    try:
        with pytest.raises(InsufficientUsage):
            billing.reserve_usage(session, funded_user.id, 31, task_id=None, reason="超预留")
        session.rollback()
        with pytest.raises(ValueError):
            billing.release_usage(session, funded_user.id, 1, task_id=None, reason="未预留先释放")
        session.rollback()
    finally:
        session.close()

    session = db_session_factory()
    try:
        usage_limit, usage_used = session.execute(
            select(User.usage_limit, User.usage_used).where(User.id == funded_user.id)
        ).one()
    finally:
        session.close()
    assert (usage_limit, usage_used) == (30, 0)


# ---- 入账与对账 ----


def test_grant_initial_quota_failure_leaves_no_partial_state_on_commit(
    db_session_factory: sessionmaker, make_user: Callable[..., User]
) -> None:
    """守卫失败后即使调用方不回滚、直接 commit，也无任何部分态落库。

    复现 #12 的原始缺陷场景：余额轨通过、次数轨失败（usage_used 已非零），
    旧实现会把余额 UPDATE 留在事务里，调用方一 commit 就凭空造钱（无账本行）。
    """
    user = make_user(card_key="card-used", usage_used=5)
    session = db_session_factory()
    try:
        with pytest.raises(BillingError, match="次数额度已非零"):
            billing.grant_initial_quota(
                session, user.id, balance_fen=2000, usage_count=10, reason="发卡初始额度"
            )
        session.commit()  # 调用方违反回滚纪律，也不允许产生部分态
    finally:
        session.close()

    session = db_session_factory()
    try:
        balance_fen, usage_limit, usage_used = session.execute(
            select(User.balance_fen, User.usage_limit, User.usage_used).where(
                User.id == user.id
            )
        ).one()
    finally:
        session.close()
    assert (balance_fen, usage_limit, usage_used) == (0, 0, 5)
    assert _ledger(db_session_factory, user.id) == []


def test_grant_initial_quota_reports_unknown_user(
    db_session_factory: sessionmaker,
) -> None:
    session = db_session_factory()
    try:
        with pytest.raises(BillingError, match="不存在"):
            billing.grant_initial_quota(
                session, 99999, balance_fen=100, usage_count=1, reason="发卡初始额度"
            )
        session.rollback()
    finally:
        session.close()


def test_grant_initial_quota_writes_issue_row(
    db_session_factory: sessionmaker, make_user: Callable[..., User]
) -> None:
    user = make_user(card_key="card-fresh")
    session = db_session_factory()
    try:
        billing.grant_initial_quota(
            session, user.id, balance_fen=12500, usage_count=30, reason="发卡初始额度"
        )
        session.commit()
    finally:
        session.close()

    (issue_row,) = [r for r in _ledger(db_session_factory, user.id) if r.kind == billing.KIND_ISSUE]
    assert (issue_row.amount_fen, issue_row.balance_after_fen, issue_row.usage_after) == (
        12500,
        12500,
        30,
    )
    assert_ledger_reconciles(db_session_factory, user.id)


def test_ledger_reconciles_across_mixed_dual_track_operations(
    db_session_factory: sessionmaker, funded_user: User
) -> None:
    """双轨混合操作后对账恒等式仍成立，且账本行数与操作一一对应。"""
    session = db_session_factory()
    try:
        billing.adjust_balance(session, funded_user.id, 500, reason="充值")
        billing.charge(session, funded_user.id, 300, task_id=301, reason="预扣费")
        billing.refund(session, funded_user.id, 300, task_id=301, reason="任务失败退款")
        billing.reserve_usage(session, funded_user.id, 2, task_id=None, reason="预留")
        billing.release_usage(session, funded_user.id, 1, task_id=None, reason="部分释放")
        session.commit()
    finally:
        session.close()

    assert _balance(db_session_factory, funded_user.id) == 13000
    assert len(_ledger(db_session_factory, funded_user.id)) == 6  # issue + 5 笔操作各一行
    assert_ledger_reconciles(db_session_factory, funded_user.id)


def test_charge_unknown_user_fails_without_ledger_row(db_session_factory: sessionmaker) -> None:
    session = db_session_factory()
    try:
        with pytest.raises(InsufficientBalance):
            billing.charge(session, 99999, 100, task_id=401, reason="不存在用户")
        session.rollback()
        with pytest.raises(BillingError):
            billing.refund(session, 99999, 100, task_id=402, reason="不存在用户")
        session.rollback()
    finally:
        session.close()
