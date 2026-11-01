"""segment change logs

Revision ID: be44c1a07d5c
Revises: a1b2c3d4e5f6
Create Date: 2026-11-01

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "be44c1a07d5c"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "segment_change_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        # 寻址沿用段落的 (session_id, stage, seq)（app.models.SegmentChangeLog）
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        # 变更记录（GLOSSARY）：该次调用的实际输入与输出，只写不改
        sa.Column("before_text", sa.Text(), nullable=False),
        sa.Column("after_text", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["session_id"], ["optimization_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_segment_change_logs_session_id", "segment_change_logs", ["session_id"], unique=False
    )
    op.create_index(
        "ux_segment_change_logs_session_stage_seq",
        "segment_change_logs",
        ["session_id", "stage", "seq"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_segment_change_logs_session_stage_seq", table_name="segment_change_logs")
    op.drop_index("ix_segment_change_logs_session_id", table_name="segment_change_logs")
    op.drop_table("segment_change_logs")
