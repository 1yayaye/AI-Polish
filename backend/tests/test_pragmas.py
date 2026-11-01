"""验证全套 SQLite PRAGMA 在连接事件上固化（ADR-0001 / rewrite-plan §5）。

PRAGMA 中 journal_mode 会持久化进库文件，其余（synchronous、busy_timeout、
foreign_keys、temp_store、mmap_size）是连接级参数——后者每个新连接都必须生效，
这里逐一断言。
"""

from sqlalchemy import text

from app.db import create_db_engine


def test_all_pragmas_fixed_on_new_connections(db_url: str) -> None:
    engine = create_db_engine(db_url)
    try:
        with engine.connect() as conn:
            pragmas = {
                name: conn.execute(text(f"PRAGMA {name}")).scalar()
                for name in (
                    "journal_mode",
                    "synchronous",
                    "busy_timeout",
                    "foreign_keys",
                    "temp_store",
                    "mmap_size",
                )
            }
    finally:
        engine.dispose()

    assert pragmas["journal_mode"] == "wal"
    assert pragmas["synchronous"] == 1  # NORMAL
    assert pragmas["busy_timeout"] == 5000
    assert pragmas["foreign_keys"] == 1
    assert pragmas["temp_store"] == 2  # MEMORY
    assert pragmas["mmap_size"] == 30_720_000  # ≈30MB


def test_pragmas_apply_to_every_connection(db_url: str) -> None:
    """连接级 PRAGMA 不能只对第一个连接生效：NullPool 下每个新连接都重放。"""
    engine = create_db_engine(db_url)
    try:
        for _ in range(3):
            with engine.connect() as conn:
                assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
                assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
    finally:
        engine.dispose()
