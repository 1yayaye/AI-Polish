import { useEffect, useState } from 'react'
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Form,
  Input,
  Layout,
  Space,
  Spin,
  Typography,
} from 'antd'
import { NavLink, Route, Routes } from 'react-router-dom'
import { AdminError, adminLogin, adminLogout, getAdminSession, onUnauthorized } from './api'
import ConfigPage from './ConfigPage'
import LedgerPage from './LedgerPage'
import SessionsPage from './SessionsPage'
import UsersPage from './UsersPage'

/**
 * 管理后台外壳：/admin 路径树整棵渲染在这里。三种视图——
 * checking（启动校验残留会话）→ login（登录表单）→ ready（后台外壳）。
 *
 * 401 汇聚点：会话 cookie 缺失/过期（12 小时 TTL）时后端一律 401。ready 态
 * 下任何管理接口的 401 经 ./api 的 onUnauthorized 统一回登录表单——运营页面
 * （发卡、充值、会话监控等）沿用 ./api 的调用方式即可获得同一行为，不需要
 * 各自处理。登录表单里的 401 不是会话过期而是凭据错误，照常展示后端原因。
 */

type AdminView =
  | { state: 'checking' }
  | { state: 'login'; error: string; submitting: boolean }
  | { state: 'ready'; username: string }

interface LoginValues {
  username: string
  password: string
}

export default function AdminApp() {
  const [view, setView] = useState<AdminView>({ state: 'checking' })

  // 启动校验：cookie 里的会话仍有效就直接进外壳（深链刷新不丢登录态）；
  // 无效/过期一律落到登录表单。
  useEffect(() => {
    getAdminSession()
      .then((s) => setView({ state: 'ready', username: s.username }))
      .catch(() => setView({ state: 'login', error: '', submitting: false }))
  }, [])

  // 401 汇聚：登录/启动校验阶段的 401 不经这里（各分支自己消化），只有
  // ready 态的过期会话触发回登录表单——功能性更新保证非 ready 态原样不动。
  useEffect(() => {
    return onUnauthorized(() => {
      setView((prev) =>
        prev.state === 'ready'
          ? { state: 'login', error: '会话已过期，请重新登录', submitting: false }
          : prev,
      )
    })
  }, [])

  async function handleLogin({ username, password }: LoginValues) {
    setView({ state: 'login', error: '', submitting: true })
    try {
      const session = await adminLogin(username, password)
      setView({ state: 'ready', username: session.username })
    } catch (err) {
      const message =
        err instanceof AdminError ? err.message : '网络错误，请稍后重试'
      setView({ state: 'login', error: message, submitting: false })
    }
  }

  async function handleLogout() {
    try {
      await adminLogout()
    } catch {
      // 登出请求失败（如会话已先一步过期 401）不拦截：目标状态就是未登录
    } finally {
      setView({ state: 'login', error: '', submitting: false })
    }
  }

  if (view.state === 'checking') {
    return (
      <main className="admin-boot">
        <Spin />
      </main>
    )
  }

  if (view.state === 'login') {
    return (
      <main className="admin-boot">
        <Card title="AI-Polish 管理后台" style={{ width: 360 }}>
          <Form<LoginValues>
            layout="vertical"
            onFinish={handleLogin}
            disabled={view.submitting}
            autoComplete="on"
          >
            <Form.Item
              name="username"
              label="用户名"
              rules={[{ required: true, message: '请输入用户名' }]}
            >
              <Input autoComplete="username" />
            </Form.Item>
            <Form.Item
              name="password"
              label="密码"
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password autoComplete="current-password" />
            </Form.Item>
            {view.error && (
              <Alert type="error" showIcon message={view.error} style={{ marginBottom: 16 }} />
            )}
            <Button type="primary" htmlType="submit" loading={view.submitting} block>
              登录
            </Button>
          </Form>
        </Card>
      </main>
    )
  }

  // antd App 提供 message 等反馈组件的上下文，运营页面统一经它提示。
  // 运营页在 /admin 路径树内按嵌套路由切换：用户管理（含某用户流水深链
  // /admin/users/:id）与全局流水。401 统一回登录表单，与具体页面无关。
  return (
    <AntApp>
      <Layout style={{ minHeight: '100vh' }}>
        <Layout.Header style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <Typography.Title level={4} style={{ color: '#fff', margin: 0 }}>
            AI-Polish 管理后台
          </Typography.Title>
          <Space size="middle">
            <NavLink
              to="/admin"
              end
              style={({ isActive }) => ({
                color: isActive ? '#fff' : 'rgba(255,255,255,0.65)',
              })}
            >
              用户管理
            </NavLink>
            <NavLink
              to="/admin/ledger"
              style={({ isActive }) => ({
                color: isActive ? '#fff' : 'rgba(255,255,255,0.65)',
              })}
            >
              全局流水
            </NavLink>
            <NavLink
              to="/admin/sessions"
              style={({ isActive }) => ({
                color: isActive ? '#fff' : 'rgba(255,255,255,0.65)',
              })}
            >
              会话监控
            </NavLink>
            <NavLink
              to="/admin/config"
              style={({ isActive }) => ({
                color: isActive ? '#fff' : 'rgba(255,255,255,0.65)',
              })}
            >
              系统配置
            </NavLink>
          </Space>
          <span style={{ color: 'rgba(255,255,255,0.65)', marginLeft: 'auto' }}>
            {view.username}
          </span>
          <Button onClick={handleLogout}>退出登录</Button>
        </Layout.Header>
        <Layout.Content style={{ padding: 24 }}>
          <Routes>
            <Route index element={<UsersPage />} />
            <Route path="users/:userId" element={<LedgerPage />} />
            <Route path="ledger" element={<LedgerPage />} />
            <Route path="sessions" element={<SessionsPage />} />
            <Route path="sessions/:sessionId" element={<SessionsPage />} />
            <Route path="config" element={<ConfigPage />} />
            {/* 提示词管理等其余运营功能由 M3 后续票接入 */}
          </Routes>
        </Layout.Content>
      </Layout>
    </AntApp>
  )
}
