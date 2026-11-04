"""排版规格纵切（issue #36，GLOSSARY：排版规格）的唯一拥有者。

自然语言描述 → AI 生成规格 → 校验/保存复用 → 送格式化，规格侧全部端点
（内置规格列表与 schema、合法性校验、AI 同步生成、已存规格 CRUD）归本模块。

- **AI 同步生成**：同步接口不走任务表——模型未配置（ADR-0005）即 503 且
  零预留；配置齐备则预留 1 次后调 LLM，内容性失败退回预留（失败不吞次数）
  并以 502 收场。短事务纪律（app.db）：认证+预留、失败退回各自独立的自管
  短事务，LLM 等待期间零数据库事务（认证不占请求会话，见
  :func:`app.auth.authenticate_card_key`）。LLM 接缝与预处理同形
  （``complete(messages)``，issue #32），测试经 FastAPI 依赖注入脚本化假客户端。
  已知例外（已接受损失）：LLM 等待窗口内进程崩溃会使预留滞留——成功不落
  任何标记，启动扫描无法区分"已成功消费"与"崩溃滞留"，不做自动退回；
  客服排查见 docs/operations.md。
- **规格校验与保存**：坏 JSON / 非法规格一律 400 挡在保存之前；保存按
  （用户, 名称）同名覆盖（与旧版语义一致——更新而非新建），规格 JSON 全文
  原样存 saved_specs.spec_json（格式归渲染库 StyleSpec 拥有，本表只存取
  不解释，见 app.models.SavedSpec）。
- **已存规格归属**：单条/删除的归属判定与任务端点同一语义——他人规格 404，
  不泄露存在性。

内置规格名列表（GET /specs）注册在 app.word_jobs（格式化页选择器数据源），
本模块只补规格生成页需要的 schema 端点（issue #36 写明它属 M4-T2 随迁，
实际未随迁，在此补齐）。
"""

import json
from datetime import datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import billing, db as app_db, llm as app_llm
from app.auth import (
    CARD_KEY_HEADER,
    InvalidCardKey,
    authenticate_card_key,
    get_current_user,
)
from app.db import get_session, short_tx
from app.models import SavedSpec, User
from app.word_formatter.services import (
    ai_generate_spec,
    export_spec_to_json,
    get_spec_schema,
    validate_custom_spec,
)

router = APIRouter(prefix="/api/word-formatter")


class SpecGeneratingAI(Protocol):
    """规格生成对 LLM 的全部依赖：一次对话补全（与
    :class:`app.llm.PolishLLMClient.complete` 同形，测试接脚本化假客户端）。"""

    async def complete(self, messages: list[dict[str, str]]) -> str: ...


def get_spec_ai() -> SpecGeneratingAI:
    """AI 规格生成的 LLM 接缝（FastAPI 依赖）：生产按 .env 惰性构造
    :class:`app.llm.PolishLLMClient`；测试经 ``app.dependency_overrides``
    注入脚本化假客户端（与 Worker 的 marking_ai 注入同一纪律）。
    未配置（ADR-0005）在这里映射为 503——依赖先于路由体解析，503 契约
    （明确失败、零预留）只能由构造接缝自己兑现，路由体内的守卫到不了。"""
    if not app_llm.is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="模型接入未配置，AI 规格生成暂不可用",
        )
    from app.llm import PolishLLMClient

    return PolishLLMClient.from_settings()


# ---- AI 同步生成：503（零预留）→ 401/403 → 预留 → LLM（零事务）→ 502 退回 ----


class GenerateSpecRequest(BaseModel):
    """AI 生成请求：自然语言排版要求（老版同款下限，过短的需求生成不出
    可用规格）。上限 1 万字符防滥用——requirements 直接进 LLM 提示词，
    超长串等于放大每次生成的调用成本。"""

    requirements: str = Field(min_length=10, max_length=10_000)


async def _generate_spec_json(requirements: str, ai: SpecGeneratingAI) -> str:
    """预留次数之后的一次生成尝试：LLM 返回文本经渲染库清洗解析并校验为
    合法规格，返回导出 JSON。一切失败（LLM 错误/返回的不是合法规格 JSON）
    统一抛异常，由调用方退回预留并对外 502。"""
    spec = await ai_generate_spec(requirements, ai)
    return export_spec_to_json(spec)


@router.post("/specs/generate")
async def generate_spec(
    request: GenerateSpecRequest,
    card_key: Annotated[str | None, Header(alias=CARD_KEY_HEADER)] = None,
    ai: SpecGeneratingAI = Depends(get_spec_ai),
) -> dict:
    """AI 同步生成排版规格：503（未配置，零预留，见 get_spec_ai）→ 401
    → 403（次数不足）→ 预留 1 次 → LLM 生成（等待期间零数据库事务）→
    成功返回规格 JSON；失败同额退回预留并以 502 收场。task_id 为空
    （无任务行，账本 reason 可追溯）。"""
    with short_tx(app_db.SessionLocal) as session:
        try:
            user = authenticate_card_key(session, card_key)
        except InvalidCardKey as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            ) from exc
        try:
            billing.reserve_usage(
                session, user.id, 1, task_id=None, reason="AI 排版规格生成预留"
            )
        except billing.InsufficientUsage as exc:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail=str(exc)
            ) from exc
    try:
        spec_json = await _generate_spec_json(request.requirements, ai)
    except Exception as exc:
        with short_tx(app_db.SessionLocal) as session:
            billing.release_usage(
                session,
                user.id,
                1,
                task_id=None,
                reason=f"AI 规格生成失败退回：{exc}",
            )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=f"生成规格失败：{exc}"
        ) from exc
    return {"success": True, "spec_json": spec_json, "spec_name": _spec_name(spec_json)}


def _spec_name(spec_json: str) -> str:
    """规格展示名（meta.name，缺省回落 Custom；响应字段，不参与校验）。"""
    try:
        return json.loads(spec_json).get("meta", {}).get("name") or "Custom"
    except Exception:
        return "Custom"


# ---- 合法性校验：坏 JSON / 非法规格一律 400 挡在保存之前 ----


class ValidateSpecRequest(BaseModel):
    """规格校验请求：spec_json 是规格 JSON 全文（字符串形态，与保存一致，
    同一 10 万字符上限——真实规格 2–10KB，上限挡住滥用字符串与其解析开销）。"""

    spec_json: str = Field(max_length=100_000)


@router.post("/specs/validate")
def validate_spec(
    request: ValidateSpecRequest, user: User = Depends(get_current_user)
) -> dict:
    """校验规格 JSON：合法返回 valid 与规格名；坏 JSON 或非法规格 400
    （detail 带原因，前端直接展示）。与其他 word-formatter 端点一样要求
    卡密（401）——校验本身无副作用，但认证一致性优先。"""
    try:
        spec = validate_custom_spec(request.spec_json)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"valid": True, "spec_name": spec.meta.get("name", "Custom")}


@router.get("/specs/schema")
def spec_schema(user: User = Depends(get_current_user)) -> dict:
    """规格 JSON Schema（渲染库 StyleSpec 的 JSON Schema），手写规格的依据。"""
    return {"schema": get_spec_schema()}


# ---- 已存规格：校验后保存（同名覆盖）、查看与删除（他人规格 404） ----


class SaveSpecRequest(BaseModel):
    """保存请求：spec_json 先过渲染库校验再入库（坏规格挡在写入之前）；
    名称/描述长度与 saved_specs 列宽一致（SQLite 不强制，这里挡住）；
    spec_json 上限 10 万字符（真实规格 2–10KB）——字符串按原文入库并随
    列表整行回传，无上限时单个用户可撑爆表与列表响应。"""

    name: str = Field(min_length=1, max_length=100)
    spec_json: str = Field(max_length=100_000)
    description: str | None = Field(default=None, max_length=500)


class SavedSpecOut(BaseModel):
    """已存规格响应：spec_json 全文回传（编辑复用的直接输入）。"""

    id: int
    name: str
    description: str | None
    spec_json: str
    created_at: datetime
    updated_at: datetime


def _spec_out(spec: SavedSpec) -> SavedSpecOut:
    return SavedSpecOut(
        id=spec.id,
        name=spec.name,
        description=spec.description,
        spec_json=spec.spec_json,
        created_at=spec.created_at,
        updated_at=spec.updated_at,
    )


def _get_owned_spec(session: Session, user_id: int, spec_id: int) -> SavedSpec:
    """本人规格行；不存在或不属于当前用户抛 :class:`LookupError`
    （对外一律 404，不泄露他人规格存在性）。"""
    spec = session.execute(
        select(SavedSpec).where(SavedSpec.id == spec_id, SavedSpec.user_id == user_id)
    ).scalar_one_or_none()
    if spec is None:
        raise LookupError(f"规格 {spec_id} 不存在")
    return spec


@router.post("/specs/save", response_model=SavedSpecOut)
def save_spec(
    request: SaveSpecRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> SavedSpecOut:
    """保存规格为本人常用规格：校验（400）→ 同名覆盖（同一 (用户, 名称)
    更新而非新建，旧版语义）→ 否则新建。"""
    try:
        validate_custom_spec(request.spec_json)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"规格 JSON 无效：{exc}"
        ) from exc
    existing = session.execute(
        select(SavedSpec).where(
            SavedSpec.user_id == user.id, SavedSpec.name == request.name
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.spec_json = request.spec_json
        existing.description = request.description
        existing.updated_at = func.current_timestamp()
        session.flush()
        session.refresh(existing)  # updated_at 是 SQL 表达式，回读落定的值
        return _spec_out(existing)
    spec = SavedSpec(
        user_id=user.id,
        name=request.name,
        description=request.description,
        spec_json=request.spec_json,
    )
    session.add(spec)
    session.flush()
    return _spec_out(spec)


@router.get("/specs/saved", response_model=list[SavedSpecOut])
def list_saved_specs(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[SavedSpecOut]:
    """本人全部已存规格，最近更新在前。"""
    specs = session.execute(
        select(SavedSpec)
        .where(SavedSpec.user_id == user.id)
        .order_by(SavedSpec.updated_at.desc(), SavedSpec.id.desc())
    ).scalars()
    return [_spec_out(spec) for spec in specs]


@router.get("/specs/saved/{spec_id}", response_model=SavedSpecOut)
def get_saved_spec(
    spec_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> SavedSpecOut:
    """单条已存规格；他人规格 404（不泄露存在性）。"""
    try:
        return _spec_out(_get_owned_spec(session, user.id, spec_id))
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete("/specs/saved/{spec_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved_spec(
    spec_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> None:
    """删除本人已存规格；他人规格 404。"""
    try:
        session.delete(_get_owned_spec(session, user.id, spec_id))
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
