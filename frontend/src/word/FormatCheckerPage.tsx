import { useEffect, useState, type FormEvent } from 'react'
import {
  ApiError,
  ISSUE_SEVERITY_LABELS,
  ISSUE_TYPE_LABELS,
  checkFormatFile,
  checkFormatText,
  getFormatCheckTypes,
  type CheckMode,
  type FormatCheckResultView,
  type FormatIssueView,
  type ParagraphTypeView,
} from '../api'

/**
 * 格式检查页（issue #37，旧版 FormatChecker 页的 TS 迁移）：粘贴文本或上传
 * txt/md/docx → 选宽松/严格两档 → 同步规则检查 → 按行号的问题明细（错误/
 * 警告/提示分级）+ 段落类型分布 + 段落类型字典说明。
 *
 * 检查免费（GLOSSARY：免费格式检查）：不创建任务、不耗次数，同步返回终态——
 * 页面没有任务列表与进度流，也没有额度刷新。
 */

const ACCEPT = '.txt,.md,.docx'

const CHECK_MODE_OPTIONS: { value: CheckMode; label: string; hint: string }[] = [
  { value: 'loose', label: '宽松', hint: '只报必须修复的错误' },
  { value: 'strict', label: '严格', hint: '所有不规范格式都报' },
]

const SEVERITY_BADGE: Record<FormatIssueView['severity'], string> = {
  error: 'status status-failed',
  warning: 'status status-running',
  info: 'status status-queued',
}

interface Props {
  cardKey: string
}

export default function FormatCheckerPage({ cardKey }: Props) {
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [mode, setMode] = useState<CheckMode>('loose')
  const [result, setResult] = useState<FormatCheckResultView | null>(null)
  const [types, setTypes] = useState<ParagraphTypeView[]>([])
  const [error, setError] = useState('')
  const [checking, setChecking] = useState(false)

  // 段落类型字典（检查结果术语的唯一数据源，服务端下发，词汇不在前端复制）
  useEffect(() => {
    let cancelled = false
    getFormatCheckTypes(cardKey)
      .then((rows) => {
        if (!cancelled) setTypes(rows)
      })
      .catch(() => {}) // 字典加载失败不挡检查，重新进页再取
    return () => {
      cancelled = true
    }
  }, [cardKey])

  async function runCheck(execute: () => Promise<FormatCheckResultView>) {
    setError('')
    setChecking(true)
    setResult(null)
    try {
      setResult(await execute())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    } finally {
      setChecking(false)
    }
  }

  async function handleTextSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    await runCheck(() => checkFormatText(cardKey, text, mode))
  }

  async function handleFileSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!file) return
    await runCheck(() => checkFormatFile(cardKey, file, mode))
  }

  // 按行号排明细（同一行多条按问题类型稳定排序），缺摘要/关键词等全文级
  // 问题报在第 1 行
  const issues = result ? [...result.issues].sort((a, b) => a.line - b.line) : []

  return (
    <main className="workspace">
      <section className="panel">
        <h2>格式检查</h2>
        <p className="hint">
          排版前先检查文章结构（标题层级、摘要、关键词、参考文献等），免费检查、
          不耗次数。
        </p>
        <div className="mode-picker" role="radiogroup" aria-label="检查模式">
          {CHECK_MODE_OPTIONS.map((option) => (
            <label key={option.value} className={`mode ${mode === option.value ? 'checked' : ''}`}>
              <input
                type="radio"
                name="check-mode"
                checked={mode === option.value}
                onChange={() => setMode(option.value)}
              />
              {option.label}（{option.hint}）
            </label>
          ))}
        </div>
        <form onSubmit={handleTextSubmit}>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="粘贴论文全文，检查标题层级跳跃、缺摘要、段落过长等问题……"
            rows={8}
            required
          />
          <div className="submit-row">
            <button type="submit" disabled={text.trim() === '' || checking}>
              {checking ? '检查中…' : '检查粘贴文本'}
            </button>
            <span className="hint">免费检查，不耗次数</span>
          </div>
        </form>
        <form onSubmit={handleFileSubmit}>
          <div className="submit-row">
            <input
              type="file"
              accept={ACCEPT}
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
            <button type="submit" disabled={!file || checking}>
              检查上传文件
            </button>
          </div>
          <p className="hint">支持 txt / md / docx（GBK 编码的记事本文件也可），docx 自动提取正文。</p>
        </form>
      </section>

      {error && <p className="error">{error}</p>}

      {result && (
        <section className="panel">
          <h2>
            检查结果（{mode === 'loose' ? '宽松' : '严格'}）
            <span className={`status ${result.is_valid ? 'status-completed' : 'status-failed'}`}>
              {result.is_valid ? '通过' : '存在问题'}
            </span>
          </h2>
          {result.issues.length === 0 ? (
            <p className="hint">
              {mode === 'loose'
                ? '宽松档未发现必须修复的错误。可切换严格档查看全部建议。'
                : '未发现格式问题。'}
            </p>
          ) : (
            <table className="job-table">
              <thead>
                <tr>
                  <th>行号</th>
                  <th>严重度</th>
                  <th>问题</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {issues.map((issue, i) => (
                  <tr key={`${issue.line}-${i}`}>
                    <td>{issue.line}</td>
                    <td>
                      <span className={SEVERITY_BADGE[issue.severity]}>
                        {ISSUE_SEVERITY_LABELS[issue.severity] ?? issue.severity}
                      </span>
                    </td>
                    <td>{ISSUE_TYPE_LABELS[issue.issue_type] ?? issue.issue_type}</td>
                    <td>
                      {issue.message}
                      {issue.content_preview && (
                        <span className="hint">（{issue.content_preview}）</span>
                      )}
                      {issue.suggestion && <p className="hint">建议：{issue.suggestion}</p>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="meta">
            段落类型统计：
            {Object.entries(result.type_statistics)
              .map(([type, count]) => {
                const label = types.find((t) => t.type === type)?.label
                return `${label ?? type} ${count} 段`
              })
              .join(' · ')}
          </p>
        </section>
      )}

      {types.length > 0 && (
        <section className="panel">
          <h2>段落类型字典</h2>
          <p className="hint">检查结果与预处理标记使用的段落类型术语。</p>
          <table className="job-table">
            <thead>
              <tr>
                <th>类型</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {types.map((t) => (
                <tr key={t.type}>
                  <td>{t.type}</td>
                  <td>{t.label}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </main>
  )
}
