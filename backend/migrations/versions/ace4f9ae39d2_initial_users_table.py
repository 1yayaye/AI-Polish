"""initial users table

Revision ID: ace4f9ae39d2
Revises:
Create Date: 2026-11-01 19:18:22.981158

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ace4f9ae39d2"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        # 卡密：用户全部身份凭证，明文唯一存储（用户凭卡密直用，无哈希必要）
        sa.Column("card_key", sa.String(), nullable=False),
        # 余额（分），双轨之一：Workspace 按字数计费（M2 接入）
        sa.Column("balance_fen", sa.Integer(), nullable=False, server_default="0"),
        # 次数额度（上限/已用），双轨之二：Word Formatter 按次计费（M4 接入）
        sa.Column("usage_limit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usage_used", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_card_key"), "users", ["card_key"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_card_key"), table_name="users")
    op.drop_table("users")
