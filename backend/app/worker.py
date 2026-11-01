"""润色流水线 Worker：单 asyncio 协程按 created_at 拉取 queued 会话（ADR-0003）。

任务表状态是唯一事实源：本模块不持任何内存队列，拉取即条件 UPDATE 认领
（queued → running，输给并发取消则跳过），进度全部落库。段落推进遵循
app.db 短事务纪律——**LLM 等待期间零写事务**，每段按"短事务读输入 →
提交 → await LLM → 短事务写结果"循环；流水线测试经假 LLM 钩子内断言
守住这条纪律。

阶段推进（GLOSSARY：处理模式/段落）：按 :func:`optimization.stages_for_mode`
的阶段序逐段调用 LLM；润色段完成后同事务把结果覆盖到同序号增强段的输入
暂存（增强在润色产物上进行，app.models.Segment 契约），并写一行变更记录
（GLOSSARY：变更记录，段落级 before/after）。

提示词解析（每阶段一次，非每段落）：该用户在该阶段启用的自定义默认提示词
（custom_prompts 的 default 行，见 :meth:`_custom_default_prompt`）优先，
无则回落系统种子（:func:`_system_prompt_for`）。断点续跑重入 :meth:`_process`
时重新解析——用户中途改默认提示词会影响续跑段落，属可接受语义。

失败语义：
- LLM 调用抛 :class:`app.llm.LLMError`（内容性错误，或瞬时错误重试耗尽）：
  终态归属与全额退款走 :func:`optimization.finalize_with_refund`（条件
  UPDATE 置会话 failed，同事务退 charged_fen；幂等与金额守卫由计费服务
  拥有）。条件 UPDATE 输给并发取消（对方已置 cancelled 并退款）时不再退。
- :class:`billing.RefundConflict` 与其余一切异常（编程错误、数据库故障）：
  单轮响亮失败——ERROR 级日志带栈显性记录（不吞），中断会话留在 running，
  其兜底退款与续跑语义由僵尸恢复协议唯一拥有（app.recovery 巡检）；常驻
  循环本身不死（与 :func:`app.recovery.patrol_forever` 同一惯例），后续
  queued 会话照常拉取，不因单轮故障永久停摆。

LLM 接缝：Worker 只依赖 :class:`SegmentPolisher`（一个 ``polish`` 方法 +
``model_name``），生产接 :class:`app.llm.PolishLLMClient`，测试接脚本化假
客户端——流水线测试不 mock 内部函数。

进度可见：每个状态变更短事务提交后经 :func:`app.stream.notify` 叫醒该会话
的 SSE 进度流（通知不携带数据，流被唤醒后重读数据库推导事件——通知丢了
也不丢进度，见 app.stream）。
"""

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import optimization, seeds, stream
from app.db import short_tx
from app.llm import LLMError
from app.models import CustomPrompt, OptimizationSession, Segment, SegmentChangeLog

logger = logging.getLogger(__name__)


class SegmentPolisher(Protocol):
    """Worker 对 LLM 的全部依赖：单段落润色 + 模型名（写只读 model_snapshot）。"""

    model_name: str

    async def polish(self, text: str, *, system_prompt: str) -> str: ...


@functools.lru_cache(maxsize=1)
def _system_prompt_for(stage: str) -> str:
    """系统级回落提示词：取系统种子（代码固化的运营商调优资产）中该阶段
    的默认启用条目。仅当该用户在该阶段无启用的自定义默认提示词时使用
    （见 _process 与 _custom_default_prompt）；构造参数 prompt_for 可整体
    替换本回落解析（测试接缝）。"""
    for item in seeds.load_system_prompts():
        if item["stage"] == stage and item.get("is_default", True) and item.get("is_active", True):
            return item["content"]
    raise ValueError(f"系统种子缺少 stage={stage} 的默认提示词")


class PipelineWorker:
    """单 Worker 流水线。调用方只需要 :meth:`run_forever`（常驻）与
    :meth:`process_next`（处理一个 queued 会话，无则 False，供测试驱动）。
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        llm: SegmentPolisher,
        *,
        poll_interval_seconds: float = 1.0,
        prompt_for: Callable[[str], str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._poll_interval = poll_interval_seconds
        self._prompt_for = prompt_for or _system_prompt_for

    async def run_forever(self) -> None:
        """常驻循环：拉到会话立刻处理下一个，空转按间隔轮询数据库。

        看门狗不死（与 :func:`app.recovery.patrol_forever` 同一惯例）：单轮
        处理的任何异常 ERROR 级记日志后按轮询间隔退避、继续下一轮；中断的
        会话留在 running，兜底退款归僵尸巡检协议（app.recovery）。取消随
        asyncio 惯例冒泡（lifespan 关停负责 gather）。
        """
        while True:
            try:
                if not await self.process_next():
                    await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Worker 单轮处理失败：中断会话留在 running 由僵尸巡检兜底，"
                    "退避后继续服务队列"
                )
                await asyncio.sleep(self._poll_interval)

    async def process_next(self) -> bool:
        """认领并处理一个 queued 会话；无排队会话返回 False。"""
        claimed = self._claim()
        if claimed is None:
            return False
        session_id, mode, user_id = claimed
        await self._process(session_id, user_id, mode)
        return True

    # ---- 状态机：认领 / 推进 / 完成 / 失败退款（全部条件 UPDATE 守卫）。
    # ---- 每个单元一个 app.db.short_tx 短事务（与 get_session 同一协议） ----

    def _claim(self) -> tuple[int, str, int] | None:
        """认领最老的 queued 会话（queued → running + model_snapshot）。"""
        with short_tx(self._session_factory) as session:
            row = session.execute(
                select(OptimizationSession)
                .where(OptimizationSession.status == optimization.SESSION_QUEUED)
                .order_by(OptimizationSession.created_at, OptimizationSession.id)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            claimed = (
                session.execute(
                    update(OptimizationSession)
                    .where(
                        OptimizationSession.id == row.id,
                        OptimizationSession.status == optimization.SESSION_QUEUED,
                    )
                    .values(
                        status=optimization.SESSION_RUNNING,
                        model_snapshot=self._llm.model_name,
                        updated_at=func.current_timestamp(),
                    )
                ).rowcount
                == 1
            )
            # 守卫失败 = 并发取消抢先置 cancelled：本 Worker 不碰它
            if not claimed:
                return None
        # 短事务已提交才通知进度流（app.stream）：唤醒后重读库，通知丢了也不丢状态
        stream.notify(row.id)
        return (row.id, row.mode, row.user_id)

    def _custom_default_prompt(self, user_id: int, stage: str) -> str | None:
        """该用户在该阶段启用的自定义默认提示词正文；无则 None（只读短事务）。

        "同用户同阶段至多一个默认"的不变量由将来的提示词管理接口维护；读取
        端按 updated_at/id 最新优先，脏数据（多条 default）下选取仍是确定的。
        user_id 过滤天然排除 is_system 行（系统行 user_id 为 NULL）。
        """
        with short_tx(self._session_factory) as session:
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

    async def _process(self, session_id: int, user_id: int, mode: str) -> None:
        stages = optimization.stages_for_mode(mode)
        for stage in stages:
            # 提示词每阶段解析一次（一次只读短事务，在首个 await 前提交关闭）：
            # 用户自定义默认优先，无则回落系统级解析
            system_prompt = self._custom_default_prompt(user_id, stage) or self._prompt_for(stage)
            for seq in self._segment_seqs(session_id, stage):
                if not self._session_active(session_id):
                    return  # 会话被并发取消：停止烧 LLM，终态已归取消方
                source = self._pending_source(session_id, stage, seq)
                if source is None:
                    continue  # 已完成段落：断点续跑不重算（T5 恢复协议的落点）
                try:
                    result = await self._llm.polish(source, system_prompt=system_prompt)
                except LLMError as exc:
                    self._fail(session_id, exc, stage=stage, seq=seq)
                    return
                self._write_result(session_id, stage, seq, source, result)
        self._complete(session_id)

    def _segment_seqs(self, session_id: int, stage: str) -> list[int]:
        with short_tx(self._session_factory) as session:
            return list(
                session.execute(
                    select(Segment.seq)
                    .where(Segment.session_id == session_id, Segment.stage == stage)
                    .order_by(Segment.seq)
                ).scalars()
            )

    def _session_active(self, session_id: int) -> bool:
        """会话仍在 running（只读短事务，LLM 调用前的取消检查点）。"""
        with short_tx(self._session_factory) as session:
            status: str | None = session.execute(
                select(OptimizationSession.status).where(OptimizationSession.id == session_id)
            ).scalar_one_or_none()
            return status == optimization.SESSION_RUNNING

    def _pending_source(self, session_id: int, stage: str, seq: int) -> str | None:
        """待处理段落的输入；已完成返回 None（断点续跑不重算）。

        enhance 段的输入是润色产物：由润色完成事务覆盖（见 _write_result），
        同事务保证两者要么都发生要么都不发生，无需运行时修补。
        """
        with short_tx(self._session_factory) as session:
            source: str | None = session.execute(
                select(Segment.source_text).where(
                    Segment.session_id == session_id,
                    Segment.stage == stage,
                    Segment.seq == seq,
                    Segment.status == optimization.SEGMENT_PENDING,
                )
            ).scalar_one_or_none()
            return source

    def _write_result(
        self, session_id: int, stage: str, seq: int, source: str, result: str
    ) -> None:
        """段落结果 + 变更记录 + 增强段输入覆盖，一个短事务原子落库。

        同事务把会话行 updated_at 拨到本次推进时刻：它是"流水线最后一次
        对该会话有写动作"的事实源，T5 巡检据此判定滞留（app.recovery）。
        """
        with short_tx(self._session_factory) as session:
            session.execute(
                update(Segment)
                .where(
                    Segment.session_id == session_id,
                    Segment.stage == stage,
                    Segment.seq == seq,
                )
                .values(
                    result_text=result,
                    status=optimization.SEGMENT_COMPLETED,
                    updated_at=func.current_timestamp(),
                )
            )
            session.execute(
                update(OptimizationSession)
                .where(OptimizationSession.id == session_id)
                .values(updated_at=func.current_timestamp())
            )
            if stage == optimization.STAGE_POLISH:
                session.execute(
                    update(Segment)
                    .where(
                        Segment.session_id == session_id,
                        Segment.stage == optimization.STAGE_ENHANCE,
                        Segment.seq == seq,
                    )
                    .values(source_text=result, updated_at=func.current_timestamp())
                )
            session.add(
                SegmentChangeLog(
                    session_id=session_id,
                    stage=stage,
                    seq=seq,
                    before_text=source,
                    after_text=result,
                )
            )
        stream.notify(session_id)

    def _complete(self, session_id: int) -> None:
        with short_tx(self._session_factory) as session:
            session.execute(
                update(OptimizationSession)
                .where(
                    OptimizationSession.id == session_id,
                    OptimizationSession.status == optimization.SESSION_RUNNING,
                )
                .values(
                    status=optimization.SESSION_COMPLETED, updated_at=func.current_timestamp()
                )
            )
        stream.notify(session_id)

    def _fail(self, session_id: int, error: LLMError, *, stage: str, seq: int) -> None:
        """任务失败收场：running → failed + 自动退款，同一短事务。

        终态归属与退款走 :func:`optimization.finalize_with_refund`；失败段
        标记发生在归属赢定之后、同一短事务提交之前，原子性不变。
        RefundConflict 按计费契约原样冒泡（编程错误显性化，不吞不绕）。
        """
        with short_tx(self._session_factory) as session:
            owned = optimization.finalize_with_refund(
                session,
                session_id,
                terminal_status=optimization.SESSION_FAILED,
                from_statuses=(optimization.SESSION_RUNNING,),
                reason=f"任务失败自动退款：{error}",
            )
            if not owned:
                return  # 并发取消抢先：对方已退款，本路径无权再动资金
            session.execute(
                update(Segment)
                .where(
                    Segment.session_id == session_id,
                    Segment.stage == stage,
                    Segment.seq == seq,
                )
                .values(status=optimization.SEGMENT_FAILED, updated_at=func.current_timestamp())
            )
        stream.notify(session_id)
