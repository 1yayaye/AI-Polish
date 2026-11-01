import { useEffect, useState } from 'react'
import { Alert, Button, Card, Form, Input, Layout, Spin, Typography } from 'antd'
import { AdminError, adminLogin, adminLogout, getAdminSession } from './api'

/**
 * 管理后台外壳：/admin 路径树整棵渲染在这里。三种视图——
 * checking（启动校验残留会话）→ login（登录表单）→ ready（后台外壳）。
 *
 * 401 汇聚点：会话 cookie 缺失/过期（12 小时 TTL）时后端一律 401，ready
 * 态下任何 AdminUnauthorized 都统一回登录表单——后续接入的运营页面（发卡、
 * 充值、会话监控等）沿用 ./api 的调用方式即可获得同一行为，不需要各自处理。
 * 登录表单里的 401 不是会话过期而是凭据错误，照常展示后端给的原因。
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

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Layout.Header style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <Typography.Title level={4} style={{ color: '#fff', margin: 0 }}>
          AI-Polish 管理后台
        </Typography.Title>
        <span style={{ color: 'rgba(255,255,255,0.65)', marginLeft: 'auto' }}>
          {view.username}
        </span>
        <Button onClick={handleLogout}>退出登录</Button>
      </Layout.Header>
      <Layout.Content style={{ padding: 24 }}>
        {/* 占位首页：发卡/充值/流水/会话监控等运营功能由 M3 后续票接入 */}
        <Card>
          <Typography.Text type="secondary">
            管理功能建设中，本页为占位首页。
          </Typography.Text>
        </Card>
      </Layout.Content>
    </Layout>
  )
}
