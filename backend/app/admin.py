"""管理端（运营商后台）API：登录认证、卡密批量生成、用户列表、账本流水查询、
会话监控（列表/详情，只读）、系统配置只读面。

认证模型：管理凭据只来自 .env（ADR-0005，改凭据 = 改文件 + 重启，不存在
后台改密码的路径）。登录成功签发 HS256 JWT 存入 HttpOnly cookie，后续请求
凭该 cookie 识别运营商；无 cookie / cookie 无效 / 未配置凭据一律 401，
不区分失败原因（不向探测者泄露信息差异，与卡密认证同一纪律）。

密码与卡密同一条红线：只经 POST body 传输，绝不进 URL/query——进 URL 的
参数会落入 Nginx/访问日志（rewrite-plan §5）。凭据比较用常数时间比较，
防止时序侧信道逐字节猜密码。
"""

import secrets
import time
from datetime import datetime
from functools import reduce
from typing import Annotated

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.orm import Session

from app import billing, llm, optimization, prompts
from app.billing import BillingError, InsufficientBalance
from app.config import settings
from app.db import get_session
from app.models import (
    CustomPrompt,
    LedgerEntry,
    OptimizationSession,
    Segment,
    User,
    mask_card_key,
)

router = APIRouter(prefix="/api/admin")

_COOKIE_NAME = "admin_session"
_TOKEN_TTL_SECONDS = 12 * 3600  # 12 小时后需重新登录；无"记住我"等旁路


def _create_token(username: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": username, "iat": now, "exp": now + _TOKEN_TTL_SECONDS},
        settings.jwt_secret,
        algorithm="HS256",
    )


def get_current_admin(
    admin_session: Annotated[str | None, Cookie(alias=_COOKIE_NAME)] = None,
) -> str:
    """校验会话 cookie，返回运营商用户名；缺失或无效一律 401。"""
    if not admin_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="未登录")
    try:
        payload = jwt.decode(
            admin_session, settings.jwt_secret, algorithms=["HS256"]
        )
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="未登录") from None
    username = payload.get("sub")
    if not isinstance(username, str):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="未登录")
    return username


def _credentials_configured() -> bool:
    return bool(settings.admin_username and settings.admin_password and settings.jwt_secret)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    username: str


@router.post("/login")
def login(body: LoginRequest, response: Response) -> LoginResponse:
    if not _credentials_configured():
        # 未配置凭据与"密码错误"不可区分：管理端没准备好就等于谁都进不来
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    username_ok = secrets.compare_digest(
        body.username.encode("utf-8"), settings.admin_username.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        body.password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )
    if not (username_ok and password_ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    response.set_cookie(
        _COOKIE_NAME,
        _create_token(body.username),
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="lax",
        max_age=_TOKEN_TTL_SECONDS,
    )
    return LoginResponse(username=body.username)


class SessionResponse(BaseModel):
    """当前管理会话归属。前端外壳启动时经此区分登录态：200 即已登录，
    401 即未登录/已过期，回登录表单——不靠尝试业务接口探測登录态。"""

    username: str


@router.get("/session")
def read_session(admin: str = Depends(get_current_admin)) -> SessionResponse:
    return SessionResponse(username=admin)


class LogoutResponse(BaseModel):
    ok: bool


@router.post("/logout")
def logout(
    response: Response,
    _admin: str = Depends(get_current_admin),
) -> LogoutResponse:
    """主动登出：清除会话 cookie（属性与签发时一致，浏览器才会真正丢弃）。
    已过期会话登出会得到 401，cookie 里的 JWT 本就已失效，行为等价。"""
    response.delete_cookie(
        _COOKIE_NAME,
        secure=settings.admin_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return LogoutResponse(ok=True)


class CardKeyBatchRequest(BaseModel):
    """批量发卡参数：数量与初始双轨额度（余额分 + 次数上限），次数已用归零。"""

    count: int = Field(ge=1, le=500)
    balance_fen: int = Field(ge=0)
    usage_limit: int = Field(ge=0)


class IssuedCardKey(BaseModel):
    card_key: str
    balance_fen: int
    usage_limit: int


class CardKeyBatchResponse(BaseModel):
    card_keys: list[IssuedCardKey]


def _generate_card_key(session: Session) -> str:
    """生成全局唯一卡密。token_urlsafe(18) ≈ 144 位熵，撞库概率可忽略；
    唯一性仍逐把校验（DB 有 unique 约束兜底），循环直到不冲突。"""
    while True:
        card_key = secrets.token_urlsafe(18)
        if session.execute(select(User.id).where(User.card_key == card_key)).first() is None:
            return card_key


@router.post("/card-keys", status_code=status.HTTP_201_CREATED)
def create_card_keys(
    body: CardKeyBatchRequest,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> CardKeyBatchResponse:
    # 用户行以零额度创建，初始双轨额度统一走计费服务入账（发卡即充值，
    # 落账本后 SUM(账本)==余额 对新卡同样成立，见 app.billing.grant_initial_quota）。
    users = [User(card_key=_generate_card_key(session)) for _ in range(body.count)]
    session.add_all(users)
    session.flush()  # 生成主键后计费服务才能入账
    for user in users:
        billing.grant_initial_quota(
            session,
            user.id,
            balance_fen=body.balance_fen,
            usage_count=body.usage_limit,
            reason="发卡初始额度",
        )
    return CardKeyBatchResponse(
        card_keys=[
            IssuedCardKey(card_key=u.card_key, balance_fen=u.balance_fen, usage_limit=u.usage_limit)
            for u in users
        ]
    )


class AdminUserSummary(BaseModel):
    """用户列表行：卡密脱敏展示（运营商发卡时已拿到完整卡密，列表不必再露全量）。"""

    id: int
    card_key: str
    balance_fen: int
    usage_limit: int
    usage_remaining: int


class AdminUserPage(BaseModel):
    """用户分页列表。total 为过滤后的用户总数，供前端换算总页数。"""

    items: list[AdminUserSummary]
    total: int
    page: int
    page_size: int


@router.get("/users")
def list_users(
    page: Annotated[int, Field(ge=1)] = 1,
    page_size: Annotated[int, Field(ge=1, le=100)] = 20,
    q: Annotated[str | None, Field(max_length=64)] = None,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> AdminUserPage:
    """用户分页列表，可按卡密片段或用户 ID 搜索（客诉定位入口，spec #15
    用户故事 4/5）。搜索词 q 为一个可选参数：纯数字视为用户 ID 精确匹配，
    其余（含数字与非数字混合片段）按卡密子串匹配——调用方不区分两种搜索，
    同一个输入框两种都能命中。

    纯只读接口：不写 users 资金字段、不落账本行（账本只归计费服务写）。
    """
    conditions = []
    if q is not None and q.strip():
        q = q.strip()
        if q.isdigit():
            conditions.append(or_(User.id == int(q), User.card_key.contains(q, autoescape=True)))
        else:
            conditions.append(User.card_key.contains(q, autoescape=True))
    where = reduce(and_, conditions) if conditions else true()

    total = session.execute(select(func.count()).select_from(User).where(where)).scalar_one()
    users = (
        session.execute(
            select(User)
            .where(where)
            .order_by(User.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return AdminUserPage(
        items=[
            AdminUserSummary(
                id=u.id,
                card_key=mask_card_key(u.card_key),
                balance_fen=u.balance_fen,
                usage_limit=u.usage_limit,
                usage_remaining=u.usage_remaining,
            )
            for u in users
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


class BalanceAdjustRequest(BaseModel):
    """充值/调整余额：delta_fen 为正即充值，为负即扣减调整，不允许为 0。"""

    delta_fen: int
    reason: str = Field(min_length=1, max_length=200)


class BalanceAdjustResponse(BaseModel):
    user_id: int
    balance_fen: int


@router.post("/users/{user_id}/balance")
def adjust_user_balance(
    user_id: int,
    body: BalanceAdjustRequest,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> BalanceAdjustResponse:
    """运营商充值/调整余额。全部变动经计费服务落账本（带原因），
    用户端额度接口下一次读取即见新值。"""
    if body.delta_fen == 0:
        raise HTTPException(422, detail="delta_fen 不允许为 0")
    if session.get(User, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="用户不存在")
    try:
        balance_fen = billing.adjust_balance(session, user_id, body.delta_fen, reason=body.reason)
    except InsufficientBalance:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="余额不足，无法完成本次扣减调整"
        ) from None
    return BalanceAdjustResponse(user_id=user_id, balance_fen=balance_fen)


class UsageAdjustRequest(BaseModel):
    """充值/调整次数额度：delta 为正即增加上限，为负即调低，不允许为 0。"""

    delta: int
    reason: str = Field(min_length=1, max_length=200)


class UsageAdjustResponse(BaseModel):
    user_id: int
    usage_limit: int
    usage_remaining: int


@router.post("/users/{user_id}/usage")
def adjust_user_usage(
    user_id: int,
    body: UsageAdjustRequest,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> UsageAdjustResponse:
    """运营商调整次数额度（spec #3 用户故事 8，Word Formatter 权益可控）。
    全部变动经计费服务落账本（带原因），用户端额度接口下一次读取即见新值。"""
    if body.delta == 0:
        raise HTTPException(422, detail="delta 不允许为 0")
    if session.get(User, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="用户不存在")
    try:
        usage_remaining = billing.adjust_usage_limit(
            session, user_id, body.delta, reason=body.reason
        )
    except BillingError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="调整后的次数上限低于已用次数"
        ) from None
    user = session.get(User, user_id)
    return UsageAdjustResponse(
        user_id=user_id, usage_limit=user.usage_limit, usage_remaining=usage_remaining
    )


class AdminLedgerEntry(BaseModel):
    """账本流水行：kind 的合法取值由 app.billing 拥有（充值/扣费/退款/
    余额调整/次数预留/释放/额度调整）。amount_fen 为带符号增量（次数轨
    恒为 0）；usage_after 仅次数轨非空。"""

    id: int
    user_id: int
    task_id: int | None
    kind: str
    amount_fen: int
    balance_after_fen: int
    usage_after: int | None
    reason: str
    created_at: datetime


class AdminLedgerPage(BaseModel):
    """账本分页（新流水在前）。按用户查询时额外返回该用户的脱敏卡密，
    供流水页标题展示；全局查询两字段为 None。"""

    items: list[AdminLedgerEntry]
    total: int
    page: int
    page_size: int
    user_id: int | None = None
    card_key: str | None = None


def _ledger_page(
    session: Session,
    page: int,
    page_size: int,
    user_id: int | None = None,
) -> AdminLedgerPage:
    """账本分页查询（只读）：按 id 倒序即新流水在前——账本行只增不改，
    id 单调递增，与写入时间同序（created_at 只有秒级精度，id 更稳定）。
    不写 users 资金字段、不落账本行：账本只归计费服务写。"""
    where = LedgerEntry.user_id == user_id if user_id is not None else true()
    total = session.execute(
        select(func.count()).select_from(LedgerEntry).where(where)
    ).scalar_one()
    entries = (
        session.execute(
            select(LedgerEntry)
            .where(where)
            .order_by(LedgerEntry.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return AdminLedgerPage(
        items=[
            AdminLedgerEntry(
                id=e.id,
                user_id=e.user_id,
                task_id=e.task_id,
                kind=e.kind,
                amount_fen=e.amount_fen,
                balance_after_fen=e.balance_after_fen,
                usage_after=e.usage_after,
                reason=e.reason,
                created_at=e.created_at,
            )
            for e in entries
        ],
        total=total,
        page=page,
        page_size=page_size,
        user_id=user_id,
    )


@router.get("/ledger")
def list_recent_ledger(
    page: Annotated[int, Field(ge=1)] = 1,
    page_size: Annotated[int, Field(ge=1, le=100)] = 20,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> AdminLedgerPage:
    """全局最近流水（新流水在前），不点开每个用户也能发现异常。"""
    return _ledger_page(session, page, page_size)


@router.get("/users/{user_id}/ledger")
def list_user_ledger(
    user_id: int,
    page: Annotated[int, Field(ge=1)] = 1,
    page_size: Annotated[int, Field(ge=1, le=100)] = 20,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> AdminLedgerPage:
    """某用户的全部账本流水（客诉时回答"我的钱去哪了"）。用户不存在 404。"""
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="用户不存在")
    ledger_page = _ledger_page(session, page, page_size, user_id=user_id)
    ledger_page.card_key = mask_card_key(user.card_key)
    return ledger_page


# ---- 会话监控（issue #28，只读接口）----
#
# 监控页回答两个问题：系统此刻在跑什么（列表），以及客诉时"这次用的是
# 哪个模型、坏在哪一段、钱退了没"（详情）。两个接口都纯只读：不写会话/
# 段落/账本，不触发任何状态迁移或退款——页面看到的就是数据库此刻的状态，
# 变动只来自 Worker/用户端/计费服务各自的写路径。


class AdminSessionSummary(BaseModel):
    """会话列表行：所属用户以脱敏卡密展示（运营商凭它把会话对上人，
    规则随卡密概念拥有，见 models.mask_card_key）；计费字段与会话表同源只读。"""

    id: int
    user_id: int
    card_key: str
    status: str
    mode: str
    word_count: int
    charged_fen: int
    model_snapshot: str | None
    created_at: datetime


class AdminSessionPage(BaseModel):
    """会话分页（新会话在前）。total 为过滤后的会话总数，供前端换算总页数。"""

    items: list[AdminSessionSummary]
    total: int
    page: int
    page_size: int


@router.get("/sessions")
def list_sessions(
    page: Annotated[int, Field(ge=1)] = 1,
    page_size: Annotated[int, Field(ge=1, le=100)] = 20,
    status: Annotated[optimization.SessionStatus | None, Query()] = None,
    user_id: Annotated[int | None, Query(ge=1)] = None,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> AdminSessionPage:
    """会话分页列表，可按状态与所属用户过滤（spec #15 用户故事 10-11）。

    user_id 过滤是客诉排查闭环的最后一跳：凭卡密片段在用户列表找到人后，
    从用户行直达他的会话与当时的模型。状态词表复用
    app.optimization.SessionStatus（词表归会话模块所有），词表外的取值由
    FastAPI 映射 422。
    """
    conditions = []
    if status is not None:
        conditions.append(OptimizationSession.status == status)
    if user_id is not None:
        conditions.append(OptimizationSession.user_id == user_id)
    where = reduce(and_, conditions) if conditions else true()
    total = session.execute(
        select(func.count()).select_from(OptimizationSession).where(where)
    ).scalar_one()
    rows = session.execute(
        select(OptimizationSession, User.card_key)
        .join(User, User.id == OptimizationSession.user_id)
        .where(where)
        .order_by(OptimizationSession.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return AdminSessionPage(
        items=[
            AdminSessionSummary(
                id=row.id,
                user_id=row.user_id,
                card_key=mask_card_key(card_key),
                status=row.status,
                mode=row.mode,
                word_count=row.word_count,
                charged_fen=row.charged_fen,
                model_snapshot=row.model_snapshot,
                created_at=row.created_at,
            )
            for row, card_key in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


class AdminSegmentStatus(BaseModel):
    """段落阶段状态：状态机取值 pending/completed/failed，含失败段落
    （客诉时判断故障发生在哪一段）。"""

    stage: str
    seq: int
    status: str


class AdminSessionLedgerRow(BaseModel):
    """该会话的账本行（task_id 即会话 id）：只有预扣 charge 与退款 refund
    两类与任务相关。退款行是否存在即"退款是否已落账"——账本是资金变动的
    唯一事实源，这里不另造冗余的退款状态字段。"""

    id: int
    kind: str
    amount_fen: int
    balance_after_fen: int
    reason: str
    created_at: datetime


class AdminSessionDetail(BaseModel):
    """会话详情：列表行全量字段 + 各段落阶段状态 + 该会话的账本行。"""

    id: int
    user_id: int
    card_key: str
    status: str
    mode: str
    word_count: int
    charged_fen: int
    model_snapshot: str | None
    created_at: datetime
    updated_at: datetime
    segments: list[AdminSegmentStatus]
    ledger: list[AdminSessionLedgerRow]


@router.get("/sessions/{session_id}")
def get_session_detail(
    session_id: int,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> AdminSessionDetail:
    """单个会话的监控详情（spec #15 用户故事 12-14）：

    - model_snapshot 只读展示当时实际用的模型（Worker 认领时一次写入，
      queued 期为 None——还没认领就没有模型可看）；
    - 段落阶段状态润色段在前、段内按序号（与用户端同一排序契约
      optimization.stage_order_for），失败段落原样可见；
    - 账本行按 id 正序即时间正序：先预扣后退款。

    会话不存在 404。纯只读：不写任何表、不触发状态迁移或退款。
    """
    row = session.get(OptimizationSession, session_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="会话不存在")
    card_key = session.scalar(select(User.card_key).where(User.id == row.user_id))
    segments = session.execute(
        select(Segment)
        .where(Segment.session_id == session_id)
        .order_by(optimization.stage_order_for(Segment.stage), Segment.seq)
    ).scalars()
    ledger_rows = session.execute(
        select(LedgerEntry)
        .where(
            LedgerEntry.task_id == session_id,
            LedgerEntry.kind.in_([billing.KIND_CHARGE, billing.KIND_REFUND]),
        )
        .order_by(LedgerEntry.id)
    ).scalars()
    return AdminSessionDetail(
        id=row.id,
        user_id=row.user_id,
        card_key=mask_card_key(card_key),
        status=row.status,
        mode=row.mode,
        word_count=row.word_count,
        charged_fen=row.charged_fen,
        model_snapshot=row.model_snapshot,
        created_at=row.created_at,
        updated_at=row.updated_at,
        segments=[
            AdminSegmentStatus(stage=s.stage, seq=s.seq, status=s.status)
            for s in segments
        ],
        ledger=[
            AdminSessionLedgerRow(
                id=e.id,
                kind=e.kind,
                amount_fen=e.amount_fen,
                balance_after_fen=e.balance_after_fen,
                reason=e.reason,
                created_at=e.created_at,
            )
            for e in ledger_rows
        ],
    )


# ---- 系统配置只读面（issue #29）----
#
# 运营商不登服务器就能确认服务配置。配置以 .env 为唯一来源（ADR-0005，
# 改配置 = 改 .env + 重启），本接口是纯只读快照：后端不存在任何改配置的
# 接口或表，页面能做的只有"看"。


class ModelAccessStatus(BaseModel):
    """模型接入三项状态。base_url 与模型名不是秘密，原值展示（排查"用的
    哪个模型/哪个接入点"的入口）；api_key 绝不返回值，只给是否已配置。"""

    configured: bool  # 三项齐备 = 润色 Worker 随应用启动（app.main.lifespan 同一判据）
    base_url: str | None
    model: str | None
    api_key_configured: bool


class SecretStatus(BaseModel):
    """敏感配置项状态：值（密码/密钥）绝不离开服务进程，只给是否已配置的
    布尔——页面截图/录屏/接口响应都不泄露凭据。"""

    configured: bool


class AdminConfigView(BaseModel):
    """运行参数与模型接入状态的只读快照。敏感项在响应模型里只有布尔字段，
    敏感值在结构上进不了任何响应（含 Swagger schema）——零泄露靠结构保证，
    不靠"序列化前记得脱敏"。"""

    recovery_max_attempts: int
    recovery_stuck_after_seconds: int
    recovery_patrol_interval_seconds: float
    max_session_text_chars: int
    llm: ModelAccessStatus
    admin_password: SecretStatus
    jwt_secret: SecretStatus


@router.get("/config")
def read_config(_admin: str = Depends(get_current_admin)) -> AdminConfigView:
    """系统配置只读面（spec #15 用户故事 15-16）：运行参数 + 模型接入状态。

    展示的运行参数与 config.Settings 字段同名同义（恢复协议参数、会话文本
    上限）；模型接入三项的齐备判据与 app.llm.is_configured 同源。敏感项
    （LLM key、管理密码、JWT 密钥）只返回是否已配置，绝不返回值。
    """
    return AdminConfigView(
        recovery_max_attempts=settings.recovery_max_attempts,
        recovery_stuck_after_seconds=settings.recovery_stuck_after_seconds,
        recovery_patrol_interval_seconds=settings.recovery_patrol_interval_seconds,
        max_session_text_chars=settings.max_session_text_chars,
        llm=ModelAccessStatus(
            configured=llm.is_configured(),
            base_url=settings.llm_base_url or None,
            model=settings.llm_model or None,
            api_key_configured=bool(settings.llm_api_key),
        ),
        admin_password=SecretStatus(configured=bool(settings.admin_password)),
        jwt_secret=SecretStatus(configured=bool(settings.jwt_secret)),
    )


# ---- 提示词管理（issue #30，issue #23 方案 A：DB 为唯一事实源）----
#
# 系统提示词的唯一正文来源是 custom_prompts 的 is_system 行（解析回落链与
# "同阶段至多一个默认"不变量归 app.prompts 拥有）：编辑走本组接口落库，改完
# 即对后续会话生效，无需发版。用户自定义行只读可见（排查"润色效果不对"时看
# 用户的默认提示词）；默认的选择权在用户手里，运营商不代改。写路径只有系统行
# 正文与默认两项，不触碰任何资金字段。


class AdminPrompt(BaseModel):
    """提示词行。user_id 为空即系统行（正文可编辑）；用户行 card_key 为脱敏
    形态（客诉时对上人）。content 全文返回——本组接口就是编辑入口。"""

    id: int
    user_id: int | None
    card_key: str | None
    name: str
    stage: str
    content: str
    is_default: bool
    is_system: bool
    is_active: bool
    updated_at: datetime


class AdminPromptPage(BaseModel):
    """提示词分页（系统行在前、行内按 id，阶段分组稳定）。total 为过滤后
    的总数，供前端换算总页数。"""

    items: list[AdminPrompt]
    total: int
    page: int
    page_size: int


def _prompt_view(session: Session, row: CustomPrompt) -> AdminPrompt:
    card_key = None
    if row.user_id is not None:
        card_key = mask_card_key(
            session.scalar(select(User.card_key).where(User.id == row.user_id))
        )
    return AdminPrompt(
        id=row.id,
        user_id=row.user_id,
        card_key=card_key,
        name=row.name,
        stage=row.stage,
        content=row.content,
        is_default=row.is_default,
        is_system=row.is_system,
        is_active=row.is_active,
        updated_at=row.updated_at,
    )


@router.get("/prompts")
def list_prompts(
    page: Annotated[int, Field(ge=1)] = 1,
    page_size: Annotated[int, Field(ge=1, le=100)] = 20,
    stage: Annotated[str | None, Field(max_length=50)] = None,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> AdminPromptPage:
    """提示词分页列表：系统行 + 用户自定义行同页可辨（scope 看字段，不靠
    接口分叉），stage 过滤可选（词表外的取值过滤后为空，不报错）。纯只读。"""
    where = CustomPrompt.stage == stage if stage else true()
    total = session.execute(
        select(func.count()).select_from(CustomPrompt).where(where)
    ).scalar_one()
    rows = session.execute(
        select(CustomPrompt, User.card_key)
        .join(User, User.id == CustomPrompt.user_id, isouter=True)
        .where(where)
        .order_by(CustomPrompt.stage, CustomPrompt.is_system.desc(), CustomPrompt.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return AdminPromptPage(
        items=[
            AdminPrompt(
                id=row.id,
                user_id=row.user_id,
                card_key=mask_card_key(card_key) if card_key is not None else None,
                name=row.name,
                stage=row.stage,
                content=row.content,
                is_default=row.is_default,
                is_system=row.is_system,
                is_active=row.is_active,
                updated_at=row.updated_at,
            )
            for row, card_key in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


class PromptContentUpdate(BaseModel):
    """系统行正文编辑：非空正文整体替换（无增量语义可言）。"""

    content: str = Field(min_length=1)


def _owned_system_prompt(
    session: Session,
    prompt_id: int,
    operation,  # prompts.edit_system_content | prompts.set_system_default
    **kwargs: object,
) -> CustomPrompt:
    """提示词写操作的统一异常映射：不存在 404、用户行只读 409——两个写
    端点共享同一张契约表，调用方（前端）只学一次错误语义。"""
    try:
        return operation(session, prompt_id, **kwargs)
    except prompts.PromptNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="提示词不存在") from None
    except prompts.PromptReadOnly:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="用户自定义提示词只读，不能修改"
        ) from None


@router.put("/prompts/{prompt_id}/content")
def edit_prompt_content(
    prompt_id: int,
    body: PromptContentUpdate,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> AdminPrompt:
    """编辑系统提示词正文（本里程碑唯一新增的写路径，落库即生效）：下一个
    会话的流水线消费点（app.worker / app.prompts）读到的就是新正文。用户行
    409 只读；不存在 404。不触碰任何资金字段。"""
    row = _owned_system_prompt(session, prompt_id, prompts.edit_system_content,
                               content=body.content)
    return _prompt_view(session, row)


@router.put("/prompts/{prompt_id}/default")
def set_prompt_default(
    prompt_id: int,
    _admin: str = Depends(get_current_admin),
    session: Session = Depends(get_session),
) -> AdminPrompt:
    """把系统行设为其阶段默认并收口"(user_id=NULL, stage) 至多一个默认"的
    不变量（app.prompts 拥有）：同阶段其余默认行同事务降级，重复/并发设默认
    被收口为恰好一个。用户行 409 只读；不存在 404。"""
    row = _owned_system_prompt(session, prompt_id, prompts.set_system_default)
    return _prompt_view(session, row)
