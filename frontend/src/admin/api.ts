/**
 * 管理后台 API 访问。凭据（用户名/密码）只经 POST body 传输，绝不进
 * URL/query——进 URL 的参数会落入 Nginx 访问日志（与用户端卡密同一纪律）。
 * 会话由后端签发的 HttpOnly cookie 承载，这里不读也不写 cookie。
 *
 * 401 纪律：管理接口对"未登录/会话过期/凭据未配置"一律返回 401 且不区分
 * 原因（backend/app/admin.py）；本模块把 401 归一为 AdminUnauthorized 抛出，
 * 由 AdminApp 统一处理为回到登录表单，调用方不需要各自判 401。
 */

export interface AdminSession {
  username: string
}

export class AdminError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

/** 会话缺失或已过期的信号：只有 AdminApp 捕获它，统一回登录表单。 */
export class AdminUnauthorized extends AdminError {}

async function parseError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string }
    return body.detail ?? `请求失败（HTTP ${response.status}）`
  } catch {
    return `请求失败（HTTP ${response.status}）`
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { ...init, credentials: 'same-origin' })
  if (!response.ok) {
    const message = await parseError(response)
    if (response.status === 401) {
      throw new AdminUnauthorized(response.status, message)
    }
    throw new AdminError(response.status, message)
  }
  return (await response.json()) as T
}

/** 启动时校验残留会话：200 即已登录（刷新深链不丢登录态），401 即回登录表单。 */
export function getAdminSession(): Promise<AdminSession> {
  return request<AdminSession>('/api/admin/session')
}

export function adminLogin(username: string, password: string): Promise<AdminSession> {
  return request<AdminSession>('/api/admin/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
}

export function adminLogout(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>('/api/admin/logout', { method: 'POST' })
}
