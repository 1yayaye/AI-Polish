"""系统配置只读面（issue #29，HTTP API 接缝）。

验收红线：敏感配置值（LLM key、管理密码、JWT 密钥）在任何响应中零出现
——包括 OpenAPI/Swagger 文档响应；无管理会话 cookie 一律 401；接口纯只读
（.env 唯一配置来源，ADR-0005，后端无改配置路径）。
"""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN_PASSWORD

# 与 conftest.ADMIN_PASSWORD / jwt_secret 值不同的全新假密钥：泄露断言里
# 每个敏感值都必须"只可能来自配置注入"，不能混用同一个字面量。
FAKE_LLM_BASE_URL = "https://llm.example.com/v1"
FAKE_LLM_API_KEY = "sk-fake-operator-key-9876543210abcdef"
FAKE_LLM_MODEL = "fake-model-for-tests"
FAKE_JWT_SECRET = "another-jwt-secret-fedcba9876543210fedcba9876543210"


def test_config_requires_login(client: TestClient) -> None:
    """无管理会话 cookie 一律 401（管理端统一纪律，不区分原因）。"""
    assert client.get("/api/admin/config").status_code == 401


def test_config_reports_runtime_params_and_model_access(
    logged_in_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行参数原值展示；模型接入三项齐备时报接入点与模型名。"""
    from app.config import settings

    monkeypatch.setattr(settings, "llm_base_url", FAKE_LLM_BASE_URL)
    monkeypatch.setattr(settings, "llm_api_key", FAKE_LLM_API_KEY)
    monkeypatch.setattr(settings, "llm_model", FAKE_LLM_MODEL)

    response = logged_in_client.get("/api/admin/config")

    assert response.status_code == 200
    body = response.json()
    assert body["recovery_max_attempts"] == settings.recovery_max_attempts
    assert body["recovery_stuck_after_seconds"] == settings.recovery_stuck_after_seconds
    assert (
        body["recovery_patrol_interval_seconds"] == settings.recovery_patrol_interval_seconds
    )
    assert body["max_session_text_chars"] == settings.max_session_text_chars
    assert body["llm"] == {
        "configured": True,
        "base_url": FAKE_LLM_BASE_URL,
        "model": FAKE_LLM_MODEL,
        "api_key_configured": True,
    }


def test_config_reports_unconfigured_llm(
    logged_in_client: TestClient,
) -> None:
    """模型接入缺项时报未配置：接入点/模型名为 null，不冒充可用。"""
    response = logged_in_client.get("/api/admin/config")

    assert response.status_code == 200
    assert response.json()["llm"] == {
        "configured": False,
        "base_url": None,
        "model": None,
        "api_key_configured": False,
    }


def test_sensitive_values_never_appear_in_any_response(
    logged_in_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """零泄露红线：配置接口与 OpenAPI 文档响应的完整响应体里，敏感值
    （LLM key、管理密码、JWT 密钥）一个都不出现——只有布尔"已配置/未配置"。
    对响应原文断言而非解析后的 JSON：任何位置（含嵌套/报错信息）出现值都算泄露。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "llm_base_url", FAKE_LLM_BASE_URL)
    monkeypatch.setattr(settings, "llm_api_key", FAKE_LLM_API_KEY)
    monkeypatch.setattr(settings, "llm_model", FAKE_LLM_MODEL)
    monkeypatch.setattr(settings, "jwt_secret", FAKE_JWT_SECRET)
    # jwt_secret 换新值会使已登录 cookie 失效：重新登录拿新会话再验证
    login = logged_in_client.post(
        "/api/admin/login",
        json={"username": settings.admin_username, "password": ADMIN_PASSWORD},
    )
    assert login.status_code == 200

    secrets = [FAKE_LLM_API_KEY, ADMIN_PASSWORD, FAKE_JWT_SECRET]

    config_response = logged_in_client.get("/api/admin/config")
    assert config_response.status_code == 200
    for secret in secrets:
        assert secret not in config_response.text
    body = config_response.json()
    assert body["llm"]["api_key_configured"] is True
    assert body["admin_password"]["configured"] is True
    assert body["jwt_secret"]["configured"] is True

    openapi_response = logged_in_client.get("/api/openapi.json")
    assert openapi_response.status_code == 200
    for secret in secrets:
        assert secret not in openapi_response.text


def test_config_endpoint_is_get_only(logged_in_client: TestClient) -> None:
    """只读面没有写路径：POST/PUT/PATCH/DELETE 都不是可用入口（.env 唯一
    配置来源，不存在经 HTTP 改配置的路径）。"""
    assert logged_in_client.post("/api/admin/config").status_code == 405
    assert logged_in_client.put("/api/admin/config").status_code == 405
    assert logged_in_client.patch("/api/admin/config").status_code == 405
    assert logged_in_client.delete("/api/admin/config").status_code == 405
