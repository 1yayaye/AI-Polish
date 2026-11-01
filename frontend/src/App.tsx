import { useState, type FormEvent } from 'react'
import Workspace from './Workspace'
import type { Quota } from './api'

/**
 * 卡密输入页：输入卡密后经 X-Card-Key 请求头查询本人额度
 * （卡密只走请求头，绝不进 URL，也不落任何日志可控的位置）。
 * 验证通过进入工作台。
 */

export default function App() {
  const [cardKey, setCardKey] = useState('')
  const [quota, setQuota] = useState<Quota | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setLoading(true)
    try {
      const response = await fetch('/api/me/quota', {
        headers: { 'X-Card-Key': cardKey.trim() },
      })
      if (response.status === 401) {
        setError('卡密无效，请检查后重试')
        setQuota(null)
        return
      }
      if (!response.ok) {
        setError('服务暂时不可用，请稍后重试')
        setQuota(null)
        return
      }
      setQuota((await response.json()) as Quota)
    } catch {
      setError('网络错误，请稍后重试')
      setQuota(null)
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
