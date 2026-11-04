import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  App as AntApp,
  Button,
  Descriptions,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  type TableProps,
} from 'antd'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  AdminUnauthorized,
  createPageChange,
  formatTime,
  getAdminSessionDetail,
  isApiError,
  ledgerKindLabel,
  listSessions,
  type AdminSessionDetail,
  type AdminSessionLedgerRow,
  type AdminSessionPage,
  type AdminSessionStatus,
  type AdminSessionSummary,
  type AdminSegmentStatus,
} from './api'

/**
 * 会话监控页（issue #28）：同一组件承担两个视图——
 * /admin/sessions 会话列表（按状态过滤 + 分页）；/admin/sessions/:sessionId
 * 会话详情（model_snapshot + 段落阶段状态 + 该会话账本行）。视图拆分与数据
 * 加载沿用 LedgerPage 的「路由参数选视图」先例，状态/模式等展示词表也只在
 * 这一处定义。
 *
 * 数据全部来自后端只读查询接口：页面不产生任何状态迁移、退款或账本变动，
 * 看到的就是数据库此刻的状态。401（会话过期）由 ./api 的 onUnauthorized
 * 统一回登录表单，这里不处理。
 */

const STATUS_OPTIONS: { value: AdminSessionStatus; label: string }[] = [
  { value: 'queued', label: '排队中' },
  { value: 'running', label: '运行中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '已失败' },
  { value: 'cancelled', label: '已取消' },
]

const STATUS_LABELS: Record<string, string> = Object.fromEntries(
  STATUS_OPTIONS.map((option) => [option.value, option.label]),
)

const STATUS_COLORS: Record<string, string> = {
  queued: 'default',
  running: 'processing',
  completed: 'success',
  failed: 'error',
  cancelled: 'warning',
}

const MODE_LABELS: Record<string, string> = {
  paper_polish: '润色',
  paper_enhance: '增强',
  paper_polish_enhance: '润色 + 增强',
}

const SEGMENT_STATUS_COLORS: Record<string, string> = {
  pending: 'default',
  completed: 'success',
  failed: 'error',
}

function modeLabel(mode: string): string {
  return MODE_LABELS[mode] ?? mode
}

function StatusTag({ status }: { status: string }) {
  return <Tag color={STATUS_COLORS[status]}>{STATUS_LABELS[status] ?? status}</Tag>
}

/** 会话列表：状态/用户过滤 + 分页，每行含模式/计费字数/预扣金额/所属用户。 */
function SessionListView() {
  const { message } = AntApp.useApp()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  // 用户过滤经 URL 承载（?user_id=N）：用户管理页凭卡密片段找到人后深链过来，
  // 刷新/分享链接都不丢过滤条件；非法值视作未过滤。
  const userIdParam = searchParams.get('user_id')
  const userIdFilter = userIdParam !== null && /^\d+$/.test(userIdParam) ? Number(userIdParam) : null
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [status, setStatus] = useState<AdminSessionStatus | null>(null)
  const [data, setData] = useState<AdminSessionPage | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(
    async (p: number, ps: number, st: AdminSessionStatus | null, uid: number | null) => {
      setLoading(true)
      try {
        const result = await listSessions({
          page: p,
          page_size: ps,
          status: st ?? undefined,
          user_id: uid ?? undefined,
        })
        setData(result)
      } catch (err) {
        if (err instanceof AdminUnauthorized) return // 全局已回登录表单
        message.error(isApiError(err) ? err.message : '加载会话列表失败，请稍后重试')
      } finally {
        setLoading(false)
      }
    },
    [message],
  )

  useEffect(() => {
    void load(page, pageSize, status, userIdFilter)
  }, [load, page, pageSize, status, userIdFilter])

  const columns: TableProps<AdminSessionSummary>['columns'] = [
    { title: 'ID', dataIndex: 'id', width: 88 },
    {
      title: '所属用户',
      dataIndex: 'card_key',
      render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (value: string) => <StatusTag status={value} />,
    },
    {
      title: '处理模式',
      dataIndex: 'mode',
      width: 120,
      render: modeLabel,
    },
    { title: '计费字数', dataIndex: 'word_count', align: 'right', width: 100 },
    { title: '预扣金额（分）', dataIndex: 'charged_fen', align: 'right', width: 130 },
    {
      title: '模型',
      dataIndex: 'model_snapshot',
      ellipsis: true,
      render: (value: string | null) =>
        value ? <Typography.Text code>{value}</Typography.Text> : <span>—</span>,
    },
    { title: '创建时间', dataIndex: 'created_at', width: 180, render: formatTime },
    {
      title: '操作',
      key: 'actions',
      width: 90,
      render: (_, row) => (
        <Button size="small" onClick={() => navigate(`/admin/sessions/${row.id}`)}>
          详情
        </Button>
      ),
    },
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Title level={4} style={{ margin: 0 }}>
        会话监控
      </Typography.Title>
      {userIdFilter !== null && (
        <Alert
          type="info"
          showIcon
          message={`仅显示用户 #${userIdFilter} 的会话`}
          description="经用户管理页的卡密片段搜索进入。取消过滤可查看全部会话。"
          action={
            <Button
              size="small"
              onClick={() => {
                setPage(1)
                setSearchParams({})
              }}
            >
              取消过滤
            </Button>
          }
        />
      )}
      <Select<AdminSessionStatus | null>
        value={status}
        onChange={(value) => {
          setPage(1) // 换过滤条件后旧页码可能越界落空页
          setStatus(value)
        }}
        style={{ width: 160 }}
        options={[{ value: null, label: '全部状态' }, ...STATUS_OPTIONS]}
      />
      <Table<AdminSessionSummary>
        rowKey="id"
        loading={loading}
        dataSource={data?.items ?? []}
        pagination={{
          current: page,
          pageSize,
          total: data?.total ?? 0,
          showSizeChanger: true,
          showTotal: (total) => `共 ${total} 个会话`,
          onChange: createPageChange(setPage, setPageSize, pageSize),
        }}
        columns={columns}
      />
    </Space>
  )
}

const SEGMENT_COLUMNS: TableProps<AdminSegmentStatus>['columns'] = [
  {
    title: '阶段',
    dataIndex: 'stage',
    width: 100,
    render: (stage: string) => <Tag>{stage === 'enhance' ? '增强' : '润色'}</Tag>,
  },
  { title: '序号', dataIndex: 'seq', width: 80 },
  {
    title: '状态',
    dataIndex: 'status',
    render: (status: string) => (
      <Tag color={SEGMENT_STATUS_COLORS[status]}>{status}</Tag>
    ),
  },
]

/** 该会话的账本行：退款行出现在账本里即"退款已落账"。 */
function SessionLedgerTable({ rows }: { rows: AdminSessionLedgerRow[] }) {
  const hasRefund = rows.some((row) => row.kind === 'refund')
  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      <Space>
        <Typography.Text strong>退款状态</Typography.Text>
        {hasRefund ? (
          <Tag color="success">已落账</Tag>
        ) : (
          <Tag>无退款流水</Tag>
        )}
      </Space>
      <Table<AdminSessionLedgerRow>
        rowKey="id"
        size="small"
        dataSource={rows}
        pagination={false}
        columns={[
          {
            title: '类型',
            dataIndex: 'kind',
            width: 110,
            render: (kind: string) => (
              <Tag color={kind === 'refund' ? 'success' : 'processing'}>
                {ledgerKindLabel(kind)}
              </Tag>
            ),
          },
          {
            title: '金额（分）',
            dataIndex: 'amount_fen',
            align: 'right',
            width: 110,
            render: (value: number) => (
              <Typography.Text type={value > 0 ? 'success' : 'danger'}>
                {value > 0 ? `+${value}` : String(value)}
              </Typography.Text>
            ),
          },
          { title: '变动后余额（分）', dataIndex: 'balance_after_fen', align: 'right', width: 150 },
          { title: '原因', dataIndex: 'reason', ellipsis: true },
          { title: '时间', dataIndex: 'created_at', width: 180, render: formatTime },
        ]}
      />
    </Space>
  )
}

/** 会话详情：只读 model_snapshot + 段落阶段状态 + 该会话账本行。 */
function SessionDetailView({ sessionId }: { sessionId: number }) {
  const navigate = useNavigate()
  const [data, setData] = useState<AdminSessionDetail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getAdminSessionDetail(sessionId)
      .then((detail) => {
        if (!cancelled) setData(detail)
      })
      .catch((err: unknown) => {
        if (err instanceof AdminUnauthorized) return // 全局已回登录表单
        if (!cancelled) {
          setError(isApiError(err) ? err.message : '加载会话详情失败，请稍后重试')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [sessionId])

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space>
        <Button onClick={() => navigate('/admin/sessions')}>返回会话列表</Button>
        <Typography.Title level={4} style={{ margin: 0 }}>
          会话 #{sessionId}
        </Typography.Title>
      </Space>
      {error && <Alert type="error" showIcon message={error} />}
      <Spin spinning={loading}>
        {data && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Descriptions
              bordered
              size="small"
              column={2}
              items={[
                { key: 'status', label: '状态', children: <StatusTag status={data.status} /> },
                { key: 'mode', label: '处理模式', children: modeLabel(data.mode) },
                {
                  key: 'card_key',
                  label: '所属用户',
                  children: <Typography.Text code>{data.card_key}</Typography.Text>,
                },
                {
                  key: 'model_snapshot',
                  label: '模型快照',
                  children: data.model_snapshot ? (
                    <Typography.Text code copyable={false}>
                      {data.model_snapshot}
                    </Typography.Text>
                  ) : (
                    <Typography.Text type="secondary">尚未认领（无模型记录）</Typography.Text>
                  ),
                },
                { key: 'word_count', label: '计费字数', children: data.word_count },
                { key: 'charged_fen', label: '预扣金额（分）', children: data.charged_fen },
                { key: 'created_at', label: '创建时间', children: formatTime(data.created_at) },
                { key: 'updated_at', label: '更新时间', children: formatTime(data.updated_at) },
              ]}
            />
            <div>
              <Typography.Title level={5}>段落阶段</Typography.Title>
              <Table<AdminSegmentStatus>
                rowKey={(row) => `${row.stage}-${row.seq}`}
                size="small"
                dataSource={data.segments}
                pagination={false}
                columns={SEGMENT_COLUMNS}
              />
            </div>
            <div>
              <Typography.Title level={5}>账本行（预扣与退款）</Typography.Title>
              <SessionLedgerTable rows={data.ledger} />
            </div>
          </Space>
        )}
      </Spin>
    </Space>
  )
}

/** 深链里的会话 ID 非数字：不发起请求，直接给出可读的无效态。 */
function InvalidSessionView() {
  const navigate = useNavigate()
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Button onClick={() => navigate('/admin/sessions')}>返回会话列表</Button>
      <Alert type="warning" showIcon message="无效的会话 ID" />
    </Space>
  )
}

export default function SessionsPage() {
  const params = useParams()
  if (params.sessionId !== undefined) {
    // 深链参数手改成非数字时，Number() 会以 NaN 发请求、标题渲染「#NaN」；
    // 与列表过滤参数同一纪律：非法值不进视图（见 SessionListView 的 user_id）
    return /^\d+$/.test(params.sessionId) ? (
      <SessionDetailView sessionId={Number(params.sessionId)} />
    ) : (
      <InvalidSessionView />
    )
  }
  return <SessionListView />
}
