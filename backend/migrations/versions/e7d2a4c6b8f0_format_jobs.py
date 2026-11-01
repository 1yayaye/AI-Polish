"""format_jobs

Revision ID: e7d2a4c6b8f0
Revises: be44c1a07d5c
Create Date: 2026-11-02

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7d2a4c6b8f0"
down_revision: str | None = "be44c1a07d5c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "format_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        # format（排版编译）/ preprocess（AI 分块标记）
        sa.Column("job_type", sa.String(16), nullable=False),
        # queued / running / completed / failed（无 cancelled：旧版取消从未暴露）
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("input_file_name", sa.String(255), nullable=True),
        # 编译/预处理配置 JSON，结构由消费方拥有，本表只存取不解释
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # 进度三列（phase/progress/message）：Worker 短事务推进的展示态
        sa.Column("phase", sa.String(32), nullable=True),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("message", sa.String(255), nullable=True),
        # 产物落盘：DB 只存文件名，完整路径由 app.word_jobs 解析（spec #31）
        sa.Column("output_path", sa.String(255), nullable=True),
        sa.Column("output_filename", sa.String(255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_format_jobs_user_id"), "format_jobs", ["user_id"])
    op.create_index(op.f("ix_format_jobs_status"), "format_jobs", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_format_jobs_status"), table_name="format_jobs")
    op.drop_index(op.f("ix_format_jobs_user_id"), table_name="format_jobs")
    op.drop_table("format_jobs")
