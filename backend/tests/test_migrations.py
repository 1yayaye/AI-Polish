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
    """迁移后的表列与 app.models 定义一致，防止两边漂移。"""
    from app.db import create_db_engine
    from app.models import FormatJob, LedgerEntry, User

    alembic.command.upgrade(_alembic_config(db_url), "head")
    engine = create_db_engine(db_url)
    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            for model in (User, LedgerEntry, FormatJob):
                columns = {c["name"] for c in inspector.get_columns(model.__tablename__)}
                model_columns = {c.name for c in model.__table__.columns}
                assert columns == model_columns
            indexes = {i["name"] for i in inspector.get_indexes("ledger_entries")}
            assert "ux_ledger_refund_once_per_task" in indexes  # 退款幂等守卫
    finally:
        engine.dispose()


def test_schema_has_model_snapshot_and_no_credential_columns(db_url: str) -> None:
    """schema 级守卫（issue #18 验收 4，ADR-0005）：模型接入只来自 .env，
    任何表不得出现 *_api_key / *_base_url 列；会话表只留只读 model_snapshot。"""
    from app.db import create_db_engine

    alembic.command.upgrade(_alembic_config(db_url), "head")
    engine = create_db_engine(db_url)
    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            for table in inspector.get_table_names():
                columns = {c["name"] for c in inspector.get_columns(table)}
                leaks = {c for c in columns if c.endswith("_api_key") or c.endswith("_base_url")}
                assert not leaks, f"{table} 出现凭据列：{leaks}"
            session_columns = {c["name"] for c in inspector.get_columns("optimization_sessions")}
            assert "model_snapshot" in session_columns  # 客诉排查用的只读快照
    finally:
        engine.dispose()
