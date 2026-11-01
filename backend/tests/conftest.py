"""测试引导：门禁命令从仓库根目录运行 `python -m pytest backend/tests`，
pytest 只会把 backend/tests 加进 sys.path；这里补上 backend/ 使 `import app` 可用。

测试接缝：HTTP API（TestClient + 临时 SQLite 文件），符合 spec 的测试决策——
只测外部行为（状态码、返回的额度），不测内部调用结构。每个测试拿到独立的
临时库：先跑 Alembic 迁移建表，再把应用的 get_session 依赖覆盖到该库。
"""

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import alembic.command
import alembic.config
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings  # noqa: E402
from app.db import create_db_engine, get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402

ALEMBIC_INI = BACKEND_DIR / "alembic.ini"

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-pass-123"


def _alembic_config(database_url: str) -> alembic.config.Config:
    cfg = alembic.config.Config(str(ALEMBIC_INI))
    # 迁移 URL 由测试显式给定（临时库）；生产路径下 env.py 回落到 .env 的配置
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture()
def db_engine(db_url: str) -> Iterator[Engine]:
    alembic.command.upgrade(_alembic_config(db_url), "head")
    engine = create_db_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session_factory(db_engine: Engine) -> sessionmaker:
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def client(db_session_factory: sessionmaker) -> Iterator[TestClient]:
    session_factory = db_session_factory

    def _override_session() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override_session
    yield TestClient(app)
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture()
def make_user(db_session_factory: sessionmaker) -> Callable[..., User]:
    """在临时库造一个卡密用户，返回 ORM 对象（值已落库）。"""

    def _make(
        card_key: str = "card-key-1",
        balance_fen: int = 0,
        usage_limit: int = 0,
        usage_used: int = 0,
    ) -> User:
        session = db_session_factory()
        try:
            user = User(
                card_key=card_key,
                balance_fen=balance_fen,
                usage_limit=usage_limit,
                usage_used=usage_used,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user
        finally:
            session.close()

    return _make


@pytest.fixture()
def admin_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """给 settings 注入测试用管理端凭据（生产从 .env 读取，ADR-0005）。"""
    monkeypatch.setattr(settings, "admin_username", ADMIN_USERNAME)
    monkeypatch.setattr(settings, "admin_password", ADMIN_PASSWORD)
    monkeypatch.setattr(settings, "jwt_secret", "test-jwt-secret-0123456789abcdef0123456789abcdef")
