"""Alembic 迁移可上下执行（issue #5 验收：upgrade → downgrade → upgrade）。"""

import alembic.command
from sqlalchemy import inspect

from conftest import _alembic_config


def _table_names(url: str) -> set[str]:
    from app.db import create_db_engine

    engine = create_db_engine(url)
    try:
        with engine.connect() as conn:
            return set(inspect(conn).get_table_names())
    finally:
        engine.dispose()


def test_migration_up_down_up(db_url: str) -> None:
    cfg = _alembic_config(db_url)

    alembic.command.upgrade(cfg, "head")
    assert "users" in _table_names(db_url)
    assert "alembic_version" in _table_names(db_url)

    alembic.command.downgrade(cfg, "base")
    assert "users" not in _table_names(db_url)

    alembic.command.upgrade(cfg, "head")
    assert "users" in _table_names(db_url)


def test_upgraded_schema_matches_models(db_url: str) -> None:
    """迁移后的 users 列与 app.models 定义一致，防止两边漂移。"""
    from app.db import create_db_engine
    from app.models import User

    alembic.command.upgrade(_alembic_config(db_url), "head")
    engine = create_db_engine(db_url)
    try:
        with engine.connect() as conn:
            columns = {c["name"] for c in inspect(conn).get_columns("users")}
    finally:
        engine.dispose()

    model_columns = {c.name for c in User.__table__.columns}
    assert columns == model_columns
