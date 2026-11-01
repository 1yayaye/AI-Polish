"""ORM 模型：全部持久化状态的唯一 schema 定义，Alembic 迁移以此为 target。

字段的业务含义以 GLOSSARY.md 为准：
- balance_fen：余额（Balance），单位"分"，Workspace 按字数计费扣减（M2 接入）。
- usage_limit / usage_used：次数额度，Word Formatter 按次计费（M4 接入）；
  剩余次数 = usage_limit - usage_used。与余额相互独立（双轨计费）。

资金字段的全部变动必须走条件原子 UPDATE + rowcount 校验（见 app/db.py
短事务纪律），由 T4 计费服务统一收口，禁止绕过。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    """卡密用户：卡密即全部身份凭证，无密码、无账号体系。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_key: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    balance_fen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usage_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usage_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    @property
    def usage_remaining(self) -> int:
        return self.usage_limit - self.usage_used


def mask_card_key(card_key: str) -> str:
    """卡密脱敏展示：保留首尾各 4 位（短卡密只留首 2 位），中段以 **** 遮盖。

    卡密的展示规则随卡密概念放在这里，供管理端用户列表与迁移对账报告共用；
    完整卡密只在发卡时交付一次。
    """
    if len(card_key) <= 8:
        return card_key[:2] + "****"
    return card_key[:4] + "****" + card_key[-4:]


class LedgerEntry(Base):
    """账本：余额与次数全部变动的流水（GLOSSARY：账本），每笔变动一行。

    - amount_fen 为带符号增量（扣减为负、充值/退款为正），因此
      SUM(amount_fen) == 该用户当前余额 恒成立——对账与迁移硬校验
      （spec #3 用户故事 10）都依赖这一条；次数轨变动 amount_fen 为 0。
    - usage_after 仅次数轨变动（usage_reserve/usage_release）非空，
      记录变动后的剩余次数。
    - task_id 无外键且可空：账本行要先于会话表存在，旧账本迁入不因缺
      旧会话而断裂（rewrite-plan §5）；新流水一律携带 task_id。
    - kind/amount/balance_after 的合法取值由 app.billing（计费服务）唯一
      拥有；其他模块只读，禁止直接写本表。
    """

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    task_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_fen: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after_fen: Mapped[int] = mapped_column(Integer, nullable=False)
    usage_after: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        # 退款状态守卫：每个 task 至多一条 refund 行，重复退款被数据库拒绝。
        # billing.refund 的幂等语义依赖此约束（含并发竞争的最终防线）。
        Index(
            "ux_ledger_refund_once_per_task",
            "task_id",
            unique=True,
            sqlite_where=text("kind = 'refund' AND task_id IS NOT NULL"),
        ),
    )


class CustomPrompt(Base):
    """调优提示词：系统预设（is_system=True，内容由代码种子注入）或用户自定义。

    系统预设的正文唯一来源是 app/seed_data/system_prompts.json（运营商调优
    资产固化进代码，重建空库经 app.seeds 注入）；用户自定义提示词由 T5 迁移
    自旧库整行搬入。两类的区分只看 is_system，不靠 user_id 是否为空。
    """

    __tablename__ = "custom_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 系统预设无归属用户，可空
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 流水线阶段：'polish' / 'enhance'
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 同用户同阶段可有一个默认提示词（M2 接入流水线时消费）
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )


class SavedSpec(Base):
    """用户保存的排版规格（GLOSSARY：排版规格），Word Formatter 复用（M4 接入）。

    spec_json 是排版规格的结构化 JSON 全文，格式由 Word Formatter 拥有，
    本表只存取不解释。经 T5 迁移自旧库整行搬入。
    """

    __tablename__ = "saved_specs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
