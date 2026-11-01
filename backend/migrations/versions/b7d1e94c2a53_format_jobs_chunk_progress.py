"""format_jobs 分块进度两列

Revision ID: b7d1e94c2a53
Revises: e7d2a4c6b8f0
Create Date: 2026-11-02

文章预处理（issue #35）的分块进度事实源：预处理任务在 AI 分块标记推进期写
chunk_index/chunk_total（"第几块/共几块"），进度流据此推导分块事件；格式化
任务两列恒空。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d1e94c2a53"
down_revision: str | None = "e7d2a4c6b8f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("format_jobs", sa.Column("chunk_index", sa.Integer(), nullable=True))
    op.add_column("format_jobs", sa.Column("chunk_total", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("format_jobs", "chunk_total")
    op.drop_column("format_jobs", "chunk_index")
