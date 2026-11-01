import { useCallback, useEffect, useState, type FormEvent } from 'react'
import {
  ApiError,
  MODE_LABELS,
  SESSION_STATUS_LABELS,
  STAGE_LABELS,
  cancelSession,
  createSession,
  downloadExport,
  formatYuan,
  getChanges,
  getQuota,
  listSessions,
  type ChangeEntry,
  type Quota,
  type SessionView,
} from './api'
import { useSessionStream } from './useSessionStream'

/**
 * 工作台页（issue #19）：提交（模式选择/文本框）→ 逐段进度实时显示（SSE）
 * → 完成 → 导出入口；每段完成前后对照（变更记录）可查。
 */

const MODE_OPTIONS = ['paper_polish', 'paper_enhance', 'paper_polish_enhance'] as const

const SEGMENT_STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  completed: '已完成',
  failed: '失败',
}

interface Props {
  cardKey: string
  quota: Quota
  onQuotaChange: (quota: Quota) => void
  onExit: () => void
}

export default function Workspace({ cardKey, quota, onQuotaChange, onExit }: Props) {
  const [mode, setMode] = useState<string>('paper_polish_enhance')
  const [text, setText] = useState('')
  const [session, setSession] = useState<SessionView | null>(null)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const { snapshot, connection, fatal } = useSessionStream(cardKey, session?.id ?? null)

  // 刷新恢复：卡密已由 App 从 sessionStorage 还原，这里只差找回进行中的
  // 会话——列表最新在前，取第一个 queued/running 的恢复为当前视图。
  // 段落进度不落本地：SSE 从头建连后服务端 snapshot 补齐全量，
  // 天然"从位置继续"。恢复失败不打断工作台，用户仍可提交新任务。
  useEffect(() => {
    let cancelled = false
    listSessions(cardKey)
      .then((sessions) => {
        const active = sessions.find((s) => s.status === 'queued' || s.status === 'running')
        if (!cancelled && active) setSession(active)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [cardKey])

  const refreshQuota = useCallback(async () => {
    try {
      onQuotaChange(await getQuota(cardKey))
    } catch {
      // 额度刷新失败不打断主流程（下次操作会再试）
    }
  }, [cardKey, onQuotaChange])

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      const created = await createSession(cardKey, text, mode)
      setSession(created)
      void refreshQuota() // 预扣费后余额已变
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleCancel() {
    if (!session) return
    setError('')
    try {
      const cancelled = await cancelSession(cardKey, session.id)
      setSession(cancelled)
      void refreshQuota() // 取消退款后余额已变
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    }
  }

  const sessionStatus = snapshot?.status ?? session?.status ?? 'queued'
  const segments = snapshot?.segments ?? session?.segments ?? []
  const cancellable = sessionStatus === 'queued' || sessionStatus === 'running'

  return (
    <main className="workspace">
      <header className="workspace-header">
        <div className="quota">
          <span className="quota-item">
            余额 <strong>¥{formatYuan(quota.balance_fen)}</strong>
          </span>
          <span className="quota-item">
            剩余次数 <strong>{quota.usage_remaining}</strong>
          </span>
        </div>
        <button type="button" className="link-button" onClick={onExit}>
          退出
        </button>
      </header>

      <section className="panel">
        <h2>提交润色任务</h2>
        <form onSubmit={handleSubmit}>
          <div className="mode-picker" role="radiogroup" aria-label="处理模式">
            {MODE_OPTIONS.map((option) => (
              <label key={option} className={mode === option ? 'mode checked' : 'mode'}>
                <input
                  type="radio"
                  name="mode"
                  value={option}
                  checked={mode === option}
                  onChange={() => setMode(option)}
                />
                {MODE_LABELS[option]}
              </label>
            ))}
          </div>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="粘贴论文文本，按段落自动切分、逐段处理……"
            rows={8}
            required
          />
          <div className="submit-row">
            <button type="submit" disabled={text.trim() === '' || submitting || session !== null}>
              {submitting ? '提交中…' : '提交任务'}
            </button>
            <span className="hint">提交即按字数预扣费，失败或取消自动退款</span>
          </div>
        </form>
      </section>

      {error && <p className="error">{error}</p>}

      {session && (
        <section className="panel">
          <h2>
            会话 #{session.id}
            <span className={`status status-${sessionStatus}`}>
              {SESSION_STATUS_LABELS[sessionStatus] ?? sessionStatus}
            </span>
            <span className={`connection connection-${connection}`}>
              {connection === 'open' && '实时连接中'}
              {connection === 'reconnecting' && '重连中…'}
            </span>
          </h2>
          <p className="meta">
            {MODE_LABELS[session.mode] ?? session.mode} · {session.word_count} 字 · 预扣
            ¥{formatYuan(session.charged_fen)}
            {sessionStatus === 'failed' && '（已自动退款）'}
            {sessionStatus === 'cancelled' && '（已退款）'}
          </p>
          {fatal && <p className="error">{fatal}</p>}

          <SegmentList sessionId={session.id} segments={segments} cardKey={cardKey} />

          <div className="actions">
            {cancellable && (
              <button type="button" onClick={handleCancel}>
                取消任务并退款
              </button>
            )}
            {sessionStatus === 'completed' && (
              <>
                <button type="button" onClick={() => void handleExport('txt')}>
                  导出 txt
                </button>
                <button type="button" onClick={() => void handleExport('docx')}>
                  导出 docx
                </button>
              </>
            )}
          </div>
        </section>
      )}
    </main>
  )

  async function handleExport(format: 'txt' | 'docx') {
    if (!session) return
    setError('')
    try {
      await downloadExport(cardKey, session.id, format)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    }
  }
}

function SegmentList({
  sessionId,
  segments,
  cardKey,
}: {
  sessionId: number
  segments: { stage: string; seq: number; status: string }[]
  cardKey: string
}) {
  const [changes, setChanges] = useState<ChangeEntry[] | null>(null)
  const [selected, setSelected] = useState<{ stage: string; seq: number } | null>(null)
  const [loadError, setLoadError] = useState('')

  async function toggleDetail(stage: string, seq: number) {
    setLoadError('')
    if (selected?.stage === stage && selected?.seq === seq) {
      setSelected(null)
      return
    }
    setSelected({ stage, seq })
    if (changes === null) {
      try {
        setChanges(await getChanges(cardKey, sessionId))
      } catch (err) {
        setLoadError(err instanceof ApiError ? err.message : '变更记录加载失败')
      }
    }
  }

  if (segments.length === 0) {
    return <p className="hint">等待切分结果…</p>
  }

  const selectedChange =
    selected && changes
      ? changes.find((c) => c.stage === selected.stage && c.seq === selected.seq)
      : undefined

  return (
    <div className="segments">
      <ol>
        {segments.map((seg) => (
          <li key={`${seg.stage}-${seg.seq}`} className={`segment segment-${seg.status}`}>
            <span className="seg-stage">{STAGE_LABELS[seg.stage] ?? seg.stage}</span>
            <span className="seg-status">{SEGMENT_STATUS_LABELS[seg.status] ?? seg.status}</span>
            {seg.status === 'completed' && (
              <button type="button" className="link-button" onClick={() => void toggleDetail(seg.stage, seg.seq)}>
                {selected?.stage === seg.stage && selected?.seq === seg.seq ? '收起对照' : '查看前后对照'}
              </button>
            )}
          </li>
        ))}
      </ol>
      {loadError && <p className="error">{loadError}</p>}
      {selectedChange && (
        <div className="change-detail">
          <div>
            <h4>处理前</h4>
            <p>{selectedChange.before_text}</p>
          </div>
          <div>
            <h4>处理后</h4>
            <p>{selectedChange.after_text}</p>
          </div>
        </div>
      )}
    </div>
  )
}
