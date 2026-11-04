"""Word Formatter 任务服务：状态机、计费闭环与恢复语义的唯一拥有者（spec #31）。

调用方（HTTP 路由、任务 Worker、启动恢复）只需要本模块的公开函数，不接触
状态词表之外的字段编排、预留/释放的事务次序与产物路径解析：

- 状态机（ADR-0003：任务表状态是唯一事实源）：创建即 queued；全部迁移走
  条件 UPDATE + rowcount 校验，并发竞争恰好一方得手。不设 cancelled
  （旧版取消从未有路由暴露）。
- 计费闭环（GLOSSARY：用量上限）：创建即 :func:`app.billing.reserve_usage`
  （task_id=任务 id，账本 usage_reserve 行）；失败路径统一走
  :func:`fail_job_with_release`（同一短事务内 running/queued → failed +
  release_usage）——失败不吞次数，且条件 UPDATE 保证只释放一次。
- 产物落盘（spec #31）：docx 写数据目录、DB 只存文件名，完整路径由
  :func:`artifact_path` 现解析——数据目录迁移不改库；字节流不落库不驻内存。
- 启动恢复（:func:`recover_interrupted`，lifespan 调用）：遗留 running 复位
  queued 且 attempt_count+1；超上限 failed + 释放预留次数。两类任务均无
  内部断点（编译一体、预处理整体组装），整体重跑是唯一续跑形态。
- 下载/删除的归属判定与 :class:`JobNotFound`（对外一律 404，不泄露他人任务
  存在性）在此收口；删除只允许终态任务（queued/running 的删除会与计费/
  Worker 竞争，直接定义为非法而非加锁处理）。
- 体量护栏（M4-T7）：粘贴/上传共用的文本字符上限与上传文件字节上限（均在
  settings）统一抛 :class:`PayloadTooLarge`（对外 413，与 Nginx
  client_max_body_size 第二道防线同一状态码——两道防线任一拒绝，客户端
  看到的语义一致）；空文本等格式问题仍是 422。
- 保留期清理（:func:`purge_expired_jobs` / :func:`purge_forever`，lifespan
  挂载巡检协程）：completed/failed 任务超过保留期（settings.
  word_job_retention_seconds）连库行与全部落盘产物一并删除；queued/running
  是合法等待/执行态，清理永不触碰。

HTTP 层同在本模块（与 app.optimization 同一组织方式）：请求/响应模型与
401/403/404/409/422 错误映射。卡密只经 X-Card-Key header（app.auth），
下载也不例外。
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app import billing
from app.auth import get_current_user
from app.config import settings
from app.db import get_session, short_tx
from app.models import FormatJob, User
from app.recovery import RecoveryReport
from app.word_formatter.models.stylespec import StyleSpec
from app.word_formatter.services import builtin_specs
from app.word_services import extract_text_from_docx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/word-formatter")

JOB_FORMAT = "format"
JOB_PREPROCESS = "preprocess"

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"

# 进度三列的 phase 词表：Worker 推进的粗粒度阶段（细阶段由渲染库内部持有，
# 不落库——进度只服务前端展示，不是状态机的一部分）
PHASE_COMPILE = "compile"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

# 终态（删除等清理操作只对终态任务开放）
_TERMINAL = (JOB_COMPLETED, JOB_FAILED)

_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class JobNotFound(Exception):
    """任务不存在或不属于当前用户（对外一律 404，不泄露他人任务存在性）。"""


class JobNotDeletable(Exception):
    """任务仍在排队/执行中，不可删除（终态任务才可清理）。"""


class JobSpecInvalid(Exception):
    """自定义排版规格 JSON 非法（对外 422，创建零副作用）。"""


class UploadFormatError(Exception):
    """上传文件不可解码为排版输入（后缀不支持/docx 损坏，对外 422，零副作用）。"""


class PayloadTooLarge(Exception):
    """输入体量超限：上传文件字节数或文本字符数越过上限（对外 413，与 Nginx
    client_max_body_size 第二道防线同一状态码，两道防线语义一致）。"""


# ---- 产物落盘：路径解析的唯一入口（DB 只存文件名） ----


def artifact_dir() -> Path:
    """docx 产物目录（settings.word_output_dir，相对 backend 工作目录）。"""
    return Path(settings.word_output_dir)


def artifact_path(output_path: str) -> Path:
    """任务产物的完整路径：format_jobs.output_path 是产物目录下的文件名。"""
    return artifact_dir() / output_path


def result_filename(job_id: int) -> str:
    """预处理结果 JSON 的落盘文件名（与 docx 产物同一目录纪律）。命名归本
    模块：结果写入（app.word_preprocess）与两类清理（用户删除/保留期巡检）
    共用同一词汇，不养第二份"{id}.json"约定。"""
    return f"{job_id}.json"


def job_artifacts(job: FormatJob) -> list[Path]:
    """任务全部落盘产物：docx 产物（output_path）与预处理任务的结果 JSON。
    用户删除与保留期清理共用——任何一种清理都不允许只删其一留下残留。"""
    artifacts: list[Path] = []
    if job.output_path:
        artifacts.append(artifact_path(job.output_path))
    if job.job_type == JOB_PREPROCESS:
        artifacts.append(artifact_path(result_filename(job.id)))
    return artifacts


def output_filename_for(job_id: int, input_file_name: str | None) -> str:
    """交付文件名：保留原文件名（去扩展名）加 _formatted 后缀；纯文本输入
    无原名，回落 formatted_{任务id}.docx（与旧版 JobManager 同一约定）。"""
    if input_file_name:
        base = input_file_name.rsplit(".", 1)[0]
        return f"{base}_formatted.docx"
    return f"formatted_{job_id}.docx"


# ---- 状态机：创建（预留）/ 失败释放 / 查询 ----


def create_format_job(
    session: Session,
    user: User,
    *,
    job_type: str,
    text: str,
    options: dict | None,
    input_file_name: str | None = None,
) -> FormatJob:
    """创建任务：落库 queued → 同事务经计费服务预留 1 次（GLOSSARY：用量上限）。

    账本 usage_reserve 行的 task_id 追溯到本任务（先 flush 拿主键）。次数
    不足抛 :class:`billing.InsufficientUsage`（路由映射 403）——任务行与预留
    同事务，调用方（get_session）回滚即零副作用。input_file_name 是上传文件
    的原名（交付文件名由 :func:`output_filename_for` 由此推导）。
    """
    job = FormatJob(
        user_id=user.id,
        job_type=job_type,
        status=JOB_QUEUED,
        input_text=text,
        options=options,
        input_file_name=input_file_name,
    )
    session.add(job)
    session.flush()
    billing.reserve_usage(
        session, user.id, 1, task_id=job.id, reason=f"Word Formatter 任务预留（{job_type}）"
    )
    return job


def fail_job_with_release(
    session: Session, job_id: int, *, from_statuses: tuple[str, ...], reason: str
) -> bool:
    """失败收场原语：条件 UPDATE 抢失败归属，赢得后同一事务释放预留次数。

    Worker 失败路径与启动恢复的超限路径共用本函数。返回 False = 归属输掉
    （对方已落终态），本路径不碰任何资金；返回 True = failed + error +
    release_usage（task_id=任务 id）都由本次调用完成。预留/释放的条件原子
    UPDATE（app.billing）保证次数任何时刻不为负。
    """
    hit = (
        session.execute(
            update(FormatJob)
            .where(FormatJob.id == job_id, FormatJob.status.in_(from_statuses))
            .values(
                status=JOB_FAILED,
                error=reason,
                phase=PHASE_FAILED,
                message=reason,
                updated_at=func.current_timestamp(),
            )
        ).rowcount
        == 1
    )
    if not hit:
        return False
    user_id = session.execute(
        select(FormatJob.user_id).where(FormatJob.id == job_id)
    ).scalar_one()
    billing.release_usage(session, user_id, 1, task_id=job_id, reason=f"任务失败释放：{reason}")
    return True


def get_job_detail(session: Session, user: User, job_id: int) -> FormatJob:
    """本人任务行；不存在或不属于当前用户抛 :class:`JobNotFound`（对外 404）。"""
    job = session.execute(
        select(FormatJob).where(FormatJob.id == job_id, FormatJob.user_id == user.id)
    ).scalar_one_or_none()
    if job is None:
        raise JobNotFound(f"任务 {job_id} 不存在")
    return job


def list_user_jobs(session: Session, user: User) -> list[FormatJob]:
    """本人全部任务，最新在前。"""
    return list(
        session.execute(
            select(FormatJob)
            .where(FormatJob.user_id == user.id)
            .order_by(FormatJob.id.desc())
        ).scalars()
    )


def delete_job(session: Session, user: User, job_id: int) -> None:
    """删除本人终态任务：连落盘产物文件一并清理（spec #31 用户故事 9）。

    queued/running 抛 :class:`JobNotDeletable`（对外 409）——删除非终态任务
    会与 Worker 认领、失败释放竞争，直接定义为非法而非加锁处理。产物文件
    缺失（已被清理/磁盘故障）不阻断删除：清理的语义就是"不留痕迹"。
    """
    job = get_job_detail(session, user, job_id)
    if job.status not in _TERMINAL:
        raise JobNotDeletable(f"任务 {job_id} 尚未完成，不可删除")
    for artifact in job_artifacts(job):
        artifact.unlink(missing_ok=True)
    session.delete(job)


# ---- 启动恢复：遗留 running 复位续跑或超限失败释放（lifespan 调用） ----


def recover_interrupted(
    session_factory: sessionmaker, *, max_attempts: int | None = None
) -> RecoveryReport:
    """启动恢复：把上一进程遗留的 running 任务复位续跑或失败释放。

    每个遗留任务 attempt_count 先递增再比较（续跑前递增）：递增后超过
    max_attempts 即 :func:`fail_job_with_release`（failed + 释放预留次数）；
    否则条件 UPDATE 复位为 queued 交还 Worker，进度三列一并清空（上一次
    进程的进度是陈旧展示态，重跑从头推导）。queued 任务不碰：新 Worker
    本就按 created_at 拉取，重启期间新提交的任务由此不丢。返回计数。
    """
    limit = settings.recovery_max_attempts if max_attempts is None else max_attempts
    with short_tx(session_factory) as session:
        zombies = session.execute(
            select(FormatJob.id, FormatJob.attempt_count).where(FormatJob.status == JOB_RUNNING)
        ).all()

    resumed = abandoned = 0
    for job_id, attempt_count in zombies:
        attempts = attempt_count + 1
        if attempts > limit:
            with short_tx(session_factory) as session:
                if fail_job_with_release(
                    session,
                    job_id,
                    from_statuses=(JOB_RUNNING,),
                    reason=f"任务恢复：续跑 {attempt_count} 次后仍中断，超过上限 {limit} 次，释放预留次数",
                ):
                    abandoned += 1
            continue
        with short_tx(session_factory) as session:
            hit = (
                session.execute(
                    update(FormatJob)
                    .where(FormatJob.id == job_id, FormatJob.status == JOB_RUNNING)
                    .values(
                        status=JOB_QUEUED,
                        attempt_count=attempts,
                        phase=None,
                        progress=None,
                        message=None,
                        updated_at=func.current_timestamp(),
                    )
                ).rowcount
                == 1
            )
        if hit:
            resumed += 1
    logger.info("格式化任务启动恢复完成：%d 个复位续跑，%d 个失败释放", resumed, abandoned)
    # resumed/abandoned 计数与润色恢复协议同形，供 lifespan 日志与测试断言
    return RecoveryReport(resumed=resumed, abandoned=abandoned)


# ---- 保留期清理：过期终态任务连库行与落盘产物一并删除（lifespan 巡检挂载） ----


def purge_expired_jobs(
    session_factory: sessionmaker, *, retention_seconds: int | None = None
) -> int:
    """清理一轮：删除超过保留期的 completed/failed 任务（spec #31 用户故事
    31），返回清理个数。

    保留期以 updated_at（终态落定时刻）计。候选在一个只读短事务里选出，
    逐任务条件 DELETE（status 仍须是终态）+ :func:`job_artifacts` 全部产物
    连带删除——产物先于库行删除，删到一半失败只留下"库行在而文件不在"，
    各读取端点已把文件缺失定义为 404，不产生悬空引用。queued/running 不进
    候选：排队/执行是合法状态，任务可能随时被 Worker 认领推进。
    """
    limit = (
        settings.word_job_retention_seconds if retention_seconds is None else retention_seconds
    )
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=limit)
    with short_tx(session_factory) as session:
        expired = session.execute(
            select(FormatJob).where(
                FormatJob.status.in_(_TERMINAL), FormatJob.updated_at < cutoff
            )
        ).scalars().all()

    purged = 0
    for job in expired:
        for artifact in job_artifacts(job):
            artifact.unlink(missing_ok=True)
        with short_tx(session_factory) as session:
            hit = (
                session.execute(
                    delete(FormatJob).where(
                        FormatJob.id == job.id, FormatJob.status.in_(_TERMINAL)
                    )
                ).rowcount
                == 1
            )
        if not hit:
            continue  # 用户并发删除赢走了归属，本轮无事可做
        purged += 1
    if purged:
        logger.info("保留期清理：删除 %d 个超过保留期 %d 秒的任务及落盘产物", purged, limit)
    return purged


async def purge_forever(
    session_factory: sessionmaker,
    *,
    interval_seconds: float | None = None,
    retention_seconds: int | None = None,
) -> None:
    """常驻清理协程：周期执行 :func:`purge_expired_jobs`（见该函数契约）。

    与润色巡检（app.recovery.patrol_forever）同一形态：单轮异常记日志后
    下一轮继续，协程不死；取消随 asyncio 惯例冒泡（lifespan 关停负责
    gather）。
    """
    interval = (
        settings.word_job_purge_interval_seconds
        if interval_seconds is None
        else interval_seconds
    )
    while True:
        await asyncio.sleep(interval)
        try:
            purge_expired_jobs(session_factory, retention_seconds=retention_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("保留期清理轮次失败：本轮跳过，下一轮继续")


# ---- HTTP 层：请求/响应模型与错误映射（401/403/404/409/422） ----

InputFormatName = Literal["auto", "markdown", "plaintext", "marked"]

# 上传可解码为排版输入的扩展名 → input_format（.md 显式按 Markdown，其余交给
# 渲染库的 auto 探测；docx 提取出的正文同样走探测）
_UPLOAD_FORMATS = {"txt": "auto", "md": "markdown", "docx": "auto"}


def validated_text(text: str) -> str:
    """排版/预处理输入文本统一校验（三条纵切共用同一护栏）：非空（422）+
    长度上限（413，独立于会话的 5 万上限，排版对象是整篇文档）。运行时读取
    settings（改配置 = 改 .env + 重启，测试可 monkeypatch）；HTTP 状态映射
    归各路由（pydantic 校验器只会产出 422，承接不了 413）。"""
    if not text.strip():
        raise ValueError("文本不能为空")
    limit = settings.max_word_text_chars
    if len(text) > limit:
        raise PayloadTooLarge(f"文本长度 {len(text)} 超过上限 {limit} 字符")
    return text


def extract_upload_text(file_name: str, content: bytes) -> tuple[str, str]:
    """上传文件 → (输入文本, input_format)：格式化/预处理/格式检查共用的上传
    护栏。文件字节上限先行（:class:`PayloadTooLarge` → 413，把大文件挡在解码
    与 docx 提文本之前——那两步才是内存放大点）。

    txt/md 按 UTF-8 优先 GBK 回落解码（spec #31 用户故事 3），docx 走提文本
    工具，input_format 由扩展名推导（.md 显式 Markdown，其余交给渲染库 auto
    探测）。不支持的后缀与损坏的 docx 抛 :class:`UploadFormatError`（路由映射
    422，挡在预留之前）；解码出的文本再过 :func:`validated_text`。
    """
    limit_bytes = settings.max_word_upload_mb * 1024 * 1024
    if len(content) > limit_bytes:
        raise PayloadTooLarge(f"文件大小超过上限 {settings.max_word_upload_mb}MB")
    suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    input_format = _UPLOAD_FORMATS.get(suffix)
    if input_format is None:
        raise UploadFormatError("仅支持 txt/md/docx 文件")
    if suffix == "docx":
        try:
            return extract_text_from_docx(content), input_format
        except Exception as exc:
            raise UploadFormatError("无法读取 docx 文件，请确认文件未损坏") from exc
    try:
        return content.decode("utf-8"), input_format
    except UnicodeDecodeError:
        # GBK 回落（Windows 记事本默认编码）：两者皆非的字节按 GBK 带替换符
        # 解出——宁可出现占位符也不让一次上传 500，由空文本/长度校验兜底
        return content.decode("gbk", errors="replace"), input_format


class FormatTextRequest(BaseModel):
    """文本格式化请求：渲染库 CompileOptions 的 HTTP 形态（custom_spec 为
    已解析的规格 JSON 对象，创建时先校验、非法挡在预留之前）。text 的体量
    护栏在路由内经 :func:`validated_text` 映射（422/413），不走 pydantic
    校验器——校验器只能产出 422。"""

    text: str
    input_format: InputFormatName = "auto"
    spec_name: str | None = None
    custom_spec: dict | None = None
    include_cover: bool = True
    include_toc: bool = True
    toc_title: str = "目 录"


def _validated_custom_spec(spec: dict | None) -> dict | None:
    """自定义规格先过渲染库校验再入库（坏规格挡在预留之前，零副作用）。"""
    if spec is None:
        return None
    try:
        StyleSpec.model_validate(spec)
    except Exception as exc:
        raise JobSpecInvalid(f"自定义排版规格非法：{exc}") from exc
    return spec


def _format_options(request: FormatTextRequest) -> dict:
    """请求体 → options JSON（结构契约归 Worker 的编译映射拥有，键名即
    CompileOptions 字段名，不引入第二套词汇）。"""
    return {
        "input_format": request.input_format,
        "spec_name": request.spec_name,
        "custom_spec": _validated_custom_spec(request.custom_spec),
        "include_cover": request.include_cover,
        "include_toc": request.include_toc,
        "toc_title": request.toc_title,
    }


class JobOut(BaseModel):
    """任务状态响应：不含 input_text（可能整篇文档，列表页不需要）。"""

    id: int
    job_type: str
    status: str
    phase: str | None
    progress: float | None
    message: str | None
    input_file_name: str | None
    output_filename: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


def _job_out(job: FormatJob) -> JobOut:
    return JobOut(
        id=job.id,
        job_type=job.job_type,
        status=job.status,
        phase=job.phase,
        progress=job.progress,
        message=job.message,
        input_file_name=job.input_file_name,
        output_filename=job.output_filename,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.get("/specs")
def list_builtin_specs(user: User = Depends(get_current_user)) -> dict[str, list[str]]:
    """内置排版规格名列表（渲染库 builtin_specs），格式化页规格选择器的数据源。"""
    return {"specs": list(builtin_specs())}


@router.post("/format/text", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def create_format_text_job(
    request: FormatTextRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> JobOut:
    """粘贴文本创建格式化任务：体量护栏（422/413）→ 规格校验（422）→
    预留次数（403）→ queued。"""
    try:
        text = validated_text(request.text)
    except PayloadTooLarge as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    try:
        options = _format_options(request)
    except JobSpecInvalid as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    try:
        job = create_format_job(
            session, user, job_type=JOB_FORMAT, text=text, options=options
        )
    except billing.InsufficientUsage as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return _job_out(job)


@router.post("/format/file", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_format_file_job(
    file: UploadFile,
    spec_name: str | None = Form(None),
    include_cover: bool = Form(True),
    include_toc: bool = Form(True),
    toc_title: str = Form("目 录"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> JobOut:
    """上传文件创建格式化任务：体量护栏（413）→ 解码/提文本（422）→
    预留次数（403）→ queued。

    解码、提文本与文件字节上限归 :func:`extract_upload_text`（与预处理/
    格式检查共用同一护栏），调用方只交文件。原文件名随任务保留，交付文件名
    由 :func:`output_filename_for` 推导（原名_formatted.docx）。
    """
    file_name = file.filename or ""
    content = await file.read()
    try:
        text, input_format = extract_upload_text(file_name, content)
        text = validated_text(text)
    except PayloadTooLarge as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)) from exc
    except UploadFormatError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    options = {
        "input_format": input_format,
        "spec_name": spec_name,
        "custom_spec": None,
        "include_cover": include_cover,
        "include_toc": include_toc,
        "toc_title": toc_title,
    }
    try:
        job = create_format_job(
            session,
            user,
            job_type=JOB_FORMAT,
            text=text,
            options=options,
            input_file_name=file_name,
        )
    except billing.InsufficientUsage as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return _job_out(job)


@router.get("/jobs", response_model=list[JobOut])
def list_my_jobs(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[JobOut]:
    """本人最近任务列表（最新在前）。"""
    return [_job_out(job) for job in list_user_jobs(session, user)]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_my_job(
    job_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> JobOut:
    try:
        job = get_job_detail(session, user, job_id)
    except JobNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _job_out(job)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_job(
    job_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> None:
    """删除本人终态任务，连落盘产物一并清理。"""
    try:
        delete_job(session, user, job_id)
    except JobNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except JobNotDeletable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/download")
def download_my_job_output(
    job_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> FileResponse:
    """下载完成任务产物：X-Card-Key header 认证（卡密绝不进 URL），FileResponse
    从盘流式发送，Content-Disposition 文件名保留原名加后缀。

    归属失败 404（不泄露他人任务存在性）；未完成 409；产物文件缺失
    （磁盘被清理）404——库行在而文件不在，按不存在处理。
    """
    try:
        job = get_job_detail(session, user, job_id)
    except JobNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if job.status != JOB_COMPLETED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="任务尚未完成，无法下载"
        )
    path = artifact_path(job.output_path)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="产物文件不存在")
    return FileResponse(
        path, media_type=_DOCX_MEDIA_TYPE, filename=job.output_filename
    )
