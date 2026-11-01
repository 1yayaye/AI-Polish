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

失败语义：
- LLM 调用抛 :class:`app.llm.LLMError`（内容性错误，或瞬时错误重试耗尽）：
  条件 UPDATE 置会话 failed，同事务走 :func:`billing.refund` 自动退款
  （金额取 charged_fen；幂等与金额守卫由计费服务拥有）。条件 UPDATE 输给
  并发取消（对方已置 cancelled 并退款）时不再退。
- :class:`billing.RefundConflict` 与其余一切异常（编程错误、数据库故障）
  直接冒泡终止 Worker：响亮失败优于吞掉，崩溃后会话停在中途状态、预扣
  资金原样在账，由 T5 僵尸恢复协议接管（本模块不实现恢复）。

LLM 接缝：Worker 只依赖 :class:`SegmentPolisher`（一个 ``polish`` 方法 +
``model_name``），生产接 :class:`app.llm.PolishLLMClient`，测试接脚本化假
客户端——流水线测试不 mock 内部函数。
"""

import asyncio
import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import billing, optimization, seeds
from app.llm import LLMError
from app.models import OptimizationSession, Segment, SegmentChangeLog


class SegmentPolisher(Protocol):
    """Worker 对 LLM 的全部依赖：单段落润色 + 模型名（写只读 model_snapshot）。"""

    model_name: str

    async def polish(self, text: str, *, system_prompt: str) -> str: ...


@functools.lru_cache(maxsize=1)
def _system_prompt_for(stage: str) -> str:
    """默认阶段提示词：取系统种子（代码固化的运营商调优资产）中该阶段的
    默认启用条目。用户自定义默认提示词的消费归 T3 编排，经构造参数替换。"""
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
        """常驻循环：拉到会话立刻处理下一个，空转按间隔轮询数据库。"""
        while True:
            if not await self.process_next():
                await asyncio.sleep(self._poll_interval)

    async def process_next(self) -> bool:
        """认领并处理一个 queued 会话；无排队会话返回 False。"""
        claimed = self._claim()
        if claimed is None:
            return False
        session_id, mode = claimed
        await self._process(session_id, mode)
        return True

    # ---- 短事务单元：与 app.db.get_session 同一提交/回滚协议 ----

    @contextmanager
    def _short_tx(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---- 状态机：认领 / 推进 / 完成 / 失败退款（全部条件 UPDATE 守卫） ----

    def _claim(self) -> tuple[int, str] | None:
        """认领最老的 queued 会话（queued → running + model_snapshot）。"""
        with self._short_tx() as session:
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
            return (row.id, row.mode) if claimed else None

    async def _process(self, session_id: int, mode: str) -> None:
        stages = optimization.stages_for_mode(mode)
        for stage in stages:
            for seq in self._segment_seqs(session_id, stage):
                if not self._session_active(session_id):
                    return  # 会话被并发取消：停止烧 LLM，终态已归取消方
                source = self._pending_source(session_id, stage, seq)
                if source is None:
                    continue  # 已完成段落：断点续跑不重算（T5 恢复协议的落点）
                try:
                    result = await self._llm.polish(
                        source, system_prompt=self._prompt_for(stage)
                    )
                except LLMError as exc:
                    self._fail(session_id, exc, stage=stage, seq=seq)
                    return
                self._write_result(session_id, stage, seq, source, result)
        self._complete(session_id)

    def _segment_seqs(self, session_id: int, stage: str) -> list[int]:
        with self._short_tx() as session:
            return list(
                session.execute(
                    select(Segment.seq)
                    .where(Segment.session_id == session_id, Segment.stage == stage)
                    .order_by(Segment.seq)
                ).scalars()
            )

    def _session_active(self, session_id: int) -> bool:
        """会话仍在 running（只读短事务，LLM 调用前的取消检查点）。"""
        with self._short_tx() as session:
            status: str | None = session.execute(
                select(OptimizationSession.status).where(OptimizationSession.id == session_id)
            ).scalar_one_or_none()
            return status == optimization.SESSION_RUNNING

    def _pending_source(self, session_id: int, stage: str, seq: int) -> str | None:
        """待处理段落的输入；已完成返回 None（断点续跑不重算）。

        enhance 段的输入是润色产物：由润色完成事务覆盖（见 _write_result），
        同事务保证两者要么都发生要么都不发生，无需运行时修补。
        """
        with self._short_tx() as session:
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
        """段落结果 + 变更记录 + 增强段输入覆盖，一个短事务原子落库。"""
        with self._short_tx() as session:
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

    def _complete(self, session_id: int) -> None:
        with self._short_tx() as session:
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

    def _fail(self, session_id: int, error: LLMError, *, stage: str, seq: int) -> None:
        """任务失败收场：running → failed + 自动退款，同一短事务。

        RefundConflict 按计费契约原样冒泡（编程错误显性化，不吞不绕）。
        """
        with self._short_tx() as session:
            failed = (
                session.execute(
                    update(OptimizationSession)
                    .where(
                        OptimizationSession.id == session_id,
                        OptimizationSession.status == optimization.SESSION_RUNNING,
                    )
                    .values(
                        status=optimization.SESSION_FAILED, updated_at=func.current_timestamp()
                    )
                ).rowcount
                == 1
            )
            if not failed:
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
            user_id, charged_fen = session.execute(
                select(OptimizationSession.user_id, OptimizationSession.charged_fen).where(
                    OptimizationSession.id == session_id
                )
            ).one()
            billing.refund(
                session,
                user_id,
                charged_fen,
                task_id=session_id,
                reason=f"任务失败自动退款：{error}",
            )
