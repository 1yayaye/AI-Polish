import { useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ApiError,
  deleteSavedSpec,
  generateSpec,
  listSavedSpecs,
  saveSpec,
  type SavedSpecView,
} from '../api'

/**
 * 排版规格页（issue #36，GLOSSARY：排版规格；旧版 SpecGenerator 页的 TS 迁移）：
 * 自然语言描述（或预置模板）→ AI 同步生成规格（预留 1 次，失败服务端退回；
 * 未配置 503）→ 三视图预览（结构/表格/JSON，可编辑）→ 保存为常用规格（同名
 * 覆盖）→ 送格式化页一次性使用。已存规格可加载回编辑器或删除。
 */

const PRESET_TEMPLATES = [
  {
    id: 'undergraduate_thesis',
    name: '本科毕业论文',
    description: '符合大多数高校本科毕业论文格式要求',
    requirements:
      '本科毕业论文格式：标题三号黑体居中，摘要四号宋体，正文小四号宋体1.5倍行距，一级标题三号黑体，二级标题四号黑体，三级标题小四号黑体，参考文献五号宋体，页边距上下2.54cm左右3.17cm',
  },
  {
    id: 'master_thesis',
    name: '硕士学位论文',
    description: '符合研究生院学位论文格式规范',
    requirements:
      '硕士学位论文格式：封面标题二号黑体，摘要小四号宋体，正文小四号宋体1.5倍行距，章标题三号黑体居中，节标题四号黑体，段落首行缩进2字符，参考文献五号宋体悬挂缩进，页边距上下2.54cm左右3cm',
  },
  {
    id: 'journal_paper',
    name: '期刊论文',
    description: '通用学术期刊论文格式',
    requirements:
      '期刊论文格式：标题三号黑体居中，作者信息五号宋体居中，摘要小五号宋体，关键词小五号宋体，正文五号宋体单倍行距，一级标题四号黑体，图表标题小五号宋体居中，参考文献小五号宋体',
  },
] as const

/** 三视图的展示形态：规格 JSON 归渲染库 StyleSpec 拥有，页面只读展示关心的
 *  字段（解析容错——AI 或手写的 JSON 都按缺省回退，不让坏值崩页面）。 */
interface SpecShape {
  meta?: { name?: string; version?: string; notes?: string }
  page?: {
    size?: string
    margins_mm?: { top?: number; bottom?: number; left?: number; right?: number }
  }
  styles?: Record<
    string,
    {
      name?: string
      run?: {
        size_pt?: number
        font?: { eastAsia?: string; ascii?: string }
      }
      paragraph?: {
        alignment?: string
        line_spacing_rule?: string
        first_line_indent_chars?: number
      }
    }
  >
  structure?: { required_h1_titles?: string[]; toc_max_level?: number }
}

function parseSpec(specJson: string): SpecShape | null {
  try {
    return JSON.parse(specJson) as SpecShape
  } catch {
    return null
  }
}

type ViewMode = 'structure' | 'table' | 'json'

const VIEW_MODE_LABELS: Record<ViewMode, string> = {
  structure: '结构',
  table: '表格',
  json: 'JSON',
}

interface Props {
  cardKey: string
  refreshQuota: () => Promise<void>
}

export default function SpecGeneratorPage({ cardKey, refreshQuota }: Props) {
  const navigate = useNavigate()
  const [requirements, setRequirements] = useState('')
  const [specName, setSpecName] = useState('')
  const [specDescription, setSpecDescription] = useState('')
  const [specJson, setSpecJson] = useState<string | null>(null)
  const [viewMode, setViewMode] = useState<ViewMode>('structure')
  const [editing, setEditing] = useState(false)
  const [editedJson, setEditedJson] = useState('')
  const [savedSpecs, setSavedSpecs] = useState<SavedSpecView[]>([])
  const [generating, setGenerating] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    listSavedSpecs(cardKey)
      .then((specs) => {
        if (!cancelled) setSavedSpecs(specs)
      })
      .catch(() => {}) // 已存规格加载失败不挡生成，重新进页再取
    return () => {
      cancelled = true
    }
  }, [cardKey])

  /** 当前工作规格：编辑态用编辑缓冲，否则用生成/加载的规格。 */
  const workingJson = editing ? editedJson : specJson

  function handleSelectTemplate(requirementsText: string, name: string, description: string) {
    setRequirements(requirementsText)
    setSpecName(name)
    setSpecDescription(description)
  }

  async function handleGenerate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setGenerating(true)
    try {
      const body = await generateSpec(cardKey, requirements)
      setSpecJson(body.spec_json)
      setEditing(false)
      if (!specName) setSpecName(body.spec_name)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    } finally {
      setGenerating(false)
      void refreshQuota() // 预留成功不退、失败已退回，两种终态额度都变了
    }
  }

  function handleStartEdit() {
    if (specJson === null) return
    setEditedJson(specJson)
    setEditing(true)
  }

  function handleFinishEdit() {
    if (parseSpec(editedJson) === null) {
      setError('JSON 格式无效，请检查后再保存')
      return
    }
    setError('')
    setSpecJson(editedJson)
    setEditing(false)
  }

  async function handleSave() {
    if (workingJson === null) return
    if (specName.trim() === '') {
      setError('请输入规格名称')
      return
    }
    setError('')
    setSaving(true)
    try {
      await saveSpec(cardKey, specName.trim(), workingJson, specDescription || undefined)
      setSavedSpecs(await listSavedSpecs(cardKey))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    } finally {
      setSaving(false)
    }
  }

  function handleLoadSpec(spec: SavedSpecView) {
    setSpecJson(spec.spec_json)
    setEditing(false)
    setSpecName(spec.name)
    setSpecDescription(spec.description ?? '')
  }

  async function handleDeleteSpec(spec: SavedSpecView) {
    if (!window.confirm(`删除已存规格「${spec.name}」？`)) return
    setError('')
    try {
      await deleteSavedSpec(cardKey, spec.id)
      setSavedSpecs((prev) => prev.filter((s) => s.id !== spec.id))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '网络错误，请稍后重试')
    }
  }

  function handleSendToFormatter() {
    // 送格式化页一次性使用：规格 JSON 全文经路由 state 交接（与预处理送
    // 标记文本同一衔接方式），格式化页提交时按 custom_spec 上送。
    if (workingJson === null || parseSpec(workingJson) === null) {
      setError('当前不是合法的规格 JSON，无法送格式化')
      return
    }
    setError('')
    navigate('/word', { state: { customSpecJson: workingJson } })
  }

  function handleExportJson() {
    if (workingJson === null) return
    const blob = new Blob([workingJson], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    try {
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${specName || 'spec'}.json`
      anchor.click()
    } finally {
      URL.revokeObjectURL(url)
    }
  }

  const spec = workingJson !== null ? parseSpec(workingJson) : null

  return (
    <main className="workspace">
      <section className="panel">
        <h2>AI 生成排版规格</h2>
        <p className="hint">
          用自然语言描述排版要求（页边距、字体字号、行距、缩进……），AI
          生成规格 JSON。生成预留 1 次，失败自动退回。
        </p>
        <div className="option-row">
          {PRESET_TEMPLATES.map((template) => (
            <button
              key={template.id}
              type="button"
              onClick={() =>
                handleSelectTemplate(template.requirements, template.name, template.description)
              }
            >
              {template.name}
            </button>
          ))}
        </div>
        <form onSubmit={handleGenerate}>
          <textarea
            value={requirements}
            onChange={(e) => setRequirements(e.target.value)}
            placeholder="例如：标题三号黑体居中，正文小四宋体 1.5 倍行距首行缩进 2 字符，页边距上下 2.54cm 左右 3.17cm……"
            rows={6}
            required
          />
          <div className="submit-row">
            <button type="submit" disabled={generating}>
              {generating ? '生成中…' : '生成规格'}
            </button>
            <span className="hint">{requirements.length} 字符</span>
          </div>
        </form>
      </section>

      {error && <p className="error">{error}</p>}

      {specJson !== null && (
        <section className="panel">
          <h2>规格预览</h2>
          <div className="option-row">
            <label>
              规格名称 <input value={specName} onChange={(e) => setSpecName(e.target.value)} />
            </label>
            <label>
              描述{' '}
              <input
                value={specDescription}
                onChange={(e) => setSpecDescription(e.target.value)}
                placeholder="可选"
              />
            </label>
          </div>
          <div className="mode-picker" role="radiogroup" aria-label="预览视图">
            {(Object.keys(VIEW_MODE_LABELS) as ViewMode[]).map((mode) => (
              <label key={mode} className={`mode ${viewMode === mode ? 'checked' : ''}`}>
                <input
                  type="radio"
                  name="spec-view"
                  checked={viewMode === mode}
                  onChange={() => setViewMode(mode)}
                />
                {VIEW_MODE_LABELS[mode]}
              </label>
            ))}
            {viewMode === 'json' && (
              <button type="button" className="link-button" onClick={editing ? handleFinishEdit : handleStartEdit}>
                {editing ? '完成编辑' : '编辑 JSON'}
              </button>
            )}
          </div>
          <div className="spec-preview">
            {viewMode === 'structure' && <SpecStructureView spec={spec} />}
            {viewMode === 'table' && <SpecTableView spec={spec} />}
            {viewMode === 'json' &&
              (editing ? (
                <textarea
                  value={editedJson}
                  onChange={(e) => setEditedJson(e.target.value)}
                  rows={16}
                  className="spec-json-editor"
                />
              ) : (
                <pre>{spec !== null ? JSON.stringify(spec, null, 2) : workingJson}</pre>
              ))}
          </div>
          <div className="actions">
            <button type="button" onClick={() => void handleSave()} disabled={saving}>
              {saving ? '保存中…' : '保存规格'}
            </button>
            <button type="button" className="link-button" onClick={handleExportJson}>
              导出 JSON
            </button>
            <button type="button" onClick={handleSendToFormatter}>
              送格式化（一次性使用）
            </button>
          </div>
        </section>
      )}

      <section className="panel">
        <h2>我的已存规格</h2>
        {savedSpecs.length === 0 ? (
          <p className="hint">还没有已存规格，生成并保存一个吧。</p>
        ) : (
          <table className="spec-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>描述</th>
                <th>更新时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {savedSpecs.map((spec2) => (
                <tr key={spec2.id}>
                  <td>{spec2.name}</td>
                  <td>{spec2.description ?? '—'}</td>
                  <td>{new Date(spec2.updated_at).toLocaleString()}</td>
                  <td className="job-actions">
                    <button type="button" className="link-button" onClick={() => handleLoadSpec(spec2)}>
                      加载
                    </button>
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => void handleDeleteSpec(spec2)}
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  )
}

/** 结构视图：页面设置与结构约束（样式明细归表格视图）。 */
function SpecStructureView({ spec }: { spec: SpecShape | null }) {
  if (spec === null) return <p className="error">JSON 解析失败</p>
  const margins = spec.page?.margins_mm
  return (
    <div>
      <p className="meta">
        名称：{spec.meta?.name ?? '—'}
        {spec.meta?.notes ? ` · ${spec.meta.notes}` : ''}
      </p>
      <p className="meta">
        纸张：{spec.page?.size ?? '—'} · 页边距（mm）：上 {margins?.top ?? '—'} / 下{' '}
        {margins?.bottom ?? '—'} / 左 {margins?.left ?? '—'} / 右 {margins?.right ?? '—'}
      </p>
      <p className="meta">
        样式数：{spec.styles ? Object.keys(spec.styles).length : 0} · 目录层级上限：
        {spec.structure?.toc_max_level ?? '—'}
      </p>
      {(spec.structure?.required_h1_titles?.length ?? 0) > 0 && (
        <p className="meta">必备一级标题：{spec.structure?.required_h1_titles?.join('、')}</p>
      )}
    </div>
  )
}

/** 表格视图：逐样式列出字体/字号/对齐/行距/缩进。 */
function SpecTableView({ spec }: { spec: SpecShape | null }) {
  if (spec === null) return <p className="error">JSON 解析失败</p>
  const styles = spec.styles ?? {}
  if (Object.keys(styles).length === 0) return <p className="hint">规格中没有样式定义。</p>
  return (
    <table className="spec-table">
      <thead>
        <tr>
          <th>样式</th>
          <th>名称</th>
          <th>中文字体</th>
          <th>英文字体</th>
          <th>字号(pt)</th>
          <th>对齐</th>
          <th>行距</th>
          <th>首行缩进</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(styles).map(([key, style]) => (
          <tr key={key}>
            <td>{key}</td>
            <td>{style.name ?? '—'}</td>
            <td>{style.run?.font?.eastAsia ?? '—'}</td>
            <td>{style.run?.font?.ascii ?? '—'}</td>
            <td>{style.run?.size_pt ?? '—'}</td>
            <td>{style.paragraph?.alignment ?? '—'}</td>
            <td>{style.paragraph?.line_spacing_rule ?? '—'}</td>
            <td>{style.paragraph?.first_line_indent_chars ?? 0}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
