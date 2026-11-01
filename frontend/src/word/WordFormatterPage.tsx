import { useEffect, useState, type FormEvent } from 'react'
import { useLocation } from 'react-router-dom'
import {
  ApiError,
  JOB_PHASE_LABELS,
  JOB_TYPE_LABELS,
  createFormatFileJob,
  createFormatTextJob,
  deleteJob,
  downloadJobOutput,
  getBuiltinSpecs,
  jobStatusLabel,
  listJobs,
  type JobView,
} from '../api'
import { useJobStream } from '../useJobStream'

/**
 * Word 排版页（issue #34，旧版 WordFormatter 页的 TS 迁移）：
 * 粘贴文本或上传 txt/md/docx（解码与 docx 提文本都在服务端）→ 选内置规格与
 * 封面/目录开关 → 提交排版任务 → SSE 实时进度 → blob 下载（header 认证）；
 * 任务列表可删除终态任务、找回进行中任务。剩余次数在外壳头部展示，创建
 * 预扣与失败退次后经 refreshQuota 刷新，与账本保持一致。
 *
 * 文章预处理页（issue #35）经路由 state 送来的标记文本直接进文本框，并按
 * marked 输入提交（保留 wf:type 结构标记，衔接与旧版一致）。
 */

const ACCEPT = '.txt,.md,.docx'

interface Props {
  cardKey: string
  refreshQuota: () => Promise<void>
}

export default function WordFormatterPage({ cardKey, refreshQuota }: Props) {
  const location = useLocation()
  const preprocessedText: string | null = location.state?.preprocessedText ?? null
  // 送来的标记文本必须按 marked 输入渲染（保留 wf:type 标记）；本次挂载内
  // 不变，无需状态。
  const inputFormat: 'auto' | 'marked' = preprocessedText ? 'marked' : 'auto'
  const [text, setText] = useState(preprocessedText ?? '')
  const [file, setFile] = useState<File | null>(null)
  const [specs, setSpecs] = useState<string[]>([])
  const [specName, setSpecName] = useState('')
  const [includeCover, setIncludeCover] = useState(true)
  const [includeToc, setIncludeToc] = useState(true)
  const [jobs, setJobs] = useState<JobView[]>([])
  const [watchingId, setWatchingId] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const { job: streamJob, connection, fatal } = useJobStream(cardKey, watchingId)

  // 首次进入：拉内置规格与任务列表（只看格式化任务，预处理任务归预处理页）；
  // 列表里有进行中任务就恢复为当前视图（进度不落本地，SSE 从头建连后服务端
  // snapshot 补齐全量）。
  useEffect(() => {
    let cancelled = false
    getBuiltinSpecs(cardKey)
      .then((names) => {
        if (cancelled) return
        setSpecs(names)
        setSpecName((prev) => prev || names[0] || '')
      })
      .catch(() => {}) // 规格加载失败不挡粘贴提交，重新进页再取
    listJobs(cardKey)
      .then((rows) => {
        if (cancelled) return
        const mine = rows.filter((j) => j.job_type === 'format')
        setJobs(mine)
        const active = mine.find((j) => j.status === 'queued' || j.status === 'running')
        if (active) setWatchingId(active.id)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [cardKey])

  // 流上的展示态合并进任务列表（三类事件载荷同形，整体替换对应行）。
  useEffect(() => {
    if (watchingId === null || !streamJob) return
    setJobs((prev) =>
      prev.map((j) => (j.id === watchingId ? { ...j, ...streamJob } : j)),
    )
  }, [watchingId, streamJob])

  // 失败退次后余额已变：刷新外壳头部的剩余次数（账本一致的页面呈现）。
  useEffect(() => {
    if (streamJob?.status === 'failed') void refreshQuota()
  }, [streamJob?.status, refreshQuota])

  async function handleTextSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      const created = await createFormatTextJob(cardKey, text, {
        input_format: inputFormat,
        spec_name: specName || undefined,
        include_cover: includeCover,
        include_toc: includeToc,
      })
      setJobs((prev) => [created, ...prev])
      setWatchingId(created.id)
      setText('')
      void refreshQuota() // 创建即预扣一次，余额已变
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleFileSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!file) return
    setError('')
    setSubmitting(true)
    try {
      const created = await createFormatFileJob(cardKey, file, {
        spec_name: specName || undefined,
        include_cover: includeCover,
        include_toc: includeToc,
      })
      setJobs((prev) => [created, ...prev])
      setWatchingId(created.id)
      setFile(null)
      void refreshQuota() // 创建即预扣一次，余额已变
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleDownload(job: JobView) {
    if (!job.output_filename) return
    setError('')
    try {
      await downloadJobOutput(cardKey, job.id, job.output_filename)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    }
  }

  async function handleDelete(job: JobView) {
    if (!window.confirm(`删除任务 #${job.id} 及其产物文件？`)) return
    setError('')
    try {
      await deleteJob(cardKey, job.id)
      setJobs((prev) => prev.filter((j) => j.id !== job.id))
      if (watchingId === job.id) setWatchingId(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    }
  }

  const watched = watchingId !== null ? jobs.find((j) => j.id === watchingId) : undefined

  return (
    <main className="workspace">
      <section className="panel">
        <h2>提交排版任务</h2>
        {preprocessedText && (
          <p className="hint">已加载预处理文本（{preprocessedText.length} 字符），排版时将保留结构标记。</p>
        )}
        <form onSubmit={handleTextSubmit}>
          <div className="option-row">
            <label>
              排版规格{' '}
              <select value={specName} onChange={(e) => setSpecName(e.target.value)}>
                {specs.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <input
                type="checkbox"
                checked={includeCover}
                onChange={(e) => setIncludeCover(e.target.checked)}
              />{' '}
              封面
            </label>
            <label>
              <input
                type="checkbox"
                checked={includeToc}
                onChange={(e) => setIncludeToc(e.target.checked)}
              />{' '}
              目录
            </label>
          </div>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="粘贴论文全文，一键排版为符合规格的 Word 文档……"
            rows={8}
            required
          />
          <div className="submit-row">
            <button type="submit" disabled={text.trim() === '' || submitting}>
              {submitting ? '提交中…' : '排版粘贴文本'}
            </button>
            <span className="hint">提交即预留 1 次，任务失败自动退回</span>
          </div>
        </form>
        <form onSubmit={handleFileSubmit}>
          <div className="submit-row">
            <input
              type="file"
              accept={ACCEPT}
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
            <button type="submit" disabled={!file || submitting}>
              排版上传文件
            </button>
          </div>
          <p className="hint">支持 txt / md / docx（GBK 编码的记事本文件也可），docx 自动提取正文。</p>
        </form>
      </section>

      {error && <p className="error">{error}</p>}

      {watched && (
        <section className="panel">
          <h2>
            任务 #{watched.id}
            <span className={`status status-${watched.status}`}>{jobStatusLabel(watched)}</span>
            <span className={`connection connection-${connection}`}>
              {connection === 'open' && '实时连接中'}
              {connection === 'reconnecting' && '重连中…'}
            </span>
          </h2>
          <p className="meta">
            {JOB_TYPE_LABELS[watched.job_type] ?? watched.job_type} ·{' '}
            {watched.input_file_name ?? '粘贴文本'}
          </p>
          {fatal && <p className="error">{fatal}</p>}
          {(watched.status === 'queued' || watched.status === 'running') && (
            <div className="job-progress">
              <div className="progress-track">
                <div
                  className="progress-fill"
                  style={{ width: `${Math.round((streamJob?.progress ?? watched.progress ?? 0) * 100)}%` }}
                />
              </div>
              <p className="meta">
                {JOB_PHASE_LABELS[streamJob?.phase ?? watched.phase ?? ''] ?? ''}{' '}
                {streamJob?.message ?? watched.message ?? '等待处理…'}
              </p>
            </div>
          )}
          {watched.status === 'completed' && (
            <div className="actions">
              <button type="button" onClick={() => void handleDownload(watched)}>
                下载 {watched.output_filename}
              </button>
            </div>
          )}
          {watched.status === 'failed' && (
            <>
              <p className="error">失败原因：{watched.error ?? '未知原因'}</p>
              <p className="hint">预留的次数已自动退回。</p>
            </>
          )}
        </section>
      )}

      <section className="panel">
        <h2>我的排版任务</h2>
        {jobs.length === 0 ? (
          <p className="hint">还没有任务，提交第一篇吧。</p>
        ) : (
          <table className="job-table">
            <thead>
              <tr>
                <th>任务</th>
                <th>输入</th>
                <th>状态</th>
                <th>提交时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id}>
                  <td>
                    #{job.id} {JOB_TYPE_LABELS[job.job_type] ?? job.job_type}
                  </td>
                  <td>{job.input_file_name ?? '粘贴文本'}</td>
                  <td>
                    <span className={`status status-${job.status}`}>{jobStatusLabel(job)}</span>
                  </td>
                  <td>{new Date(job.created_at).toLocaleString()}</td>
                  <td className="job-actions">
                    {(job.status === 'queued' || job.status === 'running') && (
                      <button type="button" className="link-button" onClick={() => setWatchingId(job.id)}>
                        {watchingId === job.id ? '查看中' : '查看进度'}
                      </button>
                    )}
                    {job.status === 'completed' && job.output_filename && (
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => void handleDownload(job)}
                      >
                        下载 {job.output_filename}
                      </button>
                    )}
                    {job.status === 'failed' && (
                      <span className="hint" title={job.error ?? ''}>
                        {job.error ?? '失败'}
                      </span>
                    )}
                    {(job.status === 'completed' || job.status === 'failed') && (
                      <button type="button" className="link-button" onClick={() => void handleDelete(job)}>
                        删除
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  )
}
