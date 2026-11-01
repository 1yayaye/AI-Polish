"""Word Formatter 任务 Worker：单 asyncio 协程按 created_at 拉取 queued 任务（spec #31）。

与润色 :class:`app.worker.PipelineWorker` 同一形态：任务表状态是唯一事实源
（ADR-0003），本模块不持任何内存队列，拉取即条件 UPDATE 认领（queued →
running，输给并发落定则跳过），进度全部落库、短事务推进——**编译等外部
等待期间零数据库事务**（app.db 短事务纪律：先短事务读出输入，提交后再编译，
结果回来另起短事务写回）。

编译约定（与 app.export 同一纪律，rewrite-plan §5 的 512MB 内存高危点）：

- 同步编译经 ``asyncio.to_thread`` 抛出事件循环；
- 进程级 ``Semaphore(1)`` 限制同时只编译一个文档；
- 编译产物直接写数据目录（按任务 id 命名），DB 只存文件名——字节流只在
  编译线程内瞬时驻留，不落库不驻留事件循环。

失败语义：

- 编译与落盘的一切失败（渲染库返回 success=False 的内容性失败、编译线程
  抛出的编程错误、写盘故障）：一律对象化收场——ERROR 级日志带栈显性记录
  （不吞），任务走 :func:`app.word_jobs.fail_job_with_release`（条件 UPDATE
  置 failed，同一短事务释放预留次数；条件 UPDATE 输给并发落定则不动资金）。
  任务失败不影响服务可用性，预留次数不扣住。
- 其余一切异常（认领/收场等数据库路径的编程错误、数据库故障）：单轮响亮
  失败——ERROR 级日志带栈显性记录，任务留在 running，复位续跑语义由启动
  恢复协议唯一拥有（app.word_jobs.recover_interrupted）；常驻循环本身不死
  （与润色 Worker 同一惯例），后续 queued 任务照常拉取，不因单轮故障永久
  停摆。

编译接缝：Worker 只依赖一个 ``compile(text, options) -> CompileResult`` 的
同步可调用（生产接 :func:`app.word_formatter.compile_document`，测试接脚本
化假编译器——不 mock 内部函数）。
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import word_jobs
from app.db import short_tx
from app.models import FormatJob
from app.word_formatter import (
    CompileOptions,
    CompileResult,
    InputFormat,
    StyleSpec,
    compile_document,
)

logger = logging.getLogger(__name__)

# 同时只编译一个 docx（与 app.export 同一约定）；进程级常量，进程内全局生效。
_COMPILE_SEMAPHORE = asyncio.Semaphore(1)


class FormatCompiler(Protocol):
    """Worker 对渲染库的全部依赖：同步编译一个文档（秒级阻塞调用）。"""

    def __call__(self, text: str, options: CompileOptions) -> CompileResult: ...


def compile_options_for(options: dict | None) -> CompileOptions:
    """format_jobs.options JSON → 渲染库 CompileOptions（键名即字段名，见
    app.word_jobs._format_options；custom_spec 在创建时已过校验，重编译期
    的解析失败按内容性失败处理）。"""
    options = options or {}
    return CompileOptions(
        input_format=InputFormat(options.get("input_format", "auto")),
        spec_name=options.get("spec_name"),
        custom_spec=StyleSpec.model_validate(options["custom_spec"])
        if options.get("custom_spec")
        else None,
        include_cover=options.get("include_cover", True),
        include_toc=options.get("include_toc", True),
        toc_title=options.get("toc_title", "目 录"),
    )


class WordJobWorker:
    """单 Worker 任务队列。调用方只需要 :meth:`run_forever`（常驻）与
    :meth:`process_next`（处理一个 queued 任务，无则 False，供测试驱动）。
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        compiler: FormatCompiler | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._compiler: FormatCompiler = compiler or compile_document
        self._poll_interval = poll_interval_seconds

    async def run_forever(self) -> None:
        """常驻循环：拉到任务立刻处理下一个，空转按间隔轮询数据库。

        看门狗不死：单轮处理的任何异常 ERROR 级记日志后按轮询间隔退避、
        继续下一轮；中断的任务留在 running，复位续跑归启动恢复协议
        （app.word_jobs.recover_interrupted）。取消随 asyncio 惯例冒泡
        （lifespan 关停负责 gather）。
        """
        while True:
            try:
                if not await self.process_next():
                    await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "任务 Worker 单轮处理失败：中断任务留在 running 由启动恢复兜底，"
                    "退避后继续服务队列"
                )
                await asyncio.sleep(self._poll_interval)

    async def process_next(self) -> bool:
        """认领并处理一个 queued 任务；无排队任务返回 False。"""
        claimed = self._claim()
        if claimed is None:
            return False
        await self._process(claimed)
        return True

    # ---- 状态机：认领 / 推进 / 完成 / 失败释放（全部短事务 + 条件 UPDATE） ----

    def _claim(self) -> int | None:
        """认领最老的 queued 任务（queued → running）；无或竞争输掉返回 None。"""
        with short_tx(self._session_factory) as session:
            row = session.execute(
                select(FormatJob)
                .where(FormatJob.status == word_jobs.JOB_QUEUED)
                .order_by(FormatJob.created_at, FormatJob.id)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            claimed = (
                session.execute(
                    update(FormatJob)
                    .where(
                        FormatJob.id == row.id,
                        FormatJob.status == word_jobs.JOB_QUEUED,
                    )
                    .values(
                        status=word_jobs.JOB_RUNNING,
                        phase=word_jobs.PHASE_COMPILE,
                        progress=0.0,
                        message="开始处理",
                        updated_at=func.current_timestamp(),
                    )
                ).rowcount
                == 1
            )
            # 守卫失败 = 并发落定抢先：本 Worker 不碰它
            return row.id if claimed else None

    def _job_input(self, job_id: int) -> tuple[str, dict | None, str | None]:
        """编译输入（文本、options、原文件名）：只读短事务，编译前提交关闭。"""
        with short_tx(self._session_factory) as session:
            job = session.get(FormatJob, job_id)
            return job.input_text, job.options, job.input_file_name

    def _write_progress(self, job_id: int, *, progress: float, message: str) -> None:
        """进度三列短事务推进（展示态，不参与状态机）。"""
        with short_tx(self._session_factory) as session:
            session.execute(
                update(FormatJob)
                .where(FormatJob.id == job_id)
                .values(
                    progress=progress,
                    message=message,
                    updated_at=func.current_timestamp(),
                )
            )

    async def _process(self, job_id: int) -> None:
        """单任务全流程：读输入 →（零事务）单并发编译+落盘 → 终态短事务。"""
        text, options, input_file_name = self._job_input(job_id)
        self._write_progress(job_id, progress=0.1, message="正在编译文档")
        # 编译 + 写盘都在工作线程完成（Semaphore(1) 排队）：事件循环零阻塞，
        # 期间不持有任何数据库事务（输入已先读出）
        async with _COMPILE_SEMAPHORE:
            result = await asyncio.to_thread(self._compile_to_disk, job_id, text, options)
        if not result.success:
            self._fail(
                job_id,
                f"文档编译失败：{result.error or '未知原因'}",
            )
            return
        self._complete(job_id, word_jobs.output_filename_for(job_id, input_file_name))

    def _compile_to_disk(
        self, job_id: int, text: str, options: dict | None
    ) -> CompileResult:
        """工作线程内：同步编译并把产物写数据目录（DB 只存文件名，见
        app.word_jobs.artifact_path）。编译/写盘的任何异常都在此对象化为
        失败结果（ERROR 级显性记录，不让异常逃出线程）——调用方按内容性
        失败统一走 failed + 释放收场，见模块级"失败语义"。"""
        output_path = f"{job_id}.docx"
        try:
            result = self._compiler(text, compile_options_for(options))
            if not result.success or not result.docx_bytes:
                return result
            target = word_jobs.artifact_path(output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(result.docx_bytes)
            return result
        except Exception as exc:
            logger.exception("任务 %d 编译/落盘失败", job_id)
            return CompileResult(success=False, error=str(exc))

    def _complete(self, job_id: int, output_filename: str) -> None:
        """完成收场：running → completed + 产物文件名与交付名，同一短事务。"""
        with short_tx(self._session_factory) as session:
            session.execute(
                update(FormatJob)
                .where(
                    FormatJob.id == job_id,
                    FormatJob.status == word_jobs.JOB_RUNNING,
                )
                .values(
                    status=word_jobs.JOB_COMPLETED,
                    output_path=f"{job_id}.docx",
                    output_filename=output_filename,
                    phase=word_jobs.PHASE_DONE,
                    progress=1.0,
                    message="编译完成",
                    updated_at=func.current_timestamp(),
                )
            )

    def _fail(self, job_id: int, reason: str) -> None:
        """失败收场：running → failed + 释放预留次数，同一短事务。

        终态归属与释放走 :func:`app.word_jobs.fail_job_with_release`；归属
        输掉（对方已落终态并释放）时本路径无权再动资金。"""
        with short_tx(self._session_factory) as session:
            word_jobs.fail_job_with_release(
                session,
                job_id,
                from_statuses=(word_jobs.JOB_RUNNING,),
                reason=reason,
            )
