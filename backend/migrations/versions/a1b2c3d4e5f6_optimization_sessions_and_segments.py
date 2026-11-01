"""optimization sessions and segments

Revision ID: a1b2c3d4e5f6
Revises: fbc9cd7b51eb
Create Date: 2026-11-01

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "fbc9cd7b51eb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "optimization_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        # queued/running/completed/failed/cancelled（app.optimization 拥有状态机）
        sa.Column("status", sa.String(16), nullable=False),
        # paper_polish / paper_enhance / paper_polish_enhance（GLOSSARY：处理模式）
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        # 计费字段：预扣费依据的计费字数与预扣金额（GLOSSARY：预扣费）
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("charged_fen", sa.Integer(), nullable=False),
        # 只读模型快照（Worker 认领时一次写入）；无 *_api_key/*_base_url 列（ADR-0005）
        sa.Column("model_snapshot", sa.String(255), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_optimization_sessions_user_id"), "optimization_sessions", ["user_id"]
    )
    op.create_index(
        op.f("ix_optimization_sessions_status"), "optimization_sessions", ["status"]
    )
    op.create_table(
        "segments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        # 流水线阶段归属：'polish' / 'enhance'
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("result_text", sa.Text(), nullable=True),
        # pending/completed/failed（ADR-0003：按段断点续跑）
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["session_id"], ["optimization_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_segments_session_id"), "segments", ["session_id"])
    op.create_index(
        "ux_segments_session_stage_seq", "segments", ["session_id", "stage", "seq"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ux_segments_session_stage_seq", table_name="segments")
    op.drop_index(op.f("ix_segments_session_id"), table_name="segments")
    op.drop_table("segments")
    op.drop_index(op.f("ix_optimization_sessions_status"), table_name="optimization_sessions")
    op.drop_index(op.f("ix_optimization_sessions_user_id"), table_name="optimization_sessions")
    op.drop_table("optimization_sessions")
