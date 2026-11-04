import { useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ApiError,
  JOB_PHASE_LABELS,
  PARAGRAPH_TYPE_LABELS,
  createPreprocessFileJob,
  createPreprocessTextJob,
  deleteJob,
  getPreprocessResult,
  jobStatusLabel,
  listJobs,
  type JobView,
  type PreprocessResultView,
} from '../api'
import { useJobStream } from '../useJobStream'

/**
 * 文章预处理页（issue #35，GLOSSARY：排版规格前置环节；旧版
 * ArticlePreprocessor 页的 TS 迁移）：粘贴文本或上传 txt/md/docx → 设分块
 * 粒度（段落数/字符数）→ 提交 AI 分块标记任务 → SSE 实时进度（阶段 +
 * 第几块/共几块）→ 结果预览（标记文本、段落类型统计、一致性校验结论）→
 * 一键送格式化页（标记文本进格式化页文本框，按 marked 输入渲染，衔接保持
 * 旧版交互）。模型接入未配置时创建即 503，页面上以错误提示呈现。
 */

const ACCEPT = '.txt,.md,.docx'

interface Props {
  cardKey: string
  refreshQuota: () => Promise<void>
}

export default function ArticlePreprocessorPage({ cardKey, refreshQuota }: Props) {
  const navigate = useNavigate()
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [chunkParagraphs, setChunkParagraphs] = useState(40)
  const [chunkChars, setChunkChars] = useState(8000)
  const [jobs, setJobs] = useState<JobView[]>([])
  const [watchingId, setWatchingId] = useState<number | null>(null)
  const [result, setResult] = useState<PreprocessResultView | null>(null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const { job: streamJob, connection, fatal } = useJobStream(cardKey, watchingId)

  // 首次进入：拉任务列表（只看预处理任务），有进行中任务就恢复为当前视图；
  // 进度不落本地，SSE 从头建连后服务端 snapshot 补齐全量。
  useEffect(() => {
    let cancelled = false
    listJobs(cardKey)
      .then((rows) => {
        if (cancelled) return
        const mine = rows.filter((j) => j.job_type === 'preprocess')
        setJobs(mine)
        const active = mine.find((j) => j.status === 'queued' || j.status === 'running')
        if (active) setWatchingId(active.id)
      })
      .catch(() => {}) // 列表加载失败不挡提交，重新进页再取
    return () => {
      cancelled = true
    }
  }, [cardKey])

  // 流上的展示态合并进任务列表（snapshot/progress/终态载荷同形）。
  // 载荷带 job_id：切换关注任务的重渲染里 streamJob 还是上一个任务的尾帧，
  // 按 id 挡掉——否则旧任务的 completed 会误触发新任务的结果请求（409）。
  useEffect(() => {
    if (watchingId === null || !streamJob || streamJob.job_id !== watchingId) return
    setJobs((prev) =>
      prev.map((j) => (j.id === watchingId ? { ...j, ...streamJob } : j)),
    )
  }, [watchingId, streamJob])

  const watched = watchingId !== null ? jobs.find((j) => j.id === watchingId) : undefined

  // 失败退次后额度已变；结果在任务完成（或回看已完成任务）时取一次。
  useEffect(() => {
    if (watched?.status === 'failed') void refreshQuota()
  }, [watched?.status, refreshQuota])

  useEffect(() => {
    if (watched?.status !== 'completed' || watchingId === null || result) return
    let cancelled = false
    getPreprocessResult(cardKey, watchingId)
      .then((body) => {
        if (!cancelled) setResult(body)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : '获取结果失败')
      })
    return () => {
      cancelled = true
    }
  }, [watched?.status, watchingId, result, cardKey])

  /** 创建并接管展示态；返回是否创建成功——失败（503/422/403/网络错误）时
   *  保留用户输入，修正后可重提（与格式化页同一交互契约）。 */
  async function submit(create: () => Promise<JobView>): Promise<boolean> {
    setError('')
    setSubmitting(true)
    setResult(null)
    try {
      const created = await create()
      setJobs((prev) => [created, ...prev])
      setWatchingId(created.id)
      void refreshQuota() // 创建即预留 1 次
      return true
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
      return false
    } finally {
      setSubmitting(false)
    }
  }

  async function handleTextSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const ok = await submit(() =>
      createPreprocessTextJob(cardKey, text, {
        chunk_paragraphs: chunkParagraphs,
        chunk_chars: chunkChars,
      }),
    )
    if (ok) setText('')
  }

  async function handleFileSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!file) return
    const ok = await submit(() =>
      createPreprocessFileJob(cardKey, file, {
        chunk_paragraphs: chunkParagraphs,
        chunk_chars: chunkChars,
      }),
    )
    if (ok) setFile(null)
  }

  // 一键送格式化（旧版交互）：标记文本进格式化页文本框并按 marked 输入渲染，
  // 用户在格式化页选规格后提交排版。
  function handleSendToFormatter() {
    if (!result?.marked_text) return
    navigate('/word', { state: { preprocessedText: result.marked_text } })
  }

  function handleExportMarked() {
    if (!result?.marked_text) return
    const blob = new Blob([result.marked_text], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    try {
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = file ? file.name.replace(/\.[^.]+$/, '_marked.md') : 'article_marked.md'
      anchor.click()
    } finally {
      URL.revokeObjectURL(url)
    }
  }

  async function handleDelete(job: JobView) {
    if (!window.confirm(`删除任务 #${job.id} 及其结果文件？`)) return
    setError('')
    try {
      await deleteJob(cardKey, job.id)
      setJobs((prev) => prev.filter((j) => j.id !== job.id))
      if (watchingId === job.id) {
        setWatchingId(null)
        setResult(null)
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    }
  }

  const chunkLabel =
    streamJob?.chunk_index != null && streamJob.chunk_total != null
      ? `第 ${streamJob.chunk_index}/${streamJob.chunk_total} 块`
      : ''

  return (
    <main className="workspace">
      <section className="panel">
        <h2>提交预处理任务</h2>
        <p className="hint">
          AI 分块标记段落类型（标题/摘要/正文/参考文献等），产物是带结构标记的
          文本，可一键送格式化排版。
        </p>
        <form onSubmit={handleTextSubmit}>
          <div className="option-row">
            <label>
              每块段落数{' '}
              <input
                type="number"
                min={10}
                max={100}
                value={chunkParagraphs}
                onChange={(e) => setChunkParagraphs(Number(e.target.value))}
              />
            </label>
            <label>
              每块字符数{' '}
              <input
                type="number"
                min={2000}
                max={15000}
                step={500}
                value={chunkChars}
                onChange={(e) => setChunkChars(Number(e.target.value))}
              />
            </label>
          </div>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="粘贴论文全文，AI 先识别文章结构，再排版事半功倍……"
            rows={8}
            required
          />
          <div className="submit-row">
            <button type="submit" disabled={text.trim() === '' || submitting}>
              {submitting ? '提交中…' : '预处理粘贴文本'}
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
              预处理上传文件
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
          {fatal && <p className="error">{fatal}</p>}
          {(watched.status === 'queued' || watched.status === 'running') && (
            <div className="job-progress">
              <div className="progress-track">
                <div
                  className="progress-fill"
                  style={{
                    width: `${Math.round((streamJob?.progress ?? watched.progress ?? 0) * 100)}%`,
                  }}
                />
              </div>
              <p className="meta">
                {JOB_PHASE_LABELS[streamJob?.phase ?? watched.phase ?? ''] ?? ''}{' '}
                {chunkLabel} {streamJob?.message ?? watched.message ?? '等待处理…'}
              </p>
            </div>
          )}
          {watched.status === 'failed' && (
            <>
              <p className="error">失败原因：{watched.error ?? '未知原因'}</p>
              <p className="hint">预留的次数已自动退回。</p>
            </>
          )}
          {watched.status === 'completed' && result && (
            <>
              <p className={result.integrity_check_passed ? 'meta' : 'error'}>
                一致性校验：
                {result.integrity_check_passed
                  ? '通过，标记文本与原文内容一致'
                  : '未通过，标记文本与原文存在差异，请检查后再送格式化'}
              </p>
              {result.warnings.length > 0 && (
                <ul className="hint">
                  {result.warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              )}
              <p className="meta">
                段落类型统计：
                {Object.entries(result.type_statistics)
                  .map(
                    ([type, count]) =>
                      `${PARAGRAPH_TYPE_LABELS[type] ?? type} ${count} 段`,
                  )
                  .join(' · ')}
              </p>
              <textarea value={result.marked_text} readOnly rows={12} />
              <div className="actions">
                <button type="button" onClick={handleSendToFormatter}>
                  一键送格式化
                </button>
                <button type="button" className="link-button" onClick={handleExportMarked}>
                  下载标记文本（.md）
                </button>
              </div>
            </>
          )}
        </section>
      )}

      <section className="panel">
        <h2>我的预处理任务</h2>
        {jobs.length === 0 ? (
          <p className="hint">还没有预处理任务，提交第一篇吧。</p>
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
                  <td>#{job.id}</td>
                  <td>{job.input_file_name ?? '粘贴文本'}</td>
                  <td>
                    <span className={`status status-${job.status}`}>{jobStatusLabel(job)}</span>
                  </td>
                  <td>{new Date(job.created_at).toLocaleString()}</td>
                  <td className="job-actions">
                    {(job.status === 'queued' ||
                      job.status === 'running' ||
                      job.status === 'completed') && (
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => {
                          if (job.id !== watchingId) setResult(null)
                          setWatchingId(job.id)
                        }}
                      >
                        {watchingId === job.id ? '查看中' : '查看进度/结果'}
                      </button>
                    )}
                    {(job.status === 'completed' || job.status === 'failed') && (
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => void handleDelete(job)}
                      >
                        删除
                      </button>
                    )}
                    {job.status === 'failed' && (
                      <span className="hint" title={job.error ?? ''}>
                        {job.error ?? '失败'}
                      </span>
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
