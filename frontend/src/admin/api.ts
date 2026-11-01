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
      unauthorizedHandler?.()
      throw new AdminUnauthorized(response.status, message)
    }
    throw new AdminError(response.status, message)
  }
  return (await response.json()) as T
}

let unauthorizedHandler: (() => void) | null = null

/**
 * 注册会话过期回调（AdminApp 挂载时注册一次）：此后任何管理接口返回 401
 * 都会触发它（业务页面沿用本模块的调用方式即可获得统一的回登录行为，
 * 不需要各自处理 401）。返回注销函数。
 */
export function onUnauthorized(handler: () => void): () => void {
  unauthorizedHandler = handler
  return () => {
    if (unauthorizedHandler === handler) unauthorizedHandler = null
  }
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

// ---- 用户管理（发卡 + 列表/搜索 + 双轨额度调整，issue #26）----

export interface IssuedCardKey {
  card_key: string
  balance_fen: number
  usage_limit: number
}

/** 批量发卡：完整卡密只在本次响应中出现，返回后没有任何接口能再取到。 */
export function issueCardKeys(params: {
  count: number
  balance_fen: number
  usage_limit: number
}): Promise<{ card_keys: IssuedCardKey[] }> {
  return request('/api/admin/card-keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
}

export interface AdminUser {
  id: number
  card_key: string
  balance_fen: number
  usage_limit: number
  usage_remaining: number
}

export interface AdminUserPage {
  items: AdminUser[]
  total: number
  page: number
  page_size: number
}

export interface ListUsersParams {
  page: number
  page_size: number
  /** 卡密片段或用户 ID，后端同一个参数两种都能命中；空串/纯空白表示不过滤。 */
  q?: string
}

/**
 * 用户分页列表。列表中的卡密是脱敏形态；搜索词（卡密片段）走 query——
 * 片段不是凭据（无法凭它认证），而完整卡密/密码仍绝不进 URL。
 */
export function listUsers(params: ListUsersParams): Promise<AdminUserPage> {
  const query = new URLSearchParams({
    page: String(params.page),
    page_size: String(params.page_size),
  })
  const q = params.q?.trim()
  if (q) query.set('q', q)
  return request(`/api/admin/users?${query.toString()}`)
}

/** 充值/扣减余额：deltaFen 为正即充值，为负即扣减，0 由 UI 排除（后端 422 兜底）。 */
export function adjustUserBalance(
  userId: number,
  params: { delta_fen: number; reason: string },
): Promise<{ user_id: number; balance_fen: number }> {
  return request(`/api/admin/users/${userId}/balance`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
}

/** 增减次数额度：delta 为正即增加上限，为负即调低，0 由 UI 排除（后端 422 兜底）。 */
export function adjustUserUsage(
  userId: number,
  params: { delta: number; reason: string },
): Promise<{ user_id: number; usage_limit: number; usage_remaining: number }> {
  return request(`/api/admin/users/${userId}/usage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
}

// ---- 账本流水查询（issue #27，只读接口）----

/** 账本行：kind 合法取值由后端计费服务拥有；amount_fen 为带符号增量。 */
export interface AdminLedgerEntry {
  id: number
  user_id: number
  task_id: number | null
  kind: string
  amount_fen: number
  balance_after_fen: number
  usage_after: number | null
  reason: string
  created_at: string
}

export interface AdminLedgerPage {
  items: AdminLedgerEntry[]
  total: number
  page: number
  page_size: number
  /** 仅按用户查询时返回；card_key 为脱敏形态，供流水页标题展示。 */
  user_id?: number
  card_key?: string
}

/** 全局最近流水（新流水在前）。 */
export function listRecentLedger(params: { page: number; page_size: number }): Promise<AdminLedgerPage> {
  const query = new URLSearchParams({
    page: String(params.page),
    page_size: String(params.page_size),
  })
  return request(`/api/admin/ledger?${query.toString()}`)
}

/** 某用户的全部账本流水（新流水在前）。 */
export function listUserLedger(
  userId: number,
  params: { page: number; page_size: number },
): Promise<AdminLedgerPage> {
  const query = new URLSearchParams({
    page: String(params.page),
    page_size: String(params.page_size),
  })
  return request(`/api/admin/users/${userId}/ledger?${query.toString()}`)
}

// ---- 会话监控（issue #28，只读接口）----

export type AdminSessionStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'

/** 会话列表行：card_key 为所属用户的脱敏卡密。 */
export interface AdminSessionSummary {
  id: number
  user_id: number
  card_key: string
  status: AdminSessionStatus
  mode: string
  word_count: number
  charged_fen: number
  model_snapshot: string | null
  created_at: string
}

export interface AdminSessionPage {
  items: AdminSessionSummary[]
  total: number
  page: number
  page_size: number
}

export interface AdminSegmentStatus {
  stage: string
  seq: number
  status: string
}

/** 该会话的账本行：kind 只有 charge（预扣）与 refund（退款），退款行存在即已落账。 */
export interface AdminSessionLedgerRow {
  id: number
  kind: string
  amount_fen: number
  balance_after_fen: number
  reason: string
  created_at: string
}

export interface AdminSessionDetail extends AdminSessionSummary {
  updated_at: string
  segments: AdminSegmentStatus[]
  ledger: AdminSessionLedgerRow[]
}

/**
 * 会话分页列表，可按状态与所属用户过滤（均省略 = 不过滤）。只读接口：
 * 不触发任何状态迁移或退款，页面看到的就是数据库此刻的状态。
 */
export function listSessions(params: {
  page: number
  page_size: number
  status?: AdminSessionStatus
  user_id?: number
}): Promise<AdminSessionPage> {
  const query = new URLSearchParams({
    page: String(params.page),
    page_size: String(params.page_size),
  })
  if (params.status) query.set('status', params.status)
  if (params.user_id !== undefined) query.set('user_id', String(params.user_id))
  return request(`/api/admin/sessions?${query.toString()}`)
}

/** 单个会话的监控详情（model_snapshot + 段落阶段状态 + 该会话账本行）。 */
export function getAdminSessionDetail(sessionId: number): Promise<AdminSessionDetail> {
  return request(`/api/admin/sessions/${sessionId}`)
}
