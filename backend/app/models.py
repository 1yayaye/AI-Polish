"""ORM 模型：全部持久化状态的唯一 schema 定义，Alembic 迁移以此为 target。

字段的业务含义以 GLOSSARY.md 为准：
- balance_fen：余额（Balance），单位"分"，Workspace 按字数计费扣减（M2 接入）。
- usage_limit / usage_used：次数额度，Word Formatter 按次计费（M4 接入）；
  剩余次数 = usage_limit - usage_used。与余额相互独立（双轨计费）。

资金字段的全部变动必须走条件原子 UPDATE + rowcount 校验（见 app/db.py
短事务纪律），由 T4 计费服务统一收口，禁止绕过。
"""

from sqlalchemy import Integer, String
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
