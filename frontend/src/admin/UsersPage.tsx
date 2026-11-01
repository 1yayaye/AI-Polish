import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  App as AntApp,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Space,
  Table,
  Typography,
} from 'antd'
import {
  AdminError,
  AdminUnauthorized,
  adjustUserBalance,
  adjustUserUsage,
  issueCardKeys,
  listUsers,
  type AdminUser,
  type AdminUserPage,
  type IssuedCardKey,
} from './api'

/**
 * 用户管理页（issue #26）：批量发卡、用户分页列表（脱敏卡密 + 双轨额度）、
 * 按卡密片段或用户 ID 搜索、余额/次数额度调整（原因必填）。
 *
 * 调整表单用「方向 + 正数金额」而不是带符号 delta：0 与负数在 UI 上不存在，
 * 空/纯空白原因被表单规则挡下——后端的 409/422 契约留给直连 API 的调用方。
 * 401（会话过期）由 ./api 的 onUnauthorized 统一回登录表单，这里不处理。
 */

type Track = 'balance' | 'usage'

function isApiError(err: unknown): err is AdminError {
  return err instanceof AdminError
}

/** 批量发卡表单。成功后把完整卡密交给父组件做一次性展示。 */
function IssueCardsModal({
  open,
  onClose,
  onIssued,
}: {
  open: boolean
  onClose: () => void
  onIssued: (keys: IssuedCardKey[]) => void
}) {
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [form] = Form.useForm<{ count: number; balance_fen: number; usage_limit: number }>()

  async function handleFinish(values: {
    count: number
    balance_fen: number
    usage_limit: number
  }) {
    setSubmitting(true)
    setError('')
    try {
      const result = await issueCardKeys(values)
      form.resetFields()
      onIssued(result.card_keys)
    } catch (err) {
      setError(isApiError(err) ? err.message : '网络错误，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      title="批量发卡"
      open={open}
      onCancel={onClose}
      footer={null}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        message="发卡即充值：初始双轨额度经计费服务入账。完整卡密只在发卡后展示一次。"
        style={{ marginBottom: 16 }}
      />
      <Form
        form={form}
        layout="vertical"
        onFinish={handleFinish}
        disabled={submitting}
        initialValues={{ count: 10, balance_fen: 0, usage_limit: 0 }}
      >
        <Form.Item
          name="count"
          label="发卡数量"
          rules={[{ required: true, message: '请输入发卡数量' }]}
        >
          <InputNumber min={1} max={500} precision={0} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item
          name="balance_fen"
          label="初始余额（分）"
          rules={[{ required: true, message: '请输入初始余额' }]}
        >
          <InputNumber min={0} precision={0} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item
          name="usage_limit"
          label="初始次数额度"
          rules={[{ required: true, message: '请输入初始次数额度' }]}
        >
          <InputNumber min={0} precision={0} style={{ width: '100%' }} />
        </Form.Item>
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
        <Button type="primary" htmlType="submit" loading={submitting} block>
          生成卡密
        </Button>
      </Form>
    </Modal>
  )
}

/** 发卡结果：完整卡密的一次性展示，关掉就再也拿不到。 */
function IssuedKeysModal({
  keys,
  onClose,
}: {
  keys: IssuedCardKey[] | null
  onClose: () => void
}) {
  const { message } = AntApp.useApp()

  async function copyAll() {
    if (!keys) return
    try {
      await navigator.clipboard.writeText(keys.map((k) => k.card_key).join('\n'))
      message.success(`已复制全部 ${keys.length} 个卡密`)
    } catch {
      message.error('复制失败，请在列表中手动选择复制')
    }
  }

  return (
    <Modal title="发卡成功" open={keys !== null} onCancel={onClose} footer={null}>
      <Alert
        type="warning"
        showIcon
        message="完整卡密仅此一次展示，关闭后无法再次查看，请立即复制保存。"
        style={{ marginBottom: 16 }}
      />
      <div style={{ maxHeight: 320, overflow: 'auto' }}>
        {(keys ?? []).map((k) => (
          <Typography.Paragraph key={k.card_key} code copyable style={{ margin: '4px 0' }}>
            {k.card_key}
          </Typography.Paragraph>
        ))}
      </div>
      <Button type="primary" onClick={copyAll} block style={{ marginTop: 16 }}>
        一键复制全部卡密
      </Button>
    </Modal>
  )
}

/** 双轨额度调整：同一张表单，两个轨道只差文案与接口。 */
function AdjustModal({
  user,
  track,
  onClose,
  onAdjusted,
}: {
  user: AdminUser
  track: Track
  onClose: () => void
  onAdjusted: () => void
}) {
  const { message } = AntApp.useApp()
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [form] = Form.useForm<{ direction: 'inc' | 'dec'; amount: number; reason: string }>()

  const isBalance = track === 'balance'
  const title = isBalance ? `调整余额 — 用户 #${user.id}` : `调整次数额度 — 用户 #${user.id}`
  const amountLabel = isBalance ? '金额（分）' : '次数'

  async function handleFinish(values: {
    direction: 'inc' | 'dec'
    amount: number
    reason: string
  }) {
    const delta = values.direction === 'inc' ? values.amount : -values.amount
    setSubmitting(true)
    setError('')
    try {
      if (isBalance) {
        await adjustUserBalance(user.id, { delta_fen: delta, reason: values.reason.trim() })
        message.success('余额已更新')
      } else {
        await adjustUserUsage(user.id, { delta, reason: values.reason.trim() })
        message.success('次数额度已更新')
      }
      form.resetFields()
      onAdjusted()
    } catch (err) {
      if (err instanceof AdminUnauthorized) return // 全局已回登录表单
      setError(isApiError(err) ? err.message : '网络错误，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal title={title} open onCancel={onClose} footer={null} destroyOnHidden>
      {isBalance ? (
        <Typography.Text type="secondary">当前余额 {user.balance_fen} 分</Typography.Text>
      ) : (
        <Typography.Text type="secondary">
          次数上限 {user.usage_limit} · 剩余 {user.usage_remaining}
        </Typography.Text>
      )}
      <Form
        form={form}
        layout="vertical"
        onFinish={handleFinish}
        disabled={submitting}
        initialValues={{ direction: 'inc' }}
        style={{ marginTop: 12 }}
      >
        <Form.Item name="direction" label="方向">
          <Radio.Group
            options={
              isBalance
                ? [
                    { value: 'inc', label: '充值' },
                    { value: 'dec', label: '扣减' },
                  ]
                : [
                    { value: 'inc', label: '增加' },
                    { value: 'dec', label: '调低' },
                  ]
            }
            optionType="button"
            buttonStyle="solid"
          />
        </Form.Item>
        <Form.Item
          name="amount"
          label={amountLabel}
          rules={[{ required: true, message: `请输入${amountLabel}` }]}
        >
          <InputNumber min={1} precision={0} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item
          name="reason"
          label="调整原因"
          extra="将记入账本，供对账与客诉排查"
          rules={[
            { required: true, whitespace: true, message: '请填写调整原因' },
            { max: 200, message: '原因最长 200 字' },
          ]}
        >
          <Input.TextArea rows={2} maxLength={200} placeholder="例如：线下收款充值" />
        </Form.Item>
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
        <Button type="primary" htmlType="submit" loading={submitting} block>
          确认调整
        </Button>
      </Form>
    </Modal>
  )
}

export default function UsersPage() {
  const { message } = AntApp.useApp()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [query, setQuery] = useState('')
  const [data, setData] = useState<AdminUserPage | null>(null)
  const [loading, setLoading] = useState(false)
  const [issueOpen, setIssueOpen] = useState(false)
  const [issued, setIssued] = useState<IssuedCardKey[] | null>(null)
  const [adjusting, setAdjusting] = useState<{ user: AdminUser; track: Track } | null>(null)

  const load = useCallback(async (p: number, ps: number, q: string) => {
    setLoading(true)
    try {
      const result = await listUsers({ page: p, page_size: ps, q })
      setData(result)
    } catch (err) {
      if (err instanceof AdminUnauthorized) return // 全局已回登录表单
      message.error(isApiError(err) ? err.message : '加载用户列表失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }, [message])

  useEffect(() => {
    void load(page, pageSize, query)
  }, [load, page, pageSize, query])

  function handleSearch(value: string) {
    setPage(1)
    setQuery(value.trim())
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space style={{ justifyContent: 'space-between', width: '100%' }} wrap>
        <Input.Search
          placeholder="按卡密片段或用户 ID 搜索"
          allowClear
          enterButton
          onSearch={handleSearch}
          style={{ maxWidth: 360 }}
        />
        <Button type="primary" onClick={() => setIssueOpen(true)}>
          批量发卡
        </Button>
      </Space>
      <Table<AdminUser>
        rowKey="id"
        loading={loading}
        dataSource={data?.items ?? []}
        pagination={{
          current: page,
          pageSize,
          total: data?.total ?? 0,
          showSizeChanger: true,
          showTotal: (total) => `共 ${total} 位用户`,
          onChange: (nextPage, nextPageSize) => {
            // 改页大小时回第 1 页：旧页码在新页大小下可能越界落空页
            if (nextPageSize !== pageSize) {
              setPage(1)
              setPageSize(nextPageSize)
            } else {
              setPage(nextPage)
            }
          },
        }}
        columns={[
          { title: 'ID', dataIndex: 'id', width: 72 },
          {
            title: '卡密（脱敏）',
            dataIndex: 'card_key',
            render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
          },
          { title: '余额（分）', dataIndex: 'balance_fen', align: 'right' },
          { title: '次数上限', dataIndex: 'usage_limit', align: 'right' },
          { title: '剩余次数', dataIndex: 'usage_remaining', align: 'right' },
          {
            title: '操作',
            key: 'actions',
            width: 200,
            render: (_, user) => (
              <Space>
                <Button
                  size="small"
                  onClick={() => setAdjusting({ user, track: 'balance' })}
                >
                  余额调整
                </Button>
                <Button size="small" onClick={() => setAdjusting({ user, track: 'usage' })}>
                  次数调整
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <IssueCardsModal
        open={issueOpen}
        onClose={() => setIssueOpen(false)}
        onIssued={(keys) => {
          setIssueOpen(false)
          setIssued(keys)
          setPage(1) // 新卡密在列表尾段，回第 1 页从最新状态开始看
          void load(1, pageSize, query)
        }}
      />
      <IssuedKeysModal keys={issued} onClose={() => setIssued(null)} />
      {adjusting && (
        <AdjustModal
          user={adjusting.user}
          track={adjusting.track}
          onClose={() => setAdjusting(null)}
          onAdjusted={() => {
            setAdjusting(null)
            void load(page, pageSize, query)
          }}
        />
      )}
    </Space>
  )
}
