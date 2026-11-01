import { useState, type FormEvent } from 'react'

/**
 * 卡密输入页（占位）：M1/T2 将接入卡密认证 API（卡密只经请求头传递，绝不进 URL）。
 * 此页仅验证骨架构建链路与基础交互形态。
 */
export default function App() {
  const [cardKey, setCardKey] = useState('')

  function handleSubmit(_event: FormEvent<HTMLFormElement>) {
    // 占位：T2 接入卡密登录后跳转工作台
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
        <button type="submit" disabled={cardKey.trim() === ''}>
          进入
        </button>
      </form>
    </main>
  )
}
