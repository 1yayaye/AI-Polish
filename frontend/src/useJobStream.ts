import { useEffect, useRef, useState } from 'react'
import { fetchEventSource } from '@microsoft/fetch-event-source'
import type { JobStreamState } from './api'

/**
 * Word 任务进度流（backend/app/word_stream.py 事件协议，与 useSessionStream
 * 同一 Hook 形态）：
 * - 连接先收 snapshot（任务状态全量）再收 progress 增量，completed/failed 后
 *   服务端关流；三类事件载荷同形，直接整体替换本地状态；
 * - 认证走 X-Card-Key 请求头（浏览器原生 EventSource 不能带 header，故用
 *   @microsoft/fetch-event-source）；卡密绝不进 URL；
 * - 断线重连带 Last-Event-ID（收到事件的 id 记入 ref，重连时随 header 上送），
 *   缺口状态由服务端每次重连必发的 snapshot 补齐——不丢、不重。
 *
 * fetch-event-source 的重试会沿用建连时的静态 headers，带不上最新的
 * Last-Event-ID，因此这里 onerror 抛错终止库内重试，由本 Hook 自己的
 * 循环用最新 id 重新建连。
 */

export interface JobStream {
  job: JobStreamState | null
  /** 连接状态：open = 实时连接中；reconnecting = 断线重连中；closed = 已终态关流 */
  connection: 'open' | 'reconnecting' | 'closed'
  /** 不可恢复的错误（如卡密无效 401、任务已删除 404）；重连类错误不会出现在这里 */
  fatal: string | null
}

export function useJobStream(cardKey: string, jobId: number | null): JobStream {
  const [job, setJob] = useState<JobStreamState | null>(null)
  const [connection, setConnection] = useState<JobStream['connection']>('reconnecting')
  const [fatal, setFatal] = useState<string | null>(null)
  const lastEventIdRef = useRef(0)

  useEffect(() => {
    if (jobId === null) {
      setJob(null)
      setConnection('reconnecting')
      setFatal(null)
      return
    }
    lastEventIdRef.current = 0
    let stopped = false // 任务切换 / 组件卸载
    let finished = false // 服务端终态关流：不再重连
    const controller = new AbortController()

    setJob(null)
    setConnection('reconnecting')
    setFatal(null)

    async function connectLoop(): Promise<void> {
      while (!stopped && !finished) {
        const headers: Record<string, string> = { 'X-Card-Key': cardKey }
        if (lastEventIdRef.current > 0) {
          headers['Last-Event-ID'] = String(lastEventIdRef.current)
        }
        try {
          await fetchEventSource(`/api/word-formatter/jobs/${jobId}/stream`, {
            signal: controller.signal,
            openWhenHidden: true,
            headers,
            async onopen(response) {
              if (response.status === 401) {
                throw new FatalStreamError('卡密无效，请重新登录')
              }
              if (response.status === 404) {
                // 任务已被删除（如另一标签页）或不存在：重连永远不可能成功
                throw new FatalStreamError('任务已删除或不存在')
              }
              if (!response.ok) {
                // 5xx 等临时故障：可重试
                throw new Error(`HTTP ${response.status}`)
              }
              setConnection('open')
            },
            onmessage(message) {
              if (message.id) {
                lastEventIdRef.current = Number(message.id)
              }
              if (
                message.event === 'snapshot' ||
                message.event === 'progress' ||
                message.event === 'completed' ||
                message.event === 'failed'
              ) {
                setJob(JSON.parse(message.data) as JobStreamState)
                if (message.event === 'completed' || message.event === 'failed') {
                  finished = true
                  setConnection('closed')
                }
              }
            },
            onerror(error) {
              // 抛出 = 停止库内自动重试，交给外层循环带最新 Last-Event-ID 重连
              throw error
            },
          })
        } catch (error) {
          if (stopped || finished) break
          if (error instanceof FatalStreamError) {
            setFatal(error.message)
            setConnection('closed')
            break
          }
          setConnection('reconnecting')
          await new Promise((resolve) => setTimeout(resolve, 2000))
        }
      }
    }

    void connectLoop()
    return () => {
      stopped = true
      controller.abort()
    }
  }, [cardKey, jobId])

  return { job, connection, fatal }
}

class FatalStreamError extends Error {}
