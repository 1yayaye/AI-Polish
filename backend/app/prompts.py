"""提示词服务：系统提示词的唯一事实源与默认不变量的归属模块（issue #23 裁决，
方案 A，issue #30 落地）。

事实源裁决：custom_prompts 的 is_system 行（user_id 为空）是系统提示词的
唯一正文来源。运营商在管理后台编辑的就是库内行，改完即对后续会话生效，
无需发版——不存在"代码/文件里的第二份正文"，两源漂移被结构性消除。
app/seed_data/system_prompts.json 降级为空库首次注入的种子（app.seeds，
幂等：按 (stage, name) 判重、不覆盖已改动行），不参与运行时解析。

解析回落链（Worker 每阶段的消费点）：该用户在该阶段启用的自定义默认行 →
库内该阶段默认启用的系统行。"同用户同阶段至多一个默认提示词"的不变量由
本模块拥有，管理接口是维护它的唯一写路径（用户自定义行后台只读——默认的
选择权在用户手里，运营商只查看不代改）。
"""

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import CustomPrompt


class PromptNotFound(LookupError):
    """按 id 寻址的提示词行不存在（管理接口映射 404）。"""


class PromptReadOnly(Exception):
    """用户自定义提示词行在后台只读（管理接口映射 409）：正文与默认都归
    用户自己管理，运营商不代改。"""


class SystemPromptMissing(LookupError):
    """库内没有该阶段可消费的系统提示词行（空库未注入种子，或默认行全部
    停用）。这是部署缺陷而非运行时可恢复状态：调用方（Worker 常驻循环）
    让它冒泡，ERROR 级显性记录后由僵尸巡检兜底该会话。"""


def user_default_content(session: Session, user_id: int, stage: str) -> str | None:
    """该用户在该阶段启用的自定义默认提示词正文；无则 None（回落系统行）。

    "同用户同阶段至多一个默认"由管理接口维护；读取端按 updated_at/id 最新
    优先，脏数据（多条 default，如旧库迁入）下选取仍是确定的。user_id 过滤
    天然排除系统行（系统行 user_id 为 NULL）。
    """
    return session.execute(
        select(CustomPrompt.content)
        .where(
            CustomPrompt.user_id == user_id,
            CustomPrompt.stage == stage,
            CustomPrompt.is_default.is_(True),
            CustomPrompt.is_active.is_(True),
        )
        .order_by(CustomPrompt.updated_at.desc(), CustomPrompt.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def system_content(session: Session, stage: str) -> str:
    """库内该阶段默认启用的系统提示词行正文（唯一事实源）。

    与用户默认查询同一防御式读法（updated_at/id 最新优先）。无默认启用行
    抛 :class:`SystemPromptMissing`：种子注入（app.main.lifespan / app.seeds）
    保证空库有行，走到这里即部署缺陷，响亮失败优于悄悄回落。
    """
    content = session.execute(
        select(CustomPrompt.content)
        .where(
            CustomPrompt.user_id.is_(None),
            CustomPrompt.is_system.is_(True),
            CustomPrompt.stage == stage,
            CustomPrompt.is_default.is_(True),
            CustomPrompt.is_active.is_(True),
        )
        .order_by(CustomPrompt.updated_at.desc(), CustomPrompt.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if content is None:
        raise SystemPromptMissing(f"库内缺少 stage={stage} 的默认启用系统提示词行")
    return content


def edit_system_content(session: Session, prompt_id: int, content: str) -> CustomPrompt:
    """编辑系统行正文（后台提示词编辑的唯一落库路径），返回更新后的行。

    只改正文，不触碰 name/stage/默认等归属字段；updated_at 同事务拨到本次
    编辑时刻——消费端按最新优先防御脏数据，管理列表也能看出最近改动。
    用户自定义行抛 :class:`PromptReadOnly`，不存在抛 :class:`PromptNotFound`。
    在调用方事务内写入（请求路径由 get_session 提交）。
    """
    row = _load(session, prompt_id)
    if not row.is_system:
        raise PromptReadOnly(f"提示词 {prompt_id} 是用户自定义行，后台只读")
    row.content = content
    row.updated_at = func.current_timestamp()
    session.flush()
    return row


def set_system_default(session: Session, prompt_id: int) -> CustomPrompt:
    """把系统行设为其阶段的默认提示词，并收口 (user_id=NULL, stage) 至多一个
    默认的不变量：同阶段其余默认行（含旧库迁入的脏数据）在同一事务内降级。

    重复调用幂等；SQLite 单写者下并发调用也串行进各自事务，不存在两个默认
    并存的提交。默认即消费语义：同时启用该行——"设为默认"却因停用而看不到
    效果是矛盾状态，在这里定义掉。用户自定义行抛 :class:`PromptReadOnly`。
    """
    row = _load(session, prompt_id)
    if not row.is_system:
        raise PromptReadOnly(f"提示词 {prompt_id} 是用户自定义行，后台只读")
    row.is_default = True
    row.is_active = True
    row.updated_at = func.current_timestamp()
    session.execute(
        update(CustomPrompt)
        .where(
            CustomPrompt.user_id.is_(None),
            CustomPrompt.stage == row.stage,
            CustomPrompt.is_default.is_(True),
            CustomPrompt.id != row.id,
        )
        .values(is_default=False, updated_at=func.current_timestamp())
    )
    session.flush()
    return row


def _load(session: Session, prompt_id: int) -> CustomPrompt:
    row = session.get(CustomPrompt, prompt_id)
    if row is None:
        raise PromptNotFound(f"提示词 {prompt_id} 不存在")
    return row
