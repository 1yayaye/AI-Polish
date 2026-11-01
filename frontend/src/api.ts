/**
 * 后端 API 访问：卡密只经 X-Card-Key 请求头传递，绝不进 URL
 * （SSE 流的连接见 useSessionStream.ts，同一约定）。
 */

export interface Quota {
  balance_fen: number
  usage_remaining: number
}

export interface SegmentView {
  stage: string
  seq: number
  status: string
}

/** SSE snapshot / done 事件载荷（backend/app/stream.py 的事件协议）。 */
export interface SessionSnapshot {
  status: string
  segments: SegmentView[]
}

/** 变更记录（GLOSSARY）：某段落经润色或增强前后的对照。 */
export interface ChangeEntry {
  stage: string
  seq: number
  before_text: string
  after_text: string
}

export interface SessionView {
  id: number
  mode: string
  status: string
  word_count: number
  charged_fen: number
  segments?: SegmentView[]
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

async function parseError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string }
    return body.detail ?? `请求失败（HTTP ${response.status}）`
  } catch {
    return `请求失败（HTTP ${response.status}）`
  }
}

export async function fetchJson<T>(
  url: string,
  cardKey: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { 'X-Card-Key': cardKey, ...(init?.headers ?? {}) },
  })
  if (!response.ok) {
    throw new ApiError(response.status, await parseError(response))
  }
  return (await response.json()) as T
}

export function getQuota(cardKey: string): Promise<Quota> {
  return fetchJson<Quota>('/api/me/quota', cardKey)
}

export function createSession(
  cardKey: string,
  text: string,
  mode: string,
): Promise<SessionView> {
  return fetchJson<SessionView>('/api/optimization/sessions', cardKey, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, mode }),
  })
}

/**
 * 本人全部会话（backend SessionSummaryOut，最新在前）：
 * 字段即 SessionView 去掉 segments——刷新恢复只用于挑出活跃会话，
 * 段落进度由 SSE snapshot 补齐，这里不重复承载。
 */
export function listSessions(cardKey: string): Promise<SessionView[]> {
  return fetchJson<SessionView[]>('/api/optimization/sessions', cardKey)
}

export function cancelSession(cardKey: string, sessionId: number): Promise<SessionView> {
  return fetchJson<SessionView>(`/api/optimization/sessions/${sessionId}/cancel`, cardKey, {
    method: 'POST',
  })
}

export function getChanges(cardKey: string, sessionId: number): Promise<ChangeEntry[]> {
  return fetchJson<ChangeEntry[]>(`/api/optimization/sessions/${sessionId}/changes`, cardKey)
}

/**
 * 导出下载：导出端点同样只认 header 认证，浏览器下载不能带 header，
 * 因此经 fetch 取回字节流再触发保存（绝不把卡密拼进 href）。
 */
export async function downloadExport(
  cardKey: string,
  sessionId: number,
  format: 'txt' | 'docx',
): Promise<void> {
  const response = await fetch(`/api/optimization/sessions/${sessionId}/export.${format}`, {
    headers: { 'X-Card-Key': cardKey },
  })
  if (!response.ok) {
    throw new ApiError(response.status, await parseError(response))
  }
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `polish-${sessionId}.${format}`
    anchor.click()
  } finally {
    URL.revokeObjectURL(url)
  }
}

export const MODE_LABELS: Record<string, string> = {
  paper_polish: '润色',
  paper_enhance: '原创性增强',
  paper_polish_enhance: '两阶段全流程',
}

export const STAGE_LABELS: Record<string, string> = {
  polish: '润色',
  enhance: '增强',
}

export const SESSION_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '处理中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

export function formatYuan(balanceFen: number): string {
  return (balanceFen / 100).toFixed(2)
}
