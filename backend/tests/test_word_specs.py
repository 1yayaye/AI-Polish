"""排版规格纵切（issue #36）：AI 同步生成（预留/退回）与规格校验、已存
规格 CRUD 的全链行为。

主接缝是 HTTP API 层（TestClient + X-Card-Key header），照 test_word_preprocess
先例只测外部行为：LLM 依赖经 generate 端点的 FastAPI 依赖
（word_specs.get_spec_ai）注入脚本化假客户端（test_word_preprocessor 的
complete 接缝，不 mock 内部函数）；模型接入配置走 settings monkeypatch
（生产 = .env，ADR-0005）。账本断言照 test_billing / test_word_jobs 先例：
预留/退回全部落账本（task_id 为空——无任务行），usage_after 连续与
SUM(账本)==余额 不变量在集成路径上复核。
"""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import billing, word_specs
from app.config import settings
from app.main import app
from app.models import LedgerEntry, SavedSpec, User
from app.word_formatter.services import build_generic_spec, export_spec_to_json

from test_billing import assert_ledger_reconciles
from test_word_jobs import _usage_remaining, _usage_rows, usage_user  # noqa: F401
from test_word_preprocess import configured_llm  # noqa: F401
from test_word_preprocessor import ScriptedMarkingLLM

# 生成的合法规格：直接取渲染库内置通用规格全文（真实管线产物），AI 文本再
# 带围栏返回，顺带覆盖生成路径的 markdown 代码块清洗
VALID_SPEC_JSON = export_spec_to_json(build_generic_spec())
VALID_SPEC_NAME = "通用论文（首行缩进）"
REQUIREMENTS = "本科毕业论文格式：标题三号黑体居中，正文小四号宋体1.5倍行距，页边距上下2.54cm。"


@pytest.fixture()
def spec_llm() -> ScriptedMarkingLLM:
    """脚本化 complete 假客户端注入 generate 端点的 LLM 依赖（FastAPI 公开
    接缝），用完摘除避免串到别的测试。"""
    llm = ScriptedMarkingLLM([f"```json\n{VALID_SPEC_JSON}\n```"])
    app.dependency_overrides[word_specs.get_spec_ai] = lambda: llm
    yield llm
    app.dependency_overrides.pop(word_specs.get_spec_ai, None)


# ---- AI 同步生成：成功预留不退、失败退回、未配置 503 零预留 ----


def test_generate_success_reserves_once_and_returns_spec(
    client: TestClient,
    usage_user: Callable[..., User],
    configured_llm: None,
    spec_llm: ScriptedMarkingLLM,
    db_session_factory: sessionmaker,
) -> None:
    user = usage_user(usage_limit=2)
    response = client.post(
        "/api/word-formatter/specs/generate",
        json={"requirements": REQUIREMENTS},
        headers={"X-Card-Key": user.card_key},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["spec_name"] == VALID_SPEC_NAME
    # 返回的规格 JSON 可被保存路径直接复用（校验合法）
    assert body["spec_json"] == VALID_SPEC_JSON

    # 成功不释放：预留 1 次（task_id 为空——同步接口无任务行）
    rows = _usage_rows(db_session_factory, user.id)
    assert [(r.kind, r.task_id, r.usage_after) for r in rows] == [
        (billing.KIND_USAGE_RESERVE, None, 1)
    ]
    assert _usage_remaining(db_session_factory, user.id) == 1
    assert_ledger_reconciles(db_session_factory, user.id)
    # 生成请求带 system 约束与需求全文（complete 接缝的调用形态）
    assert len(spec_llm.calls) == 1
    assert spec_llm.calls[0][0]["role"] == "system"


def test_generate_content_failure_releases_reserve(
    client: TestClient,
    usage_user: Callable[..., User],
    configured_llm: None,
    db_session_factory: sessionmaker,
) -> None:
    """假 LLM 内容性失败（AI 返回的不是合法规格 JSON）：预留先落账本，
    失败后同额退回——失败不吞次数，账本 usage_after 连续。"""
    llm = ScriptedMarkingLLM(["这不是 JSON"])
    app.dependency_overrides[word_specs.get_spec_ai] = lambda: llm
    try:
        user = usage_user(usage_limit=2)
        response = client.post(
            "/api/word-formatter/specs/generate",
            json={"requirements": REQUIREMENTS},
            headers={"X-Card-Key": user.card_key},
        )
    finally:
        app.dependency_overrides.pop(word_specs.get_spec_ai, None)
    assert response.status_code == 502, response.text
    assert "生成规格失败" in response.json()["detail"]

    rows = _usage_rows(db_session_factory, user.id)
    assert [(r.kind, r.task_id, r.usage_after) for r in rows] == [
        (billing.KIND_USAGE_RESERVE, None, 1),
        (billing.KIND_USAGE_RELEASE, None, 2),
    ]
    assert _usage_remaining(db_session_factory, user.id) == 2
    assert_ledger_reconciles(db_session_factory, user.id)


def test_generate_is_503_without_model_config_with_zero_reserve(
    client: TestClient,
    usage_user: Callable[..., User],
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: sessionmaker,
) -> None:
    """模型接入未配置（ADR-0005）：503 明确失败，零预留零 LLM 调用。"""
    monkeypatch.setattr(settings, "llm_base_url", "")
    llm = ScriptedMarkingLLM([VALID_SPEC_JSON])
    app.dependency_overrides[word_specs.get_spec_ai] = lambda: llm
    try:
        user = usage_user(usage_limit=3)
        response = client.post(
            "/api/word-formatter/specs/generate",
            json={"requirements": REQUIREMENTS},
            headers={"X-Card-Key": user.card_key},
        )
    finally:
        app.dependency_overrides.pop(word_specs.get_spec_ai, None)
    assert response.status_code == 503, response.text
    assert llm.calls == []
    session = db_session_factory()
    try:
        kinds = session.execute(select(LedgerEntry.kind)).scalars().all()
        assert kinds == [billing.KIND_ISSUE]  # 仅存发卡行：零预留
    finally:
        session.close()


def test_generate_rejects_insufficient_usage_and_missing_card_key(
    client: TestClient,
    usage_user: Callable[..., User],
    configured_llm: None,
    spec_llm: ScriptedMarkingLLM,
    db_session_factory: sessionmaker,
) -> None:
    # 次数不足 403，预留不落账本
    broke = usage_user(card_key="card-broke", usage_limit=0)
    assert (
        client.post(
            "/api/word-formatter/specs/generate",
            json={"requirements": REQUIREMENTS},
            headers={"X-Card-Key": broke.card_key},
        ).status_code
        == 403
    )
    # 缺卡密 401（header 认证，卡密绝不进 URL），LLM 不被调用
    assert (
        client.post(
            "/api/word-formatter/specs/generate",
            json={"requirements": REQUIREMENTS},
        ).status_code
        == 401
    )
    assert spec_llm.calls == []
    session = db_session_factory()
    try:
        kinds = session.execute(select(LedgerEntry.kind)).scalars().all()
        assert kinds == [billing.KIND_ISSUE]
    finally:
        session.close()


# ---- 合法性校验：坏 JSON / 非法规格 400 ----


def test_validate_accepts_good_spec_and_rejects_bad_json(
    client: TestClient,
    usage_user: Callable[..., User],
) -> None:
    user = usage_user()
    headers = {"X-Card-Key": user.card_key}

    good = client.post(
        "/api/word-formatter/specs/validate",
        json={"spec_json": VALID_SPEC_JSON},
        headers=headers,
    )
    assert good.status_code == 200, good.text
    assert good.json() == {"valid": True, "spec_name": VALID_SPEC_NAME}

    # 坏 JSON 400（连 JSON 都不是）
    bad_json = client.post(
        "/api/word-formatter/specs/validate",
        json={"spec_json": "{oops"},
        headers=headers,
    )
    assert bad_json.status_code == 400
    # 合法 JSON 但非法规格 400（缺 page/styles）
    bad_spec = client.post(
        "/api/word-formatter/specs/validate",
        json={"spec_json": '{"meta": {"name": "空规格"}}'},
        headers=headers,
    )
    assert bad_spec.status_code == 400


# ---- 已存规格：校验后保存、同名覆盖、归属隔离、删除 ----


def test_save_updates_same_name_instead_of_creating_new(
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    user = usage_user()
    headers = {"X-Card-Key": user.card_key}

    first = client.post(
        "/api/word-formatter/specs/save",
        json={"name": "我的规格", "spec_json": VALID_SPEC_JSON, "description": "初版"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    created = first.json()

    # 同名再存：更新而非新建——同一 id、新 spec_json、description 覆盖
    other_spec = export_spec_to_json(build_generic_spec(first_line_indent=False))
    second = client.post(
        "/api/word-formatter/specs/save",
        json={"name": "我的规格", "spec_json": other_spec, "description": "改版"},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    updated = second.json()
    assert updated["id"] == created["id"]
    assert updated["spec_json"] != created["spec_json"]
    assert updated["description"] == "改版"

    session = db_session_factory()
    try:
        rows = session.execute(select(SavedSpec)).scalars().all()
        assert len(rows) == 1
    finally:
        session.close()
    # 列表只有这一条，回传的全文是改版后的规格
    listed = client.get("/api/word-formatter/specs/saved", headers=headers)
    assert listed.status_code == 200
    assert [s["id"] for s in listed.json()] == [created["id"]]
    assert listed.json()[0]["spec_json"] == other_spec


def test_save_rejects_invalid_spec_without_side_effects(
    client: TestClient,
    usage_user: Callable[..., User],
    db_session_factory: sessionmaker,
) -> None:
    user = usage_user()
    response = client.post(
        "/api/word-formatter/specs/save",
        json={"name": "坏规格", "spec_json": "{oops"},
        headers={"X-Card-Key": user.card_key},
    )
    assert response.status_code == 400
    session = db_session_factory()
    try:
        assert session.execute(select(SavedSpec)).scalars().all() == []
    finally:
        session.close()


def test_saved_spec_ownership_isolation_and_delete(
    client: TestClient,
    usage_user: Callable[..., User],
) -> None:
    owner = usage_user(card_key="card-spec-owner")
    stranger = usage_user(card_key="card-spec-stranger")
    owner_headers = {"X-Card-Key": owner.card_key}
    stranger_headers = {"X-Card-Key": stranger.card_key}

    created = client.post(
        "/api/word-formatter/specs/save",
        json={"name": "私有规格", "spec_json": VALID_SPEC_JSON},
        headers=owner_headers,
    ).json()
    spec_id = created["id"]

    # 列表只回本人规格；他人单条 404（不泄露存在性）、删除 404
    assert [s["id"] for s in client.get("/api/word-formatter/specs/saved", headers=stranger_headers).json()] == []
    assert client.get(f"/api/word-formatter/specs/saved/{spec_id}", headers=stranger_headers).status_code == 404
    assert client.delete(f"/api/word-formatter/specs/saved/{spec_id}", headers=stranger_headers).status_code == 404

    # 本人单条可取、删除后不可再取（删除 204，回读 404）
    fetched = client.get(f"/api/word-formatter/specs/saved/{spec_id}", headers=owner_headers)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "私有规格"
    assert client.delete(f"/api/word-formatter/specs/saved/{spec_id}", headers=owner_headers).status_code == 204
    assert client.get(f"/api/word-formatter/specs/saved/{spec_id}", headers=owner_headers).status_code == 404


# ---- 规格 schema：手写规格的依据（M4-T2 漏随迁，本票补齐） ----


def test_spec_schema_returns_render_lib_schema(
    client: TestClient,
    usage_user: Callable[..., User],
) -> None:
    user = usage_user()
    response = client.get("/api/word-formatter/specs/schema", headers={"X-Card-Key": user.card_key})
    assert response.status_code == 200, response.text
    schema = response.json()["schema"]
    assert schema["title"] == "StyleSpec"
    assert "page" in schema["properties"] and "styles" in schema["properties"]
    assert client.get("/api/word-formatter/specs/schema").status_code == 401
