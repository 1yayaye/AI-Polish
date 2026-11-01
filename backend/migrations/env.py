"""Alembic 迁移环境。

数据库 URL 的唯一来源是 app.config（即 .env，ADR-0005），本文件不写任何 URL。
相对路径的 sqlite URL 相对 backend/ 工作目录解析（与 uvicorn 生产运行一致）；
测试通过 config.set_main_option("sqlalchemy.url", ...) 指向临时库来覆盖。
引擎统一走 app.db.create_db_engine，保证迁移连接与应用连接持有同一套 PRAGMA。
"""

import sys
from logging.config import fileConfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context

from app.config import settings
from app.db import create_db_engine
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or settings.database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        # SQLite 的 ALTER 能力有限，统一走 batch 模式（建副本表再换名）
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_database_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
