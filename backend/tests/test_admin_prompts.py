"""提示词管理接口（issue #30，issue #23 方案 A：DB 为唯一事实源，HTTP API 接缝）。

验收红线：提示词接口无管理会话 cookie 一律 401；系统行正文编辑落库即生效
（不存在第二事实源）；用户自定义行后台只读（409，正文与默认都不代改）；
(user_id=NULL, stage) 至多一个默认的不变量由接口维护（重复设默认被收口）；
全部接口对资金字段零写入（不落账本行）。
"""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import seeds
from app.models import CustomPrompt, LedgerEntry, User


def _seed_system_prompts(db_session_factory: sessionmaker) -> list[CustomPrompt]:
    """空库注入系统种子（与 app.main.lifespan / 迁移脚本同一入口）。"""
    session = db_session_factory()
    try:
        seeds.seed_system_prompts(session)
        session.commit()
        return list(
            session.execute(
                select(CustomPrompt).where(CustomPrompt.is_system.is_(True))
            ).scalars()
        )
    finally:
        session.close()


def _insert_user_prompt(
    db_session_factory: sessionmaker,
    user_id: int,
    *,
    stage: str = "polish",
    content: str = "用户的自定义提示词",
    is_default: bool = False,
) -> CustomPrompt:
    session = db_session_factory()
    try:
        row = CustomPrompt(
            user_id=user_id,
            name=f"user-prompt-{stage}",
            stage=stage,
            content=content,
            is_default=is_default,
            is_active=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    finally:
        session.close()


def _system_prompts(db_session_factory: sessionmaker) -> list[CustomPrompt]:
    session = db_session_factory()
    try:
        return list(
            session.execute(
                select(CustomPrompt)
                .where(CustomPrompt.is_system.is_(True))
                .order_by(CustomPrompt.stage)
            ).scalars()
        )
    finally:
        session.close()


# ---- 鉴权：无管理会话 cookie 一律 401 ----


def test_prompt_endpoints_require_login(client: TestClient) -> None:
    """列表与两个写端点，无 cookie 一律 401（管理端统一纪律，不区分原因）。"""
    assert client.get("/api/admin/prompts").status_code == 401
    assert (
        client.put("/api/admin/prompts/1/content", json={"content": "x"}).status_code == 401
    )
    assert client.put("/api/admin/prompts/1/default").status_code == 401


# ---- 列表：系统 + 用户自定义同页可辨，用户行只读可见 ----


def test_list_prompts_shows_system_and_user_rows(
    logged_in_client: TestClient,
    db_session_factory: sessionmaker,
    make_user: Callable[..., User],
) -> None:
    system_rows = _seed_system_prompts(db_session_factory)
    user = make_user(card_key="card-prompt-list")
    user_row = _insert_user_prompt(
        db_session_factory, user.id, stage="polish", content="用户默认润色", is_default=True
    )

    response = logged_in_client.get("/api/admin/prompts")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == len(system_rows) + 1
    by_id = {item["id"]: item for item in body["items"]}

    system_item = by_id[system_rows[0].id]
    assert system_item["is_system"] is True
    assert system_item["user_id"] is None
    assert system_item["card_key"] is None
    assert system_item["content"] == system_rows[0].content  # 全文返回，本页即编辑入口

    user_item = by_id[user_row.id]
    assert user_item["is_system"] is False
    assert user_item["user_id"] == user.id
    assert user_item["card_key"] == "card****list"  # 脱敏卡密：客诉时对上人
    assert user_item["content"] == "用户默认润色"
    assert user_item["is_default"] is True


def test_list_prompts_filters_by_stage(
    logged_in_client: TestClient, db_session_factory: sessionmaker
) -> None:
    _seed_system_prompts(db_session_factory)

    response = logged_in_client.get("/api/admin/prompts", params={"stage": "polish"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["stage"] for item in body["items"]] == ["polish"]

    unknown = logged_in_client.get("/api/admin/prompts", params={"stage": "no-such"})
    assert unknown.status_code == 200
    assert unknown.json()["items"] == []


# ---- 系统行正文编辑：落库即生效，不存在第二事实源 ----


def test_edit_system_prompt_content_persists(
    logged_in_client: TestClient,
    db_session_factory: sessionmaker,
    make_user: Callable[..., User],
) -> None:
    (polish_row,) = [r for r in _seed_system_prompts(db_session_factory) if r.stage == "polish"]

    response = logged_in_client.put(
        f"/api/admin/prompts/{polish_row.id}/content",
        json={"content": "运营调优后的润色提示词"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "运营调优后的润色提示词"
    assert body["is_system"] is True
    (row,) = [r for r in _system_prompts(db_session_factory) if r.id == polish_row.id]
    assert row.content == "运营调优后的润色提示词"  # 唯一事实源就是库内这一行


def test_edit_user_prompt_is_rejected_and_content_unchanged(
    logged_in_client: TestClient,
    db_session_factory: sessionmaker,
    make_user: Callable[..., User],
) -> None:
    user = make_user(card_key="card-prompt-ro")
    user_row = _insert_user_prompt(db_session_factory, user.id, content="用户的正文")

    response = logged_in_client.put(
        f"/api/admin/prompts/{user_row.id}/content", json={"content": "后台代改"}
    )

    assert response.status_code == 409
    session = db_session_factory()
    try:
        assert session.get(CustomPrompt, user_row.id).content == "用户的正文"
    finally:
        session.close()


def test_edit_missing_prompt_returns_404(logged_in_client: TestClient) -> None:
    response = logged_in_client.put(
        "/api/admin/prompts/99999/content", json={"content": "x"}
    )
    assert response.status_code == 404


def test_empty_content_is_rejected(logged_in_client: TestClient) -> None:
    assert (
        logged_in_client.put(
            "/api/admin/prompts/1/content", json={"content": ""}
        ).status_code
        == 422
    )


# ---- 默认不变量：同阶段至多一个默认，重复/脏数据被收口 ----


def test_set_default_demotes_other_defaults_and_is_idempotent(
    logged_in_client: TestClient,
    db_session_factory: sessionmaker,
) -> None:
    """旧库迁入的脏数据（同阶段两个系统默认）+ 重复设默认，都被收口为恰好一个。"""
    (polish_row,) = [r for r in _seed_system_prompts(db_session_factory) if r.stage == "polish"]
    session = db_session_factory()
    try:
        session.add(
            CustomPrompt(
                user_id=None,
                name="遗留的第二个润色默认",
                stage="polish",
                content="旧默认正文",
                is_default=True,
                is_system=True,
                is_active=True,
            )
        )
        session.commit()
    finally:
        session.close()

    for _ in range(2):  # 重复设默认必须幂等
        response = logged_in_client.put(f"/api/admin/prompts/{polish_row.id}/default")
        assert response.status_code == 200
        assert response.json()["is_default"] is True

    defaults = [r for r in _system_prompts(db_session_factory) if r.stage == "polish" and r.is_default]
    assert len(defaults) == 1
    assert defaults[0].id == polish_row.id
    # 默认即消费语义：设默认同时启用该行
    assert defaults[0].is_active is True


def test_set_default_on_user_prompt_or_missing_row(
    logged_in_client: TestClient,
    db_session_factory: sessionmaker,
    make_user: Callable[..., User],
) -> None:
    user = make_user(card_key="card-prompt-default")
    user_row = _insert_user_prompt(db_session_factory, user.id, is_default=True)

    assert (
        logged_in_client.put(f"/api/admin/prompts/{user_row.id}/default").status_code == 409
    )
    assert logged_in_client.put("/api/admin/prompts/99999/default").status_code == 404


# ---- 资金只读：提示词接口不产生任何账本行 ----


def test_prompt_endpoints_never_touch_funds(
    logged_in_client: TestClient,
    db_session_factory: sessionmaker,
    make_user: Callable[..., User],
) -> None:
    (polish_row,) = [r for r in _seed_system_prompts(db_session_factory) if r.stage == "polish"]

    assert logged_in_client.get("/api/admin/prompts").status_code == 200
    assert (
        logged_in_client.put(
            f"/api/admin/prompts/{polish_row.id}/content", json={"content": "新正文"}
        ).status_code
        == 200
    )
    assert logged_in_client.put(f"/api/admin/prompts/{polish_row.id}/default").status_code == 200

    session = db_session_factory()
    try:
        assert session.execute(select(LedgerEntry)).first() is None
    finally:
        session.close()
