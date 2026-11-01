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

预处理任务（job_type=preprocess，issue #35）共享同一状态机/计费/进度通知，
"把活干完"委托给 :func:`app.word_preprocess.execute_preprocess_job`（AI 分块
标记是 async 调用，LLM 等待期间零数据库事务）；LLM 接缝经 ``marking_ai``
注入（生产按 .env 惰性构造，测试接脚本化假客户端）。
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import word_jobs, word_preprocess, word_stream
from app.db import short_tx
from app.models import FormatJob
from app.word_formatter import (
    CompileOptions,
    CompileResult,
    InputFormat,
    StyleSpec,
    compile_document,
)
from app.word_preprocess import MarkingAI

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


def _claim_phase_for(job_type: str) -> str:
    """认领时的初始阶段：format=编译、preprocess=分段（后续由各自的推进
    逻辑覆写为细阶段）。"""
    return (
        word_jobs.PHASE_COMPILE
        if job_type == word_jobs.JOB_FORMAT
        else word_preprocess.PHASE_SPLITTING
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
        marking_ai: MarkingAI | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._compiler: FormatCompiler = compiler or compile_document
        # 预处理的 LLM 接缝：注入优先；未注入时按 .env 惰性构造并缓存
        # （构造期不解析——模型未配置不影响格式化任务的 Worker 挂载）。
        self._marking_ai: MarkingAI | None = marking_ai
        self._poll_interval = poll_interval_seconds

    def _marking_client(self) -> MarkingAI:
        """预处理任务的 LLM 客户端：测试注入优先，生产接
        :class:`app.llm.PolishLLMClient`（首次使用时按 .env 构造，未配置抛
        ValueError——调用方对象化为任务失败，不影响格式化任务）。"""
        if self._marking_ai is None:
            from app.llm import PolishLLMClient

            self._marking_ai = PolishLLMClient.from_settings()
        return self._marking_ai

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
                        phase=_claim_phase_for(row.job_type),
                        progress=0.0,
                        message="开始处理",
                        updated_at=func.current_timestamp(),
                    )
                ).rowcount
                == 1
            )
            job_id = row.id
        # 守卫失败 = 并发落定抢先：本 Worker 不碰它。短事务已提交才通知
        # 进度流（app.word_stream）：唤醒后重读库，通知丢了也不丢状态
        if claimed:
            word_stream.notify(job_id)
        return job_id if claimed else None

    def _job_input(self, job_id: int) -> tuple[str, str, dict | None, str | None]:
        """任务输入（类型、文本、options、原文件名）：只读短事务，开工前提交关闭。"""
        with short_tx(self._session_factory) as session:
            job = session.get(FormatJob, job_id)
            return job.job_type, job.input_text, job.options, job.input_file_name

    def _write_progress(self, job_id: int, *, progress: float, message: str) -> None:
        """进度三列短事务推进（展示态，不参与状态机），提交后叫醒进度流。"""
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
        word_stream.notify(job_id)

    async def _process(self, job_id: int) -> None:
        """单任务全流程：读输入 →（零事务）按类型开工 → 终态短事务。

        format：单并发 Semaphore(1) 编译 + 落盘（工作线程）；preprocess：
        分块 AI 标记（async 原生，LLM 等待期间零数据库事务，见
        app.word_preprocess）。终态收场（完成/失败释放）两型同路。
        """
        job_type, text, options, input_file_name = self._job_input(job_id)
        if job_type == word_jobs.JOB_PREPROCESS:
            await self._run_preprocess(job_id, text, options)
            return
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
        self._complete(
            job_id,
            output_path=f"{job_id}.docx",
            output_filename=word_jobs.output_filename_for(job_id, input_file_name),
            message="编译完成",
        )

    async def _run_preprocess(self, job_id: int, text: str, options: dict | None) -> None:
        """预处理任务全流程：AI 分块标记 → 结果落盘 → 完成收场。

        LLM 客户端构造失败（.env 配置在创建后被移除等）按内容性失败收场，
        不让任务悬在 running 等启动恢复。"""
        try:
            ai = self._marking_client()
        except Exception as exc:
            logger.exception("预处理任务 %d 的 LLM 客户端不可用", job_id)
            self._fail(job_id, f"预处理失败：模型接入不可用（{exc}）")
            return
        reason = await word_preprocess.execute_preprocess_job(
            self._session_factory, job_id=job_id, text=text, options=options, ai=ai
        )
        if reason is not None:
            self._fail(job_id, reason)
            return
        self._complete(
            job_id,
            output_path=f"{job_id}.json",
            output_filename=None,
            message="预处理完成",
        )

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

    def _complete(
        self,
        job_id: int,
        *,
        output_path: str,
        output_filename: str | None,
        message: str,
    ) -> None:
        """完成收场：running → completed + 产物路径/交付名，同一短事务。

        format 产物是 {id}.docx + 交付文件名；preprocess 产物是 {id}.json
        （结果经 /preprocess/{id}/result 回读），无交付文件名。"""
        with short_tx(self._session_factory) as session:
            session.execute(
                update(FormatJob)
                .where(
                    FormatJob.id == job_id,
                    FormatJob.status == word_jobs.JOB_RUNNING,
                )
                .values(
                    status=word_jobs.JOB_COMPLETED,
                    output_path=output_path,
                    output_filename=output_filename,
                    phase=word_jobs.PHASE_DONE,
                    progress=1.0,
                    message=message,
                    updated_at=func.current_timestamp(),
                )
            )
        word_stream.notify(job_id)

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
        # 归属输掉（对方已落终态）时本次提交零变更，通知后重读库无新事件，
        # 多叫醒一次无害
        word_stream.notify(job_id)
