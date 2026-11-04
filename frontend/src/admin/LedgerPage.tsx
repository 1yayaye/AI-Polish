import { useCallback, useEffect, useState } from 'react'
import { App as AntApp, Button, Space, Table, Tag, Typography, type TableProps } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import {
  AdminUnauthorized,
  createPageChange,
  formatTime,
  isApiError,
  ledgerKindLabel,
  listRecentLedger,
  listUserLedger,
  type AdminLedgerEntry,
  type AdminLedgerPage,
} from './api'

/**
 * 账本流水页（issue #27）：同一组件承担两个视图——
 * /admin/ledger 全局最近流水；/admin/users/:userId 某用户的全部流水。
 * 视图差异只有数据来源、标题与「用户 ID」一列，列表/分页/展示完全共享。
 *
 * 数据全部来自后端只读查询接口：页面不产生任何账本变动。
 * 401（会话过期）由 ./api 的 onUnauthorized 统一回登录表单，这里不处理。
 */

/** 带符号金额：余额轨显示 +N/-N；次数轨变动金额恒为 0，显示 —。 */
function AmountCell({ value }: { value: number }) {
  if (value === 0) return <span>—</span>
  const text = value > 0 ? `+${value}` : String(value)
  return (
    <Typography.Text type={value > 0 ? 'success' : 'danger'}>{text}</Typography.Text>
  )
}

export default function LedgerPage() {
  const { message } = AntApp.useApp()
  const navigate = useNavigate()
  const params = useParams()
  const userId = params.userId !== undefined ? Number(params.userId) : null
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [data, setData] = useState<AdminLedgerPage | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(
    async (uid: number | null, p: number, ps: number) => {
      setLoading(true)
      try {
        const result =
          uid === null
            ? await listRecentLedger({ page: p, page_size: ps })
            : await listUserLedger(uid, { page: p, page_size: ps })
        setData(result)
      } catch (err) {
        if (err instanceof AdminUnauthorized) return // 全局已回登录表单
        message.error(isApiError(err) ? err.message : '加载流水失败，请稍后重试')
      } finally {
        setLoading(false)
      }
    },
    [message],
  )

  useEffect(() => {
    void load(userId, page, pageSize)
  }, [load, userId, page, pageSize])

  const columns: TableProps<AdminLedgerEntry>['columns'] = []
  if (userId === null) {
    columns.push({ title: '用户 ID', dataIndex: 'user_id', width: 88 })
  }
  columns.push(
    {
      title: '类型',
      dataIndex: 'kind',
      width: 110,
      render: (kind: string) => <Tag>{ledgerKindLabel(kind)}</Tag>,
    },
    {
      title: '金额（分）',
      dataIndex: 'amount_fen',
      align: 'right',
      width: 110,
      render: (value: number) => <AmountCell value={value} />,
    },
    { title: '变动后余额（分）', dataIndex: 'balance_after_fen', align: 'right', width: 150 },
    {
      title: '变动后剩余次数',
      dataIndex: 'usage_after',
      align: 'right',
      width: 130,
      render: (value: number | null) => value ?? '—',
    },
    {
      title: '任务 ID',
      dataIndex: 'task_id',
      width: 88,
      render: (value: number | null) => value ?? '—',
    },
    { title: '原因', dataIndex: 'reason', ellipsis: true },
    { title: '时间', dataIndex: 'created_at', width: 180, render: formatTime },
  )

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {userId === null ? (
        <Typography.Title level={4} style={{ margin: 0 }}>
          全局最近流水
        </Typography.Title>
      ) : (
        <Space>
          <Button onClick={() => navigate('/admin')}>返回用户列表</Button>
          <Typography.Title level={4} style={{ margin: 0 }}>
            用户 #{userId} 流水
            {data?.card_key && (
              <Typography.Text code style={{ marginLeft: 12, fontSize: 14 }}>
                {data.card_key}
              </Typography.Text>
            )}
          </Typography.Title>
        </Space>
      )}
      <Table<AdminLedgerEntry>
        rowKey="id"
        loading={loading}
        dataSource={data?.items ?? []}
        pagination={{
          current: page,
          pageSize,
          total: data?.total ?? 0,
          showSizeChanger: true,
          showTotal: (total) => `共 ${total} 条流水`,
          onChange: createPageChange(setPage, setPageSize, pageSize),
        }}
        columns={columns}
      />
    </Space>
  )
}
