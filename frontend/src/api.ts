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
  if (response.status === 204) {
    return undefined as T
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
 * 下载保存的公共路径：下载端点只认 header 认证，浏览器下载不能带 header，
 * 因此经 fetch 取回字节流再触发保存（绝不把卡密拼进 href）。
 */
async function downloadBlob(url: string, cardKey: string, filename: string): Promise<void> {
  const response = await fetch(url, {
    headers: { 'X-Card-Key': cardKey },
  })
  if (!response.ok) {
    throw new ApiError(response.status, await parseError(response))
  }
  const blob = await response.blob()
  const url2 = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = url2
    anchor.download = filename
    anchor.click()
  } finally {
    URL.revokeObjectURL(url2)
  }
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
  await downloadBlob(`/api/optimization/sessions/${sessionId}/export.${format}`, cardKey, `polish-${sessionId}.${format}`)
}

// ---- Word Formatter（backend/app/word_jobs.py 的任务端点，issue #33/#34） ----

/** 任务状态响应（JobOut）：不含 input_text（可能整篇文档，列表页不需要）。 */
export interface JobView {
  id: number
  job_type: string
  status: string
  phase: string | null
  progress: number | null
  message: string | null
  input_file_name: string | null
  output_filename: string | null
  error: string | null
  created_at: string
  updated_at: string
}

/** 任务进度流事件载荷（backend/app/word_stream.py）：snapshot / progress /
 *  终态同形，前端一次合并即得完整展示态。 */
export interface JobStreamState {
  status: string
  phase: string | null
  progress: number | null
  message: string | null
  error: string | null
  output_filename: string | null
}

/** 排版选项（CompileOptions 的页面形态；自定义规格 JSON 归规格生成页 #36）。 */
export interface FormatOptions {
  spec_name?: string
  include_cover?: boolean
  include_toc?: boolean
  toc_title?: string
}

export function createFormatTextJob(
  cardKey: string,
  text: string,
  options: FormatOptions,
): Promise<JobView> {
  return fetchJson<JobView>('/api/word-formatter/format/text', cardKey, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, ...options }),
  })
}

/** 上传排版：解码（GBK 回落）与 docx 提文本都在服务端，这里只交原文件。 */
export function createFormatFileJob(
  cardKey: string,
  file: File,
  options: FormatOptions,
): Promise<JobView> {
  const form = new FormData()
  form.append('file', file)
  if (options.spec_name) form.append('spec_name', options.spec_name)
  form.append('include_cover', String(options.include_cover ?? true))
  form.append('include_toc', String(options.include_toc ?? true))
  return fetchJson<JobView>('/api/word-formatter/format/file', cardKey, {
    method: 'POST',
    body: form,
  })
}

/** 本人全部任务（最新在前）：刷新恢复只用于挑出活跃任务，进度由 SSE 补齐。 */
export function listJobs(cardKey: string): Promise<JobView[]> {
  return fetchJson<JobView[]>('/api/word-formatter/jobs', cardKey)
}

export function deleteJob(cardKey: string, jobId: number): Promise<void> {
  return fetchJson<void>(`/api/word-formatter/jobs/${jobId}`, cardKey, {
    method: 'DELETE',
  })
}

/** 内置排版规格名列表（GET /specs，格式化页规格选择器的数据源）。 */
export function getBuiltinSpecs(cardKey: string): Promise<string[]> {
  return fetchJson<{ specs: string[] }>('/api/word-formatter/specs', cardKey).then(
    (body) => body.specs,
  )
}

/** 任务产物下载：与导出下载同一 header 认证 + blob 保存路径。 */
export function downloadJobOutput(
  cardKey: string,
  jobId: number,
  filename: string,
): Promise<void> {
  return downloadBlob(`/api/word-formatter/jobs/${jobId}/download`, cardKey, filename)
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

export const JOB_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '排版中',
  completed: '已完成',
  failed: '失败',
}

export const JOB_TYPE_LABELS: Record<string, string> = {
  format: '文档排版',
  preprocess: '文章预处理',
}

export const JOB_PHASE_LABELS: Record<string, string> = {
  compile: '编译文档',
  done: '完成',
  failed: '失败',
}

export function formatYuan(balanceFen: number): string {
  return (balanceFen / 100).toFixed(2)
}
