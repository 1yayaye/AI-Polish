"""custom_prompts and saved_specs

Revision ID: fbc9cd7b51eb
Revises: 93148e938800
Create Date: 2026-11-01

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fbc9cd7b51eb"
down_revision: str | None = "93148e938800"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "custom_prompts",
        sa.Column("id", sa.Integer(), nullable=False),
        # 系统预设无归属用户，可空；正文唯一来源是代码种子（app/seed_data）
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        # 流水线阶段：'polish' / 'enhance'
        sa.Column("stage", sa.String(50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_custom_prompts_user_id"), "custom_prompts", ["user_id"])

    op.create_table(
        "saved_specs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        # 排版规格的 JSON 全文，格式由 Word Formatter（M4）拥有
        sa.Column("spec_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_saved_specs_user_id"), "saved_specs", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_saved_specs_user_id"), table_name="saved_specs")
    op.drop_table("saved_specs")
    op.drop_index(op.f("ix_custom_prompts_user_id"), table_name="custom_prompts")
    op.drop_table("custom_prompts")
