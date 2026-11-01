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
 *  终态同形，前端一次合并即得完整展示态。chunk_index/chunk_total 只有预处理
 *  任务的标记段有值（第几块/共几块），格式化任务恒 null。 */
export interface JobStreamState {
  status: string
  phase: string | null
  progress: number | null
  message: string | null
  chunk_index: number | null
  chunk_total: number | null
  error: string | null
  output_filename: string | null
}

/** 排版选项（CompileOptions 的页面形态；自定义规格 JSON 来自规格生成页 #36，
 *  服务端先校验再创建——坏规格 422 挡在预留之前）。预处理送来的文本带
 *  wf:type 结构标记，必须按 marked 输入渲染。 */
export interface FormatOptions {
  input_format?: 'auto' | 'markdown' | 'plaintext' | 'marked'
  spec_name?: string
  custom_spec?: Record<string, unknown>
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

// ---- 文章预处理（backend/app/word_preprocess.py 的端点，issue #35） ----

/** 预处理分块参数：决定 AI 标记的分块粒度（护栏在服务端，边界外 422）。 */
export interface PreprocessOptions {
  chunk_paragraphs?: number
  chunk_chars?: number
}

export function createPreprocessTextJob(
  cardKey: string,
  text: string,
  options: PreprocessOptions,
): Promise<JobView> {
  return fetchJson<JobView>('/api/word-formatter/preprocess/text', cardKey, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, ...options }),
  })
}

/** 上传预处理：解码（GBK 回落）与 docx 提文本都在服务端，这里只交原文件。 */
export function createPreprocessFileJob(
  cardKey: string,
  file: File,
  options: PreprocessOptions,
): Promise<JobView> {
  const form = new FormData()
  form.append('file', file)
  if (options.chunk_paragraphs !== undefined) {
    form.append('chunk_paragraphs', String(options.chunk_paragraphs))
  }
  if (options.chunk_chars !== undefined) {
    form.append('chunk_chars', String(options.chunk_chars))
  }
  return fetchJson<JobView>('/api/word-formatter/preprocess/file', cardKey, {
    method: 'POST',
    body: form,
  })
}

/** 预处理段落明细（结果端点 paragraphs 元素）。 */
export interface PreprocessParagraphView {
  index: number
  text: string
  paragraph_type: string | null
  confidence: number
  is_rule_identified: boolean
}

/** 预处理结果（GET /preprocess/{id}/result）：标记文本是一键送格式化的
 *  直接输入，integrity_check_passed 是"标记后与原文一致"的校验结论。 */
export interface PreprocessResultView {
  marked_text: string
  paragraphs: PreprocessParagraphView[]
  type_statistics: Record<string, number>
  integrity_check_passed: boolean
  warnings: string[]
}

export function getPreprocessResult(
  cardKey: string,
  jobId: number,
): Promise<PreprocessResultView> {
  return fetchJson<PreprocessResultView>(
    `/api/word-formatter/preprocess/${jobId}/result`,
    cardKey,
  )
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

// ---- 格式检查（backend/app/word_check.py 的端点，issue #37） ----

/** 检查档位：宽松只报 ERROR 级，严格报全部问题。 */
export type CheckMode = 'loose' | 'strict'

/** 段落类型字典条目（GET /format-check/types）：检查结果与标记文本的术语表。 */
export interface ParagraphTypeView {
  type: string
  label: string
}

/** 单条格式问题：行号定位 + 严重度分级 + 问题类型（页面按行号排明细）。 */
export interface FormatIssueView {
  line: number
  paragraph_index: number
  issue_type: string
  severity: 'error' | 'warning' | 'info'
  message: string
  suggestion: string
  content_preview: string
}

/** 检查结果（同步免费：不创建任务、不耗次数）。 */
export interface FormatCheckResultView {
  is_valid: boolean
  mode: string
  issues: FormatIssueView[]
  type_statistics: Record<string, number>
}

/** 段落类型字典与说明（检查结果术语的唯一数据源）。 */
export function getFormatCheckTypes(cardKey: string): Promise<ParagraphTypeView[]> {
  return fetchJson<{ types: ParagraphTypeView[] }>(
    '/api/word-formatter/format-check/types',
    cardKey,
  ).then((body) => body.types)
}

export function checkFormatText(
  cardKey: string,
  text: string,
  mode: CheckMode,
): Promise<FormatCheckResultView> {
  return fetchJson<FormatCheckResultView>('/api/word-formatter/format-check/text', cardKey, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, mode }),
  })
}

/** 上传检查：解码（GBK 回落）与 docx 提文本都在服务端，这里只交原文件。 */
export function checkFormatFile(
  cardKey: string,
  file: File,
  mode: CheckMode,
): Promise<FormatCheckResultView> {
  const form = new FormData()
  form.append('file', file)
  form.append('mode', mode)
  return fetchJson<FormatCheckResultView>('/api/word-formatter/format-check/file', cardKey, {
    method: 'POST',
    body: form,
  })
}

// ---- 排版规格（backend/app/word_specs.py 的端点，issue #36） ----

/** AI 生成响应：spec_json 是规格 JSON 全文（保存与送格式化的直接输入）。 */
export interface GeneratedSpec {
  success: boolean
  spec_json: string
  spec_name: string
}

/** 已存规格（SavedSpecOut）：spec_json 全文回传，编辑复用零转换。 */
export interface SavedSpecView {
  id: number
  name: string
  description: string | null
  spec_json: string
  created_at: string
  updated_at: string
}

/** AI 同步生成排版规格（预留 1 次，失败服务端自动退回；未配置 503）。 */
export function generateSpec(
  cardKey: string,
  requirements: string,
): Promise<GeneratedSpec> {
  return fetchJson<GeneratedSpec>('/api/word-formatter/specs/generate', cardKey, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requirements }),
  })
}

/** 规格合法性校验：合法返回 valid 与规格名；坏 JSON / 非法规格 400
 *  （ApiError 携带 detail）。 */
export function validateSpec(
  cardKey: string,
  specJson: string,
): Promise<{ valid: boolean; spec_name: string }> {
  return fetchJson<{ valid: boolean; spec_name: string }>(
    '/api/word-formatter/specs/validate',
    cardKey,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ spec_json: specJson }),
    },
  )
}

/** 保存为本人常用规格：同名覆盖（更新而非新建），坏规格 400 挡在写入前。 */
export function saveSpec(
  cardKey: string,
  name: string,
  specJson: string,
  description?: string,
): Promise<SavedSpecView> {
  return fetchJson<SavedSpecView>('/api/word-formatter/specs/save', cardKey, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, spec_json: specJson, description: description ?? null }),
  })
}

/** 本人全部已存规格（最近更新在前）。 */
export function listSavedSpecs(cardKey: string): Promise<SavedSpecView[]> {
  return fetchJson<SavedSpecView[]>('/api/word-formatter/specs/saved', cardKey)
}

export function deleteSavedSpec(cardKey: string, specId: number): Promise<void> {
  return fetchJson<void>(`/api/word-formatter/specs/saved/${specId}`, cardKey, {
    method: 'DELETE',
  })
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
  running: '处理中',
  completed: '已完成',
  failed: '失败',
}

export const JOB_TYPE_LABELS: Record<string, string> = {
  format: '文档排版',
  preprocess: '文章预处理',
}

/** 任务状态徽标文案：running 按任务类型给具体动词（排版中/标记中）。 */
export function jobStatusLabel(job: Pick<JobView, 'job_type' | 'status'>): string {
  if (job.status === 'running') {
    return job.job_type === 'preprocess' ? '标记中' : '排版中'
  }
  return JOB_STATUS_LABELS[job.status] ?? job.status
}

export const JOB_PHASE_LABELS: Record<string, string> = {
  compile: '编译文档',
  splitting: '分割段落',
  marking: 'AI 标记',
  validating: '一致性校验',
  done: '完成',
  failed: '失败',
}

/** 预处理段落类型文案（backend PreprocessResult 的 VALID_PARAGRAPH_TYPES）。 */
export const PARAGRAPH_TYPE_LABELS: Record<string, string> = {
  title_cn: '中文标题',
  title_en: '英文标题',
  abstract_cn: '中文摘要',
  abstract_en: '英文摘要',
  keywords_cn: '中文关键词',
  keywords_en: '英文关键词',
  heading_1: '一级标题',
  heading_2: '二级标题',
  heading_3: '三级标题',
  body: '正文',
  reference: '参考文献',
  acknowledgement: '致谢',
  figure_caption: '图题',
  table_caption: '表题',
}

/** 问题严重度分级（backend IssueSeverity）：宽松档只报 error。 */
export const ISSUE_SEVERITY_LABELS: Record<string, string> = {
  error: '错误',
  warning: '警告',
  info: '提示',
}

/** 问题类型文案（backend IssueType）。 */
export const ISSUE_TYPE_LABELS: Record<string, string> = {
  heading_skip: '标题层级跳跃',
  heading_duplicate: '重复标题层级',
  missing_abstract: '缺少摘要',
  missing_keywords: '缺少关键词',
  invalid_list: '无效列表格式',
  empty_paragraph: '空段落',
  long_paragraph: '段落过长',
  reference_format: '参考文献格式问题',
  existing_marker: '已有段落标记',
}

export function formatYuan(balanceFen: number): string {
  return (balanceFen / 100).toFixed(2)
}
