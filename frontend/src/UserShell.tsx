import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
import { formatYuan, type Quota } from './api'

/**
 * 用户页外壳（issue #34）：卡密门控通过后的公共框架——顶部导航（工作台与
 * Word 排版各页互达）+ 余额/剩余次数展示（复用 /api/me/quota）+ 退出。
 * 卡密即凭据的 sessionStorage 边界仍归 App；门控之后的所有用户页都渲染在
 * 这里，深链 /word 等同样先过卡密门。
 */

const NAV_ITEMS: { to: string; label: string; end?: boolean }[] = [
  { to: '/', label: '工作台', end: true },
  { to: '/word', label: 'Word 排版' },
  { to: '/word/specs', label: '规格生成' },
  { to: '/word/preprocess', label: '文章预处理' },
  { to: '/word/check', label: '格式检查' },
]

interface Props {
  quota: Quota
  onExit: () => void
  children: ReactNode
}

export default function UserShell({ quota, onExit, children }: Props) {
  return (
    <>
      <header className="user-header">
        <nav className="user-nav" aria-label="页面导航">
          {NAV_ITEMS.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end}>
              {item.label}
            </NavLink>
          ))}
        </nav>
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
      {children}
    </>
  )
}
