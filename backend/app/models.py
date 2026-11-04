"""ORM 模型：全部持久化状态的唯一 schema 定义，Alembic 迁移以此为 target。

字段的业务含义以 GLOSSARY.md 为准：
- balance_fen：余额（Balance），单位"分"，Workspace 按字数计费扣减（M2 接入）。
- usage_limit / usage_used：次数额度，Word Formatter 按次计费（M4 接入）；
  剩余次数 = usage_limit - usage_used。与余额相互独立（双轨计费）。

资金字段的全部变动必须走条件原子 UPDATE + rowcount 校验（见 app/db.py
短事务纪律），由 T4 计费服务统一收口，禁止绕过。
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, text
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
    - usage_after 仅次数轨变动（usage_reserve/usage_release/usage_adjust）
      非空，记录变动后的剩余次数。
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
    """调优提示词：系统预设（is_system=True）或用户自定义。

    方案 A（issue #23 裁决，issue #30 落地）：系统行的正文是系统提示词唯一
    事实源——管理后台编辑库内行（app.prompts 拥有写路径与解析回落链），改完
    即对后续会话生效；app/seed_data/system_prompts.json 降级为空库首次注入的
    种子（app.seeds 幂等注入，不覆盖已改动行），不参与运行时解析。用户自定义
    提示词由 T5 迁移自旧库整行搬入，后台只读。两类的区分只看 is_system，
    不靠 user_id 是否为空。
    """

    __tablename__ = "custom_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 系统预设无归属用户，可空
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 流水线阶段：'polish' / 'enhance'
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 同用户同阶段至多一个默认提示词；流水线每阶段的消费点在 app.worker
    # （_custom_default_prompt）。该不变量由提示词服务（app.prompts，M3/T6
    # 管理接口为其唯一写路径）维护，读取端按 updated_at/id 最新优先防御脏数据
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )


class OptimizationSession(Base):
    """优化会话（GLOSSARY：会话）：用户提交的一次完整润色任务。

    状态机（唯一事实源，ADR-0003）：queued → running → completed/failed，
    queued/running 可 cancelled。全部状态迁移由会话模块（app.optimization）
    与 Worker 拥有，其他模块只读。

    计费字段：
    - word_count：预扣费依据的计费字数（去空白字符数），创建时一次写入。
    - charged_fen：预扣金额（GLOSSARY：预扣费）。取消/失败的退款金额以
      本字段为准，与账本原 charge 行互为对账。

    model_snapshot 只读：记录当时实际使用的模型（运营商客诉排查用，
    spec #15 用户故事 12），由 Worker 认领会话时一次写入；queued 期为空。
    不设任何 *_api_key / *_base_url 列——模型接入只来自 .env（ADR-0005）。
    """

    __tablename__ = "optimization_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    # queued / running / completed / failed / cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True, nullable=False)
    # 处理模式（GLOSSARY）：paper_polish / paper_enhance / paper_polish_enhance
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False)
    charged_fen: Mapped[int] = mapped_column(Integer, nullable=False)
    model_snapshot: Mapped[str | None] = mapped_column(String(255))
    # Worker 续跑重试计数（僵尸恢复协议，spec #15；T2/T5 接入）
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )


class Segment(Base):
    """段落（GLOSSARY：段落）：流水线处理的最小文本单元。

    会话创建时按处理模式切分落库：polish 阶段行总是存在，enhance 阶段行
    仅在模式含增强时创建（阶段归属见 stage）。enhance 行的 source_text
    创建时先存原文占位，Worker 在润色完成后以其结果覆盖（增强在润色产物
    上进行）——状态机的事实源仍是 status，source_text 只是输入暂存。

    - status：pending / completed / failed（ADR-0003：按段断点续跑）。
    - (session_id, stage, seq) 唯一：同会话同阶段内序号即处理顺序。
    """

    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("optimization_sessions.id"), index=True, nullable=False
    )
    # 流水线阶段：'polish'（润色）/ 'enhance'（原创性增强）
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    result_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        Index("ux_segments_session_stage_seq", "session_id", "stage", "seq", unique=True),
    )


class SegmentChangeLog(Base):
    """变更记录（GLOSSARY）：某段落经润色或增强前后对照的审计条目，只写不改。

    与段落行的区分：Segment.source_text 是输入暂存（enhance 阶段会被
    Worker 以润色产物覆盖），本表是只增不改的审计事实——每段每阶段完成
    时由 Worker 写入一行 before/after，客诉与"每段改了什么"（spec #15
    用户故事 10）以此为准。寻址沿用 (session_id, stage, seq)，与段落行
    一一对应；before_text 即该次调用实际发给 LLM 的输入。
    """

    __tablename__ = "segment_change_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("optimization_sessions.id"), index=True, nullable=False
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    before_text: Mapped[str] = mapped_column(Text, nullable=False)
    after_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        Index("ux_segment_change_logs_session_stage_seq", "session_id", "stage", "seq", unique=True),
    )


class FormatJob(Base):
    """Word Formatter 任务（GLOSSARY：格式化任务）：格式化/预处理各一次提交一行。

    状态机（唯一事实源，ADR-0003）：queued → running → completed/failed；
    不设 cancelled（旧版取消从未有路由暴露）。全部状态迁移由任务模块
    （app.word_jobs）与任务 Worker（app.word_worker）拥有，其他模块只读。

    计费字段：创建即经计费服务 reserve_usage（task_id=本任务 id，账本
    usage_reserve 行）；失败路径由任务模块统一 release_usage。行内不存任何
    金额——次数轨变动只看账本。

    产物列（M4 落盘纪律，spec #31）：docx 写数据目录（settings.word_output_dir
    下按任务 id 命名），DB 只存文件名；下载时由 app.word_jobs 解析出完整路径
    （数据目录迁移不改库）。output_filename 是交付文件名（保留原文件名加后缀）。

    进度三列（phase/progress/message）由 Worker 在短事务内推进，是进度流
    （issue #34）的现读现推事实源；任务整体无内部断点，进度只是展示态。
    """

    __tablename__ = "format_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    # format（排版编译）/ preprocess（AI 分块标记，M4-T4 接入）
    job_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # queued / running / completed / failed
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True, nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    input_file_name: Mapped[str | None] = mapped_column(String(255))
    # 编译/预处理配置 JSON（input_format/spec_name/custom_spec/include_cover 等），
    # 结构由消费方（app.word_worker / M4-T4 预处理）拥有，本表只存取不解释
    options: Mapped[dict | None] = mapped_column(JSON)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 进度三列：阶段名、0.0-1.0 进度、人读消息
    phase: Mapped[str | None] = mapped_column(String(32))
    progress: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str | None] = mapped_column(String(255))
    # 分块进度两列：预处理任务的"第几块/共几块"（AI 分块标记推进期写入，
    # 进度流的分块序号事实源）；格式化任务恒空
    chunk_index: Mapped[int | None] = mapped_column(Integer)
    chunk_total: Mapped[int | None] = mapped_column(Integer)
    output_path: Mapped[str | None] = mapped_column(String(255))
    output_filename: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
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
