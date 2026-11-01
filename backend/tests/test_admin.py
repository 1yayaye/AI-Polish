"""管理端认证与发卡（HTTP API 接缝）。

覆盖 issue #6 验收：登录成败与 HttpOnly cookie、未认证一律 401、批量发卡
唯一性与初始双轨额度、用户列表脱敏。密码只经 POST body，绝不进 URL/query
（进 URL 会落入 Nginx 访问日志，rewrite-plan §5）。
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.models import LedgerEntry, User
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


# ---- 会话检查与登出 ----


def test_session_reports_current_admin(logged_in_client: TestClient) -> None:
    response = logged_in_client.get("/api/admin/session")

    assert response.status_code == 200
    assert response.json() == {"username": ADMIN_USERNAME}


def test_session_requires_login(client: TestClient) -> None:
    """外壳启动时用 401/200 区分登录态：无 cookie 一律 401（不区分原因）。"""
    assert client.get("/api/admin/session").status_code == 401


def test_expired_session_cookie_rejected(
    client: TestClient, admin_credentials: None
) -> None:
    """12 小时 TTL 过期（exp 已过）的会话等同未登录，管理接口一律 401。"""
    import time

    import jwt

    from app.config import settings

    now = int(time.time())
    expired = jwt.encode(
        {
            "sub": ADMIN_USERNAME,
            "iat": now - 12 * 3600 - 60,
            "exp": now - 60,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    client.cookies.set(COOKIE_NAME, expired)

    assert client.get("/api/admin/session").status_code == 401
    assert client.get("/api/admin/users").status_code == 401


def test_logout_clears_cookie_and_revokes_session(logged_in_client: TestClient) -> None:
    assert logged_in_client.get("/api/admin/users").status_code == 200

    response = logged_in_client.post("/api/admin/logout")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    set_cookie = response.headers["set-cookie"]
    assert f"{COOKIE_NAME}=" in set_cookie
    assert "max-age=0" in set_cookie.lower()  # 删除指令：浏览器随即丢弃 cookie
    # 登出后再访问管理接口回到未认证（401），前端据此回到登录表单
    assert logged_in_client.get("/api/admin/users").status_code == 401


def test_logout_requires_session(client: TestClient) -> None:
    assert client.post("/api/admin/logout").status_code == 401


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
    page = response.json()
    assert page["total"] == 1
    assert len(page["items"]) == 1
    row = page["items"][0]
    assert row["card_key"] == "card****1234"
    assert "card-secret-1234" not in response.text  # 全量卡密绝不出现
    assert row["balance_fen"] == 500
    assert row["usage_limit"] == 10
    assert row["usage_remaining"] == 7


# ---- 用户列表分页与搜索（issue #26）----


def _seed_users(make_user, count: int) -> list[int]:
    return [make_user(card_key=f"fragment-user-{i:02d}-zzz").id for i in range(count)]


def test_user_list_paginates(logged_in_client: TestClient, make_user) -> None:
    ids = _seed_users(make_user, 5)

    response = logged_in_client.get("/api/admin/users", params={"page": 2, "page_size": 2})

    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 5
    assert page["page"] == 2
    assert page["page_size"] == 2
    assert [row["id"] for row in page["items"]] == ids[2:4]  # 按 id 稳定排序

    first = logged_in_client.get("/api/admin/users", params={"page": 1, "page_size": 2}).json()
    assert [row["id"] for row in first["items"]] == ids[:2]

    last = logged_in_client.get("/api/admin/users", params={"page": 3, "page_size": 2}).json()
    assert [row["id"] for row in last["items"]] == ids[4:]


def test_user_list_rejects_out_of_range_paging(logged_in_client: TestClient) -> None:
    """page/page_size 越界是调用方 bug：422 拒绝，不静默截断。"""
    assert logged_in_client.get("/api/admin/users", params={"page": 0}).status_code == 422
    assert logged_in_client.get("/api/admin/users", params={"page_size": 0}).status_code == 422
    assert logged_in_client.get("/api/admin/users", params={"page_size": 101}).status_code == 422


def test_user_search_by_card_key_fragment(logged_in_client: TestClient, make_user) -> None:
    _seed_users(make_user, 3)
    target_id = make_user(card_key="needle-in-haystack-42").id

    response = logged_in_client.get("/api/admin/users", params={"q": "needle-in"})

    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 1
    assert [row["id"] for row in page["items"]] == [target_id]
    # 搜索命中也不泄露全量卡密
    assert "needle-in-haystack-42" not in response.text


def test_user_search_by_user_id(logged_in_client: TestClient, make_user) -> None:
    _seed_users(make_user, 3)
    target_id = make_user(card_key="abc-999-xyz").id

    response = logged_in_client.get("/api/admin/users", params={"q": str(target_id)})

    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 1
    assert [row["id"] for row in page["items"]] == [target_id]


def test_user_search_no_match_returns_empty_page(logged_in_client: TestClient, make_user) -> None:
    _seed_users(make_user, 2)

    page = logged_in_client.get("/api/admin/users", params={"q": "no-such-key"}).json()

    assert page["total"] == 0
    assert page["items"] == []


def test_user_search_whitespace_only_q_means_all(
    logged_in_client: TestClient, make_user
) -> None:
    """空搜索框（含纯空格）不是搜索条件：返回全部用户。"""
    _seed_users(make_user, 2)

    page = logged_in_client.get("/api/admin/users", params={"q": "   "}).json()

    assert page["total"] == 2


def test_user_queries_never_write_ledger(
    logged_in_client: TestClient, make_user, db_session_factory: sessionmaker
) -> None:
    """查询接口对资金字段零直接写：列表/搜索前后账本行数不变，
    SUM(账本)==余额 的不变量没有第二条写路径可破坏。"""
    make_user(card_key="reader-target-1")

    def count_ledger() -> int:
        session = db_session_factory()
        try:
            return session.query(LedgerEntry).count()
        finally:
            session.close()

    before = count_ledger()

    assert logged_in_client.get("/api/admin/users").status_code == 200
    assert logged_in_client.get("/api/admin/users", params={"q": "reader"}).status_code == 200
    assert logged_in_client.get("/api/admin/users", params={"q": "1"}).status_code == 200

    assert count_ledger() == before
