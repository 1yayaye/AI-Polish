"""卡密认证与额度查询（HTTP API 接缝）。

覆盖 issue #5 验收：缺失/无效卡密 401；有效卡密访问本人双轨额度，
返回余额（分）与剩余次数。卡密只出现在 X-Card-Key header，绝不进 URL。
"""

from fastapi.testclient import TestClient

from app.models import User


def test_missing_card_key_rejected(client: TestClient) -> None:
    response = client.get("/api/me/quota")

    assert response.status_code == 401
    assert "detail" in response.json()


def test_invalid_card_key_rejected(client: TestClient) -> None:
    response = client.get("/api/me/quota", headers={"X-Card-Key": "no-such-key"})

    assert response.status_code == 401


def test_empty_card_key_rejected(client: TestClient) -> None:
    response = client.get("/api/me/quota", headers={"X-Card-Key": ""})

    assert response.status_code == 401


def test_valid_card_key_returns_dual_track_quota(
    client: TestClient, make_user
) -> None:
    make_user(card_key="card-abc", balance_fen=12500, usage_limit=30, usage_used=7)

    response = client.get("/api/me/quota", headers={"X-Card-Key": "card-abc"})

    assert response.status_code == 200
    body = response.json()
    assert body["balance_fen"] == 12500
    assert body["usage_remaining"] == 23


def test_quota_isolated_per_card_key(client: TestClient, make_user) -> None:
    make_user(card_key="card-mine", balance_fen=100, usage_limit=10, usage_used=1)
    make_user(card_key="card-other", balance_fen=999, usage_limit=99, usage_used=9)

    response = client.get("/api/me/quota", headers={"X-Card-Key": "card-mine"})

    assert response.status_code == 200
    body = response.json()
    assert body["balance_fen"] == 100
    assert body["usage_remaining"] == 9


def test_card_key_never_in_url(client: TestClient, make_user) -> None:
    """卡密路由本身不含任何路径参数：卡密只经 header 传递，URL 无从泄露。"""
    make_user(card_key="card-abc")

    response = client.get("/api/me/quota", headers={"X-Card-Key": "card-abc"})

    assert response.status_code == 200
    assert "card-abc" not in response.url.path
    assert "card-abc" not in (response.url.query or "")
