import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  App as AntApp,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import {
  AdminError,
  AdminUnauthorized,
  listPrompts,
  setSystemPromptDefault,
  updateSystemPromptContent,
  type AdminPrompt,
  type AdminPromptPage,
} from './api'

/**
 * 提示词管理页（issue #30，方案 A：DB 为唯一事实源）：系统行正文编辑 + 设为
 * 阶段默认，用户自定义行只读可见（排查"润色效果不对"客诉时看用户的默认
 * 提示词）。
 *
 * 系统行保存即生效——下一个会话的流水线消费的就是库内新正文，无需发版；
 * 用户自定义行的默认与正文归用户自己管理，页面只展示。401（会话过期）由
 * ./api 的 onUnauthorized 统一回登录表单，这里不处理。
 */

const STAGE_LABELS: Record<string, string> = {
  polish: '润色',
  enhance: '原创性增强',
}

function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString()
}

function isApiError(err: unknown): err is AdminError {
  return err instanceof AdminError
}

/** 系统行正文编辑：保存即落库，立即对后续会话生效。 */
function EditContentModal({
  prompt,
  onClose,
  onSaved,
}: {
  prompt: AdminPrompt
  onClose: () => void
  onSaved: () => void
}) {
  const { message } = AntApp.useApp()
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [form] = Form.useForm<{ content: string }>()

  async function handleFinish(values: { content: string }) {
    setSubmitting(true)
    setError('')
    try {
      await updateSystemPromptContent(prompt.id, values.content)
      message.success('正文已保存，对下一个会话立即生效')
      form.resetFields()
      onSaved()
    } catch (err) {
      if (err instanceof AdminUnauthorized) return // 全局已回登录表单
      setError(isApiError(err) ? err.message : '网络错误，请稍后重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      title={`编辑系统提示词正文 — ${prompt.name}`}
      open
      onCancel={onClose}
      footer={null}
      width={720}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        message="保存后立即生效：下一个会话的流水线就使用新正文，无需发版或重启。"
        style={{ marginBottom: 16 }}
      />
      <Form
        form={form}
        layout="vertical"
        onFinish={handleFinish}
        disabled={submitting}
        initialValues={{ content: prompt.content }}
      >
        <Form.Item
          name="content"
          label="正文"
          rules={[{ required: true, whitespace: true, message: '正文不能为空' }]}
        >
          <Input.TextArea rows={18} style={{ fontFamily: 'monospace' }} />
        </Form.Item>
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
        <Button type="primary" htmlType="submit" loading={submitting} block>
          保存正文
        </Button>
      </Form>
    </Modal>
  )
}

export default function PromptsPage() {
  const { message } = AntApp.useApp()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [stage, setStage] = useState<string | undefined>(undefined)
  const [data, setData] = useState<AdminPromptPage | null>(null)
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState<AdminPrompt | null>(null)

  const load = useCallback(
    async (p: number, ps: number, s: string | undefined) => {
      setLoading(true)
      try {
        const result = await listPrompts({ page: p, page_size: ps, stage: s })
        setData(result)
      } catch (err) {
        if (err instanceof AdminUnauthorized) return // 全局已回登录表单
        message.error(isApiError(err) ? err.message : '加载提示词列表失败，请稍后重试')
      } finally {
        setLoading(false)
      }
    },
    [message],
  )

  useEffect(() => {
    void load(page, pageSize, stage)
  }, [load, page, pageSize, stage])

  async function handleSetDefault(prompt: AdminPrompt) {
    try {
      await setSystemPromptDefault(prompt.id)
      message.success(`已设为「${stageLabel(prompt.stage)}」阶段的默认提示词`)
      void load(page, pageSize, stage)
    } catch (err) {
      if (err instanceof AdminUnauthorized) return
      message.error(isApiError(err) ? err.message : '设置默认失败，请稍后重试')
    }
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space style={{ justifyContent: 'space-between', width: '100%' }} wrap>
        <Typography.Text type="secondary">
          系统提示词以数据库为唯一事实源：编辑正文保存即生效，无需发版。用户自定义提示词只读可见。
        </Typography.Text>
        <Select
          value={stage}
          onChange={(value) => {
            setPage(1)
            setStage(value)
          }}
          allowClear
          placeholder="全部阶段"
          style={{ width: 180 }}
          options={[
            { value: 'polish', label: '润色' },
            { value: 'enhance', label: '原创性增强' },
          ]}
        />
      </Space>
      <Table<AdminPrompt>
        rowKey="id"
        loading={loading}
        dataSource={data?.items ?? []}
        pagination={{
          current: page,
          pageSize,
          total: data?.total ?? 0,
          showSizeChanger: true,
          showTotal: (total) => `共 ${total} 条提示词`,
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
          { title: 'ID', dataIndex: 'id', width: 64 },
          {
            title: '阶段',
            dataIndex: 'stage',
            width: 120,
            render: (stage: string) => <Tag>{stageLabel(stage)}</Tag>,
          },
          { title: '名称', dataIndex: 'name' },
          {
            title: '归属',
            key: 'owner',
            width: 200,
            render: (_, prompt) =>
              prompt.is_system ? (
                <Tag color="blue">系统</Tag>
              ) : (
                <span>
                  <Tag>用户 #{prompt.user_id}</Tag>
                  {prompt.card_key && (
                    <Typography.Text code>{prompt.card_key}</Typography.Text>
                  )}
                </span>
              ),
          },
          {
            title: '默认',
            dataIndex: 'is_default',
            width: 80,
            render: (isDefault: boolean) => (isDefault ? <Tag color="green">默认</Tag> : '—'),
          },
          {
            title: '状态',
            dataIndex: 'is_active',
            width: 80,
            render: (isActive: boolean) => (isActive ? '启用' : <Tag>停用</Tag>),
          },
          {
            title: '更新时间',
            dataIndex: 'updated_at',
            width: 180,
            render: formatTime,
          },
          {
            title: '操作',
            key: 'actions',
            width: 200,
            render: (_, prompt) =>
              prompt.is_system ? (
                <Space>
                  <Button size="small" onClick={() => setEditing(prompt)}>
                    编辑正文
                  </Button>
                  {prompt.is_default ? (
                    <Button size="small" disabled>
                      当前默认
                    </Button>
                  ) : (
                    <Popconfirm
                      title={`设为「${stageLabel(prompt.stage)}」阶段的默认提示词？同阶段其余默认将自动取消。`}
                      onConfirm={() => void handleSetDefault(prompt)}
                    >
                      <Button size="small">设为默认</Button>
                    </Popconfirm>
                  )}
                </Space>
              ) : (
                <Typography.Text type="secondary">只读</Typography.Text>
              ),
          },
        ]}
      />
      {editing && (
        <EditContentModal
          prompt={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            void load(page, pageSize, stage)
          }}
        />
      )}
    </Space>
  )
}
