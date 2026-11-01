"""测试引导：门禁命令从仓库根目录运行 `python -m pytest backend/tests`，
pytest 只会把 backend/tests 加进 sys.path；这里补上 backend/ 使 `import app` 可用。

测试接缝：HTTP API（TestClient + 临时 SQLite 文件），符合 spec 的测试决策——
只测外部行为（状态码、返回的额度），不测内部调用结构。每个测试拿到独立的
临时库：先跑 Alembic 迁移建表，再把 app.db.SessionLocal 指到该库——请求的
提交/回滚协议始终只有 get_session 一份（生产同款），测试不复制事务边界。
"""

import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import alembic.command
import alembic.config
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import db as app_db  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import create_db_engine  # noqa: E402
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
def client(db_session_factory: sessionmaker, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """把 app.db.SessionLocal 换成测试库的 session 工厂：get_session 依赖保持
    原样（它每次调用都从 app.db 模块取 SessionLocal），提交/回滚协议与生产
    同源，不会在测试里悄悄分叉。

    base_url 用 https：生产会话 cookie 带 Secure 标志（默认），http 客户端
    不回传 Secure cookie，登录后的请求会全部 401。
    """
    monkeypatch.setattr(app_db, "SessionLocal", db_session_factory)
    yield TestClient(app, base_url="https://testserver")


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


@pytest.fixture()
def logged_in_client(client: TestClient, admin_credentials: None) -> Iterator[TestClient]:
    response = client.post(
        "/api/admin/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    yield client
