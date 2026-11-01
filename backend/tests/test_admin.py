"""管理端认证与发卡（HTTP API 接缝）。

覆盖 issue #6 验收：登录成败与 HttpOnly cookie、未认证一律 401、批量发卡
唯一性与初始双轨额度、用户列表脱敏。密码只经 POST body，绝不进 URL/query
（进 URL 会落入 Nginx 访问日志，rewrite-plan §5）。
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.models import User
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME

COOKIE_NAME = "admin_session"


def login(client: TestClient, username: str, password: str) -> Any:
    return client.post("/api/admin/login", json={"username": username, "password": password})


# ---- 登录成败 ----


def test_login_success_sets_httponly_cookie(client: TestClient, admin_credentials: None) -> None:
    response = login(client, ADMIN_USERNAME, ADMIN_PASSWORD)

    assert response.status_code == 200
    assert response.json() == {"username": ADMIN_USERNAME}
    set_cookie = response.headers["set-cookie"]
    assert f"{COOKIE_NAME}=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "secure" in set_cookie.lower()  # 默认走 HTTPS，JWT 不允许明文传输
    assert client.cookies.get(COOKIE_NAME)


def test_login_cookie_secure_flag_follows_settings(
    client: TestClient, admin_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Secure 标志由 .env 配置（默认 true）；本地纯 HTTP 调试可显式关闭。"""
    from app.config import settings

    monkeypatch.setattr(settings, "admin_cookie_secure", False)
    set_cookie = login(client, ADMIN_USERNAME, ADMIN_PASSWORD).headers["set-cookie"]
    assert "secure" not in set_cookie.lower()


def test_login_wrong_password_rejected(client: TestClient, admin_credentials: None) -> None:
    response = login(client, ADMIN_USERNAME, "wrong-password")

    assert response.status_code == 401
    assert COOKIE_NAME not in client.cookies


def test_login_unknown_username_rejected(client: TestClient, admin_credentials: None) -> None:
    response = login(client, "nobody", ADMIN_PASSWORD)

    assert response.status_code == 401


def test_login_rejected_when_admin_not_configured(client: TestClient) -> None:
    """管理端凭据未配置（.env 缺项）时登录一律拒绝，不存在默认账号。"""
    response = login(client, ADMIN_USERNAME, ADMIN_PASSWORD)

    assert response.status_code == 401


def test_password_never_travels_in_query(client: TestClient, admin_credentials: None) -> None:
    """登录只接受 POST body：GET 带 query 密码不是一条可用的登录路径，
    且 OpenAPI 契约中登录操作不声明任何 query 参数。"""
    assert client.get("/api/admin/login", params={"password": ADMIN_PASSWORD}).status_code == 405

    spec = client.get("/api/openapi.json").json()
    login_op = spec["paths"]["/api/admin/login"]["post"]
    assert login_op.get("parameters", []) == []
    assert "requestBody" in login_op


# ---- 未认证拦截 ----


def test_unauthenticated_admin_requests_rejected(client: TestClient) -> None:
    assert client.get("/api/admin/users").status_code == 401
    assert client.post(
        "/api/admin/card-keys", json={"count": 1, "balance_fen": 0, "usage_limit": 0}
    ).status_code == 401


def test_tampered_session_cookie_rejected(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, "not-a-valid-jwt")

    response = client.get("/api/admin/users")

    assert response.status_code == 401


# ---- 批量发卡 ----


def test_batch_creation_generates_unique_keys_with_initial_quota(
    logged_in_client: TestClient, db_session_factory: sessionmaker
) -> None:
    response = logged_in_client.post(
        "/api/admin/card-keys",
        json={"count": 5, "balance_fen": 12500, "usage_limit": 30},
    )

    assert response.status_code == 201
    card_keys = response.json()["card_keys"]
    assert len(card_keys) == 5
    assert len({item["card_key"] for item in card_keys}) == 5
    for item in card_keys:
        assert item["balance_fen"] == 12500
        assert item["usage_limit"] == 30

    session = db_session_factory()
    try:
        rows = session.query(User).filter(User.card_key.in_([i["card_key"] for i in card_keys])).all()
        assert len(rows) == 5
        for row in rows:
            assert row.balance_fen == 12500
            assert row.usage_limit == 30
            assert row.usage_used == 0
    finally:
        session.close()


def test_batch_creation_rejects_invalid_input(logged_in_client: TestClient) -> None:
    assert (
        logged_in_client.post(
            "/api/admin/card-keys",
            json={"count": 0, "balance_fen": 100, "usage_limit": 10},
        ).status_code
        == 422
    )
    assert (
        logged_in_client.post(
            "/api/admin/card-keys",
            json={"count": 1, "balance_fen": -1, "usage_limit": 10},
        ).status_code
        == 422
    )


# ---- 用户列表 ----


def test_user_list_masks_card_key_and_shows_dual_track_quota(
    logged_in_client: TestClient, make_user
) -> None:
    make_user(card_key="card-secret-1234", balance_fen=500, usage_limit=10, usage_used=3)

    response = logged_in_client.get("/api/admin/users")

    assert response.status_code == 200
    users = response.json()
    assert len(users) == 1
    row = users[0]
    assert row["card_key"] == "card****1234"
    assert "card-secret-1234" not in response.text  # 全量卡密绝不出现
    assert row["balance_fen"] == 500
    assert row["usage_limit"] == 10
    assert row["usage_remaining"] == 7
