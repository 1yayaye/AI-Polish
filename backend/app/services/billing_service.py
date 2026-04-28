"""Workspace balance billing helpers."""

from datetime import datetime
from math import ceil
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.models import BillingTransaction, OptimizationSession, User


def calculate_workspace_charge_cents(char_count: int, price_per_10k_cents: int) -> int:
    if char_count <= 0:
        return 0
    if price_per_10k_cents <= 0:
        raise HTTPException(status_code=400, detail="Workspace price is not configured")
    return max(1, ceil(char_count * price_per_10k_cents / 10000))


def _current_balance(user: User) -> int:
    return user.workspace_balance_cents or 0


def _total_spent(user: User) -> int:
    return user.workspace_total_spent_cents or 0


def _create_transaction(
    db: Session,
    *,
    user: User,
    transaction_type: str,
    amount_cents: int,
    balance_after_cents: int,
    session: Optional[OptimizationSession] = None,
    reason: Optional[str] = None,
) -> BillingTransaction:
    transaction = BillingTransaction(
        user_id=user.id,
        optimization_session_id=session.id if session else None,
        transaction_type=transaction_type,
        amount_cents=amount_cents,
        balance_after_cents=balance_after_cents,
        reason=reason,
    )
    db.add(transaction)
    return transaction


def precharge_workspace_session(
    db: Session,
    *,
    user: User,
    session: OptimizationSession,
    char_count: int,
    price_per_10k_cents: int,
) -> int:
    if session.billing_status == "precharged":
        raise HTTPException(status_code=409, detail="Task was already precharged")

    amount_cents = calculate_workspace_charge_cents(char_count, price_per_10k_cents)
    if amount_cents <= 0:
        raise HTTPException(status_code=400, detail="Billing character count must be positive")

    result = db.execute(
        update(User)
        .where(
            User.id == user.id,
            User.workspace_balance_cents >= amount_cents,
        )
        .values(
            workspace_balance_cents=User.workspace_balance_cents - amount_cents,
            workspace_total_spent_cents=User.workspace_total_spent_cents + amount_cents,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=402, detail="Insufficient workspace balance")

    db.flush()
    db.refresh(user)

    session.billing_char_count = char_count
    session.billing_amount_cents = amount_cents
    session.billing_price_per_10k_cents = price_per_10k_cents
    session.billing_status = "precharged"
    session.billing_refunded_at = None

    _create_transaction(
        db,
        user=user,
        session=session,
        transaction_type="workspace_precharge",
        amount_cents=-amount_cents,
        balance_after_cents=user.workspace_balance_cents,
        reason="workspace task precharge",
    )
    return amount_cents


def mark_workspace_charge_succeeded(db: Session, *, session: OptimizationSession) -> bool:
    if session.billing_status != "precharged":
        return False
    session.billing_status = "charged"
    db.add(session)
    return True


def refund_workspace_charge(
    db: Session,
    *,
    session: OptimizationSession,
    reason: Optional[str] = None,
) -> bool:
    if session.billing_status == "refunded":
        return False

    amount_cents = session.billing_amount_cents or 0
    if session.billing_status != "precharged" or amount_cents <= 0:
        return False

    user = session.user or db.query(User).filter(User.id == session.user_id).first()
    if not user:
        return False

    user.workspace_balance_cents = _current_balance(user) + amount_cents
    user.workspace_total_spent_cents = max(0, _total_spent(user) - amount_cents)

    session.billing_status = "refunded"
    session.billing_refunded_at = datetime.utcnow()

    _create_transaction(
        db,
        user=user,
        session=session,
        transaction_type="workspace_refund",
        amount_cents=amount_cents,
        balance_after_cents=user.workspace_balance_cents,
        reason=reason or "workspace task refunded",
    )
    return True


def adjust_user_workspace_balance(
    db: Session,
    *,
    user: User,
    delta_cents: int,
    reason: Optional[str] = None,
) -> BillingTransaction:
    new_balance = _current_balance(user) + delta_cents
    if new_balance < 0:
        raise HTTPException(status_code=400, detail="Workspace balance cannot be negative")

    user.workspace_balance_cents = new_balance
    return _create_transaction(
        db,
        user=user,
        transaction_type="admin_adjustment",
        amount_cents=delta_cents,
        balance_after_cents=new_balance,
        reason=reason or "admin balance adjustment",
    )
