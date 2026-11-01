import { useEffect, useState, type ReactNode } from 'react'
import { Alert, Card, Descriptions, Space, Spin, Tag, Typography } from 'antd'
import {
  AdminError,
  AdminUnauthorized,
  getAdminConfig,
  type AdminConfigView,
} from './api'

/**
 * 系统配置页（issue #29）：运行参数与模型接入状态的只读展示。数据全部来自
 * 后端只读快照接口（GET /api/admin/config），敏感项（LLM key、管理密码、
 * JWT 密钥）后端就只给"已配置/未配置"布尔——页面想显示值也没有值可显示，
 * 截图/录屏不泄露凭据。
 *
 * 页面没有任何写操作：配置以 .env 为唯一来源（ADR-0005），改配置 = 改
 * .env + 重启服务，后端不存在改配置的接口。401（会话过期）由 ./api 的
 * onUnauthorized 统一回登录表单，这里不处理。
 */

function isApiError(err: unknown): err is AdminError {
  return err instanceof AdminError
}

/** 敏感项统一展示为布尔标签：不出现任何值。 */
function ConfiguredTag({ configured }: { configured: boolean }) {
  return configured ? <Tag color="success">已配置</Tag> : <Tag color="warning">未配置</Tag>
}

function present(value: string | null): ReactNode {
  return value ? <Typography.Text code>{value}</Typography.Text> : <Typography.Text type="secondary">未配置</Typography.Text>
}

function ConfigView({ config }: { config: AdminConfigView }) {
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="配置只读：修改配置 = 编辑服务器上的 .env 后重启服务"
        description="全部配置以 .env 为唯一来源（ADR-0005），本页面与后端接口都不提供任何修改配置的入口。"
      />
      <Card size="small" title="运行参数">
        <Descriptions
          bordered
          size="small"
          column={1}
          items={[
            {
              key: 'recovery_max_attempts',
              label: '僵尸恢复：最大续跑次数',
              children: config.recovery_max_attempts,
            },
            {
              key: 'recovery_stuck_after_seconds',
              label: '僵尸恢复：滞留兜底阈值（秒）',
              children: config.recovery_stuck_after_seconds,
            },
            {
              key: 'recovery_patrol_interval_seconds',
              label: '僵尸恢复：巡检间隔（秒）',
              children: config.recovery_patrol_interval_seconds,
            },
            {
              key: 'max_session_text_chars',
              label: '会话文本上限（字符）',
              children: config.max_session_text_chars,
            },
          ]}
        />
      </Card>
      <Card
        size="small"
        title="模型接入"
        extra={
          config.llm.configured ? (
            <Tag color="success">三项齐备</Tag>
          ) : (
            <Tag color="error">未齐备（润色流水线未启动）</Tag>
          )
        }
      >
        <Descriptions
          bordered
          size="small"
          column={1}
          items={[
            { key: 'base_url', label: '接入点', children: present(config.llm.base_url) },
            { key: 'model', label: '模型名', children: present(config.llm.model) },
            {
              key: 'api_key',
              label: 'API Key',
              children: <ConfiguredTag configured={config.llm.api_key_configured} />,
            },
          ]}
        />
      </Card>
      <Card size="small" title="敏感项（只显示是否已配置，永不显示值）">
        <Descriptions
          bordered
          size="small"
          column={1}
          items={[
            { key: 'admin_password', label: '管理密码', children: <ConfiguredTag configured={config.admin_password.configured} /> },
            { key: 'jwt_secret', label: 'JWT 密钥', children: <ConfiguredTag configured={config.jwt_secret.configured} /> },
          ]}
        />
      </Card>
    </Space>
  )
}

export default function ConfigPage() {
  const [config, setConfig] = useState<AdminConfigView | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getAdminConfig()
      .then((result) => {
        if (!cancelled) setConfig(result)
      })
      .catch((err: unknown) => {
        if (err instanceof AdminUnauthorized) return // 全局已回登录表单
        if (!cancelled) {
          setError(isApiError(err) ? err.message : '加载系统配置失败，请稍后重试')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Title level={4} style={{ margin: 0 }}>
        系统配置
      </Typography.Title>
      {error && <Alert type="error" showIcon message={error} />}
      <Spin spinning={loading}>{config && <ConfigView config={config} />}</Spin>
    </Space>
  )
}
