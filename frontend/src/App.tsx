import { useEffect, useState, type FormEvent } from 'react'
import Workspace from './Workspace'
import { ApiError, getQuota, type Quota } from './api'

/**
 * 卡密输入页：输入卡密后经 X-Card-Key 请求头查询本人额度
 * （卡密只走请求头，绝不进 URL，也不落任何日志可控的位置）。
 * 验证通过进入工作台。
 *
 * 卡密即凭据（GLOSSARY），持久化边界选 sessionStorage：刷新保留、
 * 标签页关闭即清除；挂载时静默验证残留卡密，有效则用户无感回到工作台。
 */

const CARD_KEY_STORAGE = 'ai-polish.card-key'

function loadStoredCardKey(): string {
  // 隐私模式等场景下 sessionStorage 可能整体不可用：降级为不记住卡密，
  // 退回每次手动输入，而不是让页面崩掉。
  try {
    return sessionStorage.getItem(CARD_KEY_STORAGE)?.trim() ?? ''
  } catch {
    return ''
  }
}

function saveStoredCardKey(cardKey: string): void {
  try {
    sessionStorage.setItem(CARD_KEY_STORAGE, cardKey)
  } catch {
    // 同上：存不下就只影响下次刷新，不影响本次使用
  }
}

function clearStoredCardKey(): void {
  try {
    sessionStorage.removeItem(CARD_KEY_STORAGE)
  } catch {
    // 同上
  }
}

export default function App() {
  const [cardKey, setCardKey] = useState(loadStoredCardKey)
  const [quota, setQuota] = useState<Quota | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // 刷新恢复：sessionStorage 里有卡密就静默验证——成功直接进入工作台
  // （用户无感）；401 说明卡密已失效，清除后停留输入页；网络故障则
  // 保留卡密（输入框预填）等待重试。
  useEffect(() => {
    const stored = loadStoredCardKey()
    if (stored === '') return
    setLoading(true)
    getQuota(stored)
      .then((q) => setQuota(q))
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          clearStoredCardKey()
          setCardKey('')
          setError('保存的卡密已失效，请重新输入')
        } else {
          setError('网络错误，请稍后重试')
        }
      })
      .finally(() => setLoading(false))
  }, [])

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setLoading(true)
    const trimmed = cardKey.trim()
    try {
      const q = await getQuota(trimmed)
      saveStoredCardKey(trimmed)
      setQuota(q)
    } catch (err) {
      setQuota(null)
      if (err instanceof ApiError) {
        setError(err.status === 401 ? '卡密无效，请检查后重试' : '服务暂时不可用，请稍后重试')
      } else {
        setError('网络错误，请稍后重试')
      }
    } finally {
      setLoading(false)
    }
  }

  if (quota !== null) {
    return (
      <Workspace
        cardKey={cardKey.trim()}
        quota={quota}
        onQuotaChange={setQuota}
        onExit={() => {
          clearStoredCardKey()
          setQuota(null)
          setCardKey('')
        }}
      />
    )
  }

  return (
    <main className="entry">
      <h1>AI-Polish 学术写作助手</h1>
      <p className="hint">输入卡密开始使用，无需注册账号。</p>
      <form className="card-key-form" onSubmit={handleSubmit}>
        <input
          type="password"
          name="cardKey"
          placeholder="请输入卡密"
          value={cardKey}
          onChange={(e) => setCardKey(e.target.value)}
          autoComplete="off"
          required
        />
        <button type="submit" disabled={cardKey.trim() === '' || loading}>
          {loading ? '查询中…' : '进入'}
        </button>
      </form>
      {error && <p className="error">{error}</p>}
    </main>
  )
}
