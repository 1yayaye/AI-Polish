"""ledger entries

Revision ID: 93148e938800
Revises: ace4f9ae39d2
Create Date: 2026-11-01

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "93148e938800"
down_revision: str | None = "ace4f9ae39d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 退款状态守卫：每个 task 至多一条 refund 行（billing.refund 幂等语义的
# 数据库防线）。task_id 为 NULL 的历史/管理行不参与该约束。
_REFUND_GUARD = "kind = 'refund' AND task_id IS NOT NULL"


def upgrade() -> None:
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        # 无外键且可空：旧账本迁入不因缺旧会话断裂（rewrite-plan §5）
        sa.Column("task_id", sa.Integer(), nullable=True),
        # kind：issue/charge/refund/adjust/usage_reserve/usage_release（app.billing 拥有）
        sa.Column("kind", sa.String(16), nullable=False),
        # 带符号增量：SUM(amount_fen) == 用户当前余额 恒成立
        sa.Column("amount_fen", sa.Integer(), nullable=False),
        sa.Column("balance_after_fen", sa.Integer(), nullable=False),
        # 仅次数轨变动非空（变动后剩余次数）
        sa.Column("usage_after", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ledger_entries_user_id"), "ledger_entries", ["user_id"])
    op.create_index(op.f("ix_ledger_entries_task_id"), "ledger_entries", ["task_id"])
    op.create_index(
        "ux_ledger_refund_once_per_task",
        "ledger_entries",
        ["task_id"],
        unique=True,
        sqlite_where=sa.text(_REFUND_GUARD),
    )


def downgrade() -> None:
    op.drop_index("ux_ledger_refund_once_per_task", table_name="ledger_entries")
    op.drop_index(op.f("ix_ledger_entries_task_id"), table_name="ledger_entries")
    op.drop_index(op.f("ix_ledger_entries_user_id"), table_name="ledger_entries")
    op.drop_table("ledger_entries")
