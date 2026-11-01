"""僵尸恢复协议（issue #20，spec #15 用户故事 7/14）：崩溃会话的断点续跑与兜底退款。

崩溃语义的唯一拥有者。进程在任何时刻死亡（kill -9、断电、部署重启）后，
启动恢复器与巡检协程共同保证：会话要么从断点续跑至终态，要么全额退款置
failed——任何进程崩溃不得吞掉用户预付余额（rewrite-plan §6 M2 验收原话）。

协议（ADR-0003：任务表状态是唯一事实源，本模块不持任何内存状态）：

- 启动恢复（:func:`recover_interrupted`，lifespan 内、Worker 启动前调用一次）：
  扫描 running 会话——它们是上一进程死时停在中途的僵尸。attempt_count 先
  递增（GLOSSARY：预扣费的失败兜底），未超上限则条件 UPDATE 复位为 queued
  交还 Worker；Worker 的段落推进天然从首个未完成段落继续（app.worker 跳过
  completed 段），已完成段落不重算、不重复计费。queued 会话不碰：新 Worker
  本就按 created_at 拉取，重启期间新提交的任务由此不丢。
- 巡检（:func:`patrol_forever`，与 Worker 并存的常驻看门狗协程）：周期扫描
  running 且 updated_at 早于阈值的会话。updated_at 由流水线每次推进时拨新
  （app.worker._write_result），停滞即"进程活着但流水线卡死"的活僵尸，兜底
  退款置 failed。阈值默认约 2 小时（settings.recovery_stuck_after_seconds）。

兜底退款（:func:`_abandon`）：条件 UPDATE running→failed 赢得终态归属后，
同一短事务走 :func:`app.billing.refund` 全额退 charged_fen（金额取自会话行，
与原 charge 行必等额，#13 守卫恒通过）。:class:`billing.RefundConflict`
（竞争输掉退款，计费契约要求调用方整体放弃事务）在兜底路径不崩溃：事务
整体回滚、ERROR 级告警日志显性记录（编程错误不静默），会话留在 running
交由下一轮巡检重试——对方若确实退过款，重试时 refund 幂等返回 False，
failed 照常落定；资金在任何交错下最多退一次（账本部分唯一索引兜底）。
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from app import billing, optimization, stream
from app.config import settings
from app.db import short_tx
from app.models import OptimizationSession

logger = logging.getLogger(__name__)


class RecoveryReport(NamedTuple):
    """一次启动恢复的结果计数（lifespan 日志与测试断言之用）。"""

    resumed: int
    abandoned: int


def recover_interrupted(
    session_factory: sessionmaker, *, max_attempts: int | None = None
) -> RecoveryReport:
    """启动恢复：把上一进程遗留的 running 僵尸复位续跑或兜底退款。

    每个僵尸会话 attempt_count 先递增再比较（续跑前递增）：递增后超过
    max_attempts 即放弃续跑、兜底退款置 failed；否则条件 UPDATE 复位为
    queued（Worker 随后按 created_at 认领，从断点段落继续）。返回计数。
    """
    limit = settings.recovery_max_attempts if max_attempts is None else max_attempts
    with short_tx(session_factory) as session:
        zombies = session.execute(
            select(OptimizationSession.id, OptimizationSession.attempt_count).where(
                OptimizationSession.status == optimization.SESSION_RUNNING
            )
        ).all()

    resumed = abandoned = 0
    for session_id, attempt_count in zombies:
        attempts = attempt_count + 1
        if attempts > limit:
            if _abandon(
                session_factory,
                session_id,
                reason=f"僵尸恢复：续跑 {attempt_count} 次后仍中断，超过上限 {limit} 次，兜底退款",
            ):
                abandoned += 1
            continue
        with short_tx(session_factory) as session:
            hit = (
                session.execute(
                    update(OptimizationSession)
                    .where(
                        OptimizationSession.id == session_id,
                        OptimizationSession.status == optimization.SESSION_RUNNING,
                    )
                    .values(
                        status=optimization.SESSION_QUEUED,
                        attempt_count=attempts,
                        updated_at=func.current_timestamp(),
                    )
                ).rowcount
                == 1
            )
        if hit:
            resumed += 1
            stream.notify(session_id)  # 进度流感知复位（订阅者通常不存在，零开销）
        else:
            logger.warning("僵尸会话 %d 复位续跑失败：状态已被并发落定，跳过", session_id)
    logger.info("僵尸恢复完成：%d 个会话复位续跑，%d 个兜底退款", resumed, abandoned)
    return RecoveryReport(resumed=resumed, abandoned=abandoned)


async def patrol_forever(
    session_factory: sessionmaker,
    *,
    stuck_after_seconds: float | None = None,
    interval_seconds: float | None = None,
) -> None:
    """常驻看门狗：周期巡检滞留的 running 会话并兜底退款（见模块级协议）。

    看门狗不死：单轮巡检的任何异常记日志后下一轮继续；取消随 asyncio 惯例
    冒泡（lifespan 关停负责 gather）。
    """
    interval = (
        settings.recovery_patrol_interval_seconds
        if interval_seconds is None
        else interval_seconds
    )
    while True:
        await asyncio.sleep(interval)
        try:
            patrol_once(session_factory, stuck_after_seconds=stuck_after_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("僵尸巡检轮次失败：本轮跳过，下一轮继续")


def patrol_once(
    session_factory: sessionmaker, *, stuck_after_seconds: float | None = None
) -> int:
    """巡检一轮：兜底退款全部滞留超阈值的 running 会话，返回收置个数。

    滞留判定：running 且 updated_at 早于 now - stuck_after_seconds（流水线
    最后一次推进时刻，见模块级协议）。
    """
    threshold = (
        settings.recovery_stuck_after_seconds
        if stuck_after_seconds is None
        else stuck_after_seconds
    )
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=threshold)
    with short_tx(session_factory) as session:
        stuck = session.execute(
            select(OptimizationSession.id).where(
                OptimizationSession.status == optimization.SESSION_RUNNING,
                OptimizationSession.updated_at < cutoff,
            )
        ).scalars().all()
    abandoned = 0
    for session_id in stuck:
        if _abandon(
            session_factory,
            session_id,
            reason=f"巡检兜底：running 滞留超过 {int(threshold)} 秒无推进",
        ):
            abandoned += 1
    if abandoned:
        logger.warning("僵尸巡检：本轮兜底退款 %d 个滞留会话", abandoned)
    return abandoned


def _abandon(session_factory: sessionmaker, session_id: int, *, reason: str) -> bool:
    """兜底收置一个会话：running → failed + 全额退款，同一短事务。

    终态归属与退款走 :func:`optimization.finalize_with_refund`。返回 True
    表示本次完成收置；False 表示终态已被并发落定（对方已退款，本路径无权
    再动资金）或退款竞争输掉（RefundConflict，告警日志显性记录，事务整体
    回滚，会话留在 running 交由下一轮重试）。
    """
    try:
        with short_tx(session_factory) as session:
            owned = optimization.finalize_with_refund(
                session,
                session_id,
                terminal_status=optimization.SESSION_FAILED,
                from_statuses=(optimization.SESSION_RUNNING,),
                reason=reason,
            )
    except billing.RefundConflict as exc:
        # 计费契约：竞争输掉必须整体放弃事务（short_tx 已回滚）。兜底路径
        # 不因此崩溃——响亮记录后放弃，下一轮巡检重试（幂等退款守卫兜底）。
        logger.error(
            "兜底退款竞争输掉（RefundConflict）：会话 %d 留在 running 待重试，"
            "事务已回滚。这通常是编程错误，请排查并发退款来源：%s",
            session_id,
            exc,
        )
        return False
    if not owned:
        return False
    logger.warning("会话 %d 兜底退款：%s", session_id, reason)
    stream.notify(session_id)
    return True
