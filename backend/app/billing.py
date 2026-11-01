"""计费服务：全系统唯一的额度变动入口，双轨（余额 + 次数）共用本模块与账本。

余额轨（Workspace 按字数扣费，GLOSSARY：余额）与次数轨（Word Formatter
按次预留，GLOSSARY：用量上限）在此共享同一事务边界与账本——禁止两条轨
各写各的（rewrite-plan §3）。

## 接口约定（调用方需要知道的全部）

- 每个函数接收一个处于"短事务"中的 Session（app.db 纪律），变动与账本行
  在同一事务内落库，commit 由调用方的请求边界（get_session）完成。
- 全部资金变动走条件原子 UPDATE + rowcount 校验（``UPDATE ... WHERE
  balance >= amount``）：并发下不可能透支；余额不足抛
  :class:`InsufficientBalance`，次数不足抛 :class:`InsufficientUsage`，
  永不产生负值。
- 每次变动写一行账本：amount_fen 为带符号增量，故 SUM(账本) ==
  用户当前余额恒成立；balance_after_fen 为变动后余额（由 UPDATE 之后的
  同事务读回得到，SQLite 单写者串行保证该值属于本次变动）。
- reason 必填：账本即业务监控（rewrite-plan §5），无原因的变动不存在。
- task_id：charge/refund 必传（新流水一律可追溯到任务）；usage 两个操作
  可空（Word Formatter 任务可能无任务行）。

## 退款幂等（崩溃重试不双倍退款）

"已退款"状态持久化在账本里：部分唯一索引 ux_ledger_refund_once_per_task
保证每个 task 至多一条 refund 行。refund 先查守卫（对方的退款行在本事务
可见时返回 False 而非报错），数据库唯一索引兜住并发竞争。竞争输掉（守卫
检查之后对方才提交同 task 退款）时抛 :class:`RefundConflict`：本事务里
可能已有调用方的其他未提交变动，refund 无权替调用方回滚（静默回滚会把这些
变动一并吞掉），由调用方显式放弃整个事务。

非资金错误（释放超过预留量、金额非正）按编程错误抛 ValueError，不设
专门的恢复路径——调用方写错了就该当场炸出来。
"""

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import LedgerEntry, User

KIND_ISSUE = "issue"
KIND_CHARGE = "charge"
KIND_REFUND = "refund"
KIND_ADJUST = "adjust"
KIND_USAGE_RESERVE = "usage_reserve"
KIND_USAGE_RELEASE = "usage_release"


class BillingError(Exception):
    """计费失败基类：调用方可恢复的业务失败。"""


class InsufficientBalance(BillingError):
    """余额不足以完成本次扣减。"""


class InsufficientUsage(BillingError):
    """剩余次数不足以完成本次预留。"""


class RefundConflict(BillingError):
    """退款竞争输掉：对方在守卫检查之后提交了同 task 的退款。

    本事务必须整体放弃（调用方回滚），不能只当作"已退过"继续提交——
    事务里可能还有其他未提交变动，静默回滚会丢数据，响亮失败才安全。
    """


def ledger_balance_fen(session: Session, user_id: int) -> int:
    """账本口径的余额（SUM(amount_fen)），对账用；无流水视为 0。"""
    total = session.execute(
        select(func.sum(LedgerEntry.amount_fen)).where(LedgerEntry.user_id == user_id)
    ).scalar_one()
    return total or 0


def _append(
    session: Session,
    user_id: int,
    *,
    kind: str,
    amount_fen: int,
    balance_after_fen: int,
    task_id: int | None,
    reason: str,
    usage_after: int | None = None,
) -> None:
    session.add(
        LedgerEntry(
            user_id=user_id,
            task_id=task_id,
            kind=kind,
            amount_fen=amount_fen,
            balance_after_fen=balance_after_fen,
            usage_after=usage_after,
            reason=reason,
        )
    )


def _adjust_balance(
    session: Session, user_id: int, delta_fen: int, *, min_balance: int | None
) -> int | None:
    """条件原子 UPDATE 余额并同事务读回变动后值；无行命中返回 None。

    条件（min_balance）与增量在同一语句内求值，由 SQLite 单写者串行化，
    并发下不可能透支。
    """
    stmt = update(User).where(User.id == user_id)
    if min_balance is not None:
        stmt = stmt.where(User.balance_fen >= min_balance)
    if session.execute(stmt.values(balance_fen=User.balance_fen + delta_fen)).rowcount != 1:
        return None
    balance_after: int = session.execute(
        select(User.balance_fen).where(User.id == user_id)
    ).scalar_one()
    return balance_after


def charge(session: Session, user_id: int, amount_fen: int, *, task_id: int, reason: str) -> int:
    """预扣费：按预计字数从余额一次性扣减（GLOSSARY：预扣费）。

    返回变动后余额；余额不足抛 InsufficientBalance（此时无任何变动）。
    amount_fen 必须为正。同一 task 重复 charge 由调用方（任务状态机）避免，
    本函数不重复拦截。
    """
    if amount_fen <= 0:
        raise ValueError(f"charge amount must be positive, got {amount_fen}")
    balance_after = _adjust_balance(
        session, user_id, -amount_fen, min_balance=amount_fen
    )
    if balance_after is None:
        raise InsufficientBalance(f"余额不足：需要 {amount_fen} 分")
    _append(
        session,
        user_id,
        kind=KIND_CHARGE,
        amount_fen=-amount_fen,
        balance_after_fen=balance_after,
        task_id=task_id,
        reason=reason,
    )
    return balance_after


def refund(session: Session, user_id: int, amount_fen: int, *, task_id: int, reason: str) -> bool:
    """按 task 幂等退款（任务失败自动退款，GLOSSARY：预扣费）。

    返回 True 表示本次调用完成了退款；False 表示该 task 已退过（幂等：
    对方的退款行在本事务可见）。竞争输掉（守卫检查之后对方才提交）抛
    RefundConflict，调用方须回滚整个事务。amount_fen 必须为正且等于原
    扣减额。见模块级"退款幂等"一节。
    """
    if amount_fen <= 0:
        raise ValueError(f"refund amount must be positive, got {amount_fen}")
    already_refunded = session.execute(
        select(LedgerEntry.id).where(
            LedgerEntry.task_id == task_id, LedgerEntry.kind == KIND_REFUND
        )
    ).first()
    if already_refunded is not None:
        return False
    balance_after = _adjust_balance(session, user_id, amount_fen, min_balance=None)
    if balance_after is None:
        raise BillingError(f"退款失败：用户 {user_id} 不存在")
    _append(
        session,
        user_id,
        kind=KIND_REFUND,
        amount_fen=amount_fen,
        balance_after_fen=balance_after,
        task_id=task_id,
        reason=reason,
    )
    try:
        session.flush()
    except IntegrityError:
        # 并发竞争输掉 ux_ledger_refund_once_per_task：对方在守卫检查之后
        # 提交了同 task 退款。本事务可能已有调用方的其他变动，refund 无权
        # 静默回滚（会吞掉它们），响亮抛出让调用方决定整个事务的去留。
        raise RefundConflict(f"task {task_id} 的退款已被并发请求完成") from None
    return True


def adjust_balance(session: Session, user_id: int, delta_fen: int, *, reason: str) -> int:
    """运营商充值/调整余额：delta_fen 为正即充值，为负即扣减调整。

    返回变动后余额；负向调整超过当前余额抛 InsufficientBalance。用户必须
    存在（user_id 外键兜底，调用方负责给出有效用户）。
    """
    if delta_fen == 0:
        raise ValueError("adjust delta must be non-zero")
    min_balance = -delta_fen if delta_fen < 0 else None
    balance_after = _adjust_balance(session, user_id, delta_fen, min_balance=min_balance)
    if balance_after is None:
        if delta_fen < 0:
            raise InsufficientBalance("余额不足，无法完成本次扣减调整")
        raise BillingError(f"充值失败：用户 {user_id} 不存在")
    _append(
        session,
        user_id,
        kind=KIND_ADJUST,
        amount_fen=delta_fen,
        balance_after_fen=balance_after,
        task_id=None,
        reason=reason,
    )
    return balance_after


def grant_initial_quota(
    session: Session, user_id: int, *, balance_fen: int, usage_count: int, reason: str
) -> None:
    """发卡初始双轨额度入账。用户必须为全新状态（双轨均为 0，条件守卫）。

    用户行创建时资金字段恒为零，全部资金由此入账——"users 的资金字段只在
    计费服务内变动"因此无例外成立。写一行 issue 账本同时覆盖两轨。
    """
    if balance_fen < 0 or usage_count < 0:
        raise ValueError("initial quota must be non-negative")
    if session.execute(
        update(User)
        .where(User.id == user_id, User.balance_fen == 0)
        .values(balance_fen=balance_fen)
    ).rowcount != 1:
        raise BillingError(f"发卡入账失败：用户 {user_id} 不存在或余额已非零")
    if session.execute(
        update(User)
        .where(User.id == user_id, User.usage_limit == 0, User.usage_used == 0)
        .values(usage_limit=usage_count)
    ).rowcount != 1:
        raise BillingError(f"发卡入账失败：用户 {user_id} 次数额度已非零")
    _append(
        session,
        user_id,
        kind=KIND_ISSUE,
        amount_fen=balance_fen,
        balance_after_fen=balance_fen,
        task_id=None,
        reason=reason,
        usage_after=usage_count,
    )


def _adjust_usage(
    session: Session,
    user_id: int,
    delta_used: int,
    *,
    require_remaining: int | None = None,
    require_used_at_least: int | None = None,
) -> int | None:
    """条件原子 UPDATE 次数轨并同事务读回变动后剩余次数；无行命中返回 None。

    reserve 用 require_remaining（剩余次数下限），release 用
    require_used_at_least（已用下限，防释放超过预留把 usage_used 打负）。
    """
    stmt = update(User).where(User.id == user_id)
    if require_remaining is not None:
        stmt = stmt.where(User.usage_limit - User.usage_used >= require_remaining)
    if require_used_at_least is not None:
        stmt = stmt.where(User.usage_used >= require_used_at_least)
    if session.execute(stmt.values(usage_used=User.usage_used + delta_used)).rowcount != 1:
        return None
    usage_limit, usage_used = session.execute(
        select(User.usage_limit, User.usage_used).where(User.id == user_id)
    ).one()
    return usage_limit - usage_used


def _record_usage(
    session: Session,
    user_id: int,
    *,
    kind: str,
    usage_after: int,
    task_id: int | None,
    reason: str,
) -> None:
    """次数轨变动记账：amount_fen 恒为 0（不碰余额），usage_after 记剩余。"""
    balance_after: int = session.execute(
        select(User.balance_fen).where(User.id == user_id)
    ).scalar_one()
    _append(
        session,
        user_id,
        kind=kind,
        amount_fen=0,
        balance_after_fen=balance_after,
        task_id=task_id,
        reason=reason,
        usage_after=usage_after,
    )


def reserve_usage(
    session: Session, user_id: int, count: int = 1, *, task_id: int | None, reason: str
) -> int:
    """次数轨预留：剩余次数不足抛 InsufficientUsage，不碰余额。返回变动后剩余次数。"""
    if count <= 0:
        raise ValueError(f"reserve count must be positive, got {count}")
    usage_after = _adjust_usage(session, user_id, count, require_remaining=count)
    if usage_after is None:
        raise InsufficientUsage(f"剩余次数不足：需要 {count} 次")
    _record_usage(
        session, user_id, kind=KIND_USAGE_RESERVE, usage_after=usage_after,
        task_id=task_id, reason=reason,
    )
    return usage_after


def release_usage(
    session: Session, user_id: int, count: int = 1, *, task_id: int | None, reason: str
) -> int:
    """次数轨释放（任务失败/完成归还预留）。释放超过已用量是调用方 bug，
    抛 ValueError。返回变动后剩余次数。"""
    if count <= 0:
        raise ValueError(f"release count must be positive, got {count}")
    usage_after = _adjust_usage(session, user_id, -count, require_used_at_least=count)
    if usage_after is None:
        raise ValueError(f"释放失败：用户 {user_id} 不存在或释放量超过已预留量")
    _record_usage(
        session, user_id, kind=KIND_USAGE_RELEASE, usage_after=usage_after,
        task_id=task_id, reason=reason,
    )
    return usage_after
