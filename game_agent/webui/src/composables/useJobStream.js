/**
 * 生成任务的进度流（GET + SSE）——N2a 的"边跑边看"。
 *
 * **和 `useTurnStream.js` 长得像，但断线语义正好相反，别把两者的结论互相搬：**
 *
 * | | 回合流（POST） | 任务进度流（GET） |
 * | --- | --- | --- |
 * | 断线重连 | **会把回合重跑一遍**（重复扣费、重复落盘）→ 绝对禁止 | 只是**重新订阅**，不重跑任何东西 |
 * | 重连后收到什么 | 新的一回合 | 服务端**先回放全部历史事件**再续播 |
 *
 * 为什么这里仍然不用 `EventSource`（它是 GET、看起来正合适）：它的自动重连是
 * **不带状态**的，而本端点的订阅语义是"回放 + 续播"。用它就会在每次重连后
 * 重新收到一遍历史，日志里出现重复行，除非我再写一层去重——那不如自己控制重连。
 *
 * **重连策略是"有上限的显式重连"**，不是无限自动重连：任务在服务端照跑
 * （断线不取消任务），所以短暂掉线应该接上；但接不上时要把状态如实显示成
 * "已断开，任务仍在服务端运行"，而不是无限重试假装一切正常。
 *
 * **日志以服务端回放为准**：每次订阅都从空列表重建，因为服务端总会先回放
 * 完整历史。这样"重连后重复行"这个问题在结构上就不存在——不需要去重代码。
 */

import { ref, shallowRef } from 'vue'

import { parseSseChunk } from './useTurnStream'

/** 终态阶段：收到它们就收流（与后端 `_TERMINAL_STAGES` 同源）。 */
export const TERMINAL_STAGES = ['done', 'error', 'cancelled']

const MAX_RECONNECT = 3
const RECONNECT_DELAY_MS = 1200

export function useJobStream() {
  const events = ref([]) // [{stage, message, ts, ...}]，服务端回放顺序
  const running = ref(false) // 流还在跑（= 已订阅且未收到终态）
  const disconnected = ref(false) // 重连也用完了，任务可能还在服务端跑
  const error = shallowRef('')

  let currentJob = null
  let attempts = 0

  function isTerminal(ev) {
    return TERMINAL_STAGES.includes(ev && ev.stage)
  }

  /** 拉一次任务快照（终态的 result/cost 只有它带得全）。 */
  async function snapshot(jobId) {
    const res = await fetch(`/api/packs/generate/${jobId}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  }

  /**
   * 订阅一个任务的进度。**会一直读到终态**（或重连次数用尽）。
   * 返回最后一次拿到的任务快照（终态时含 `result` / `cost`）。
   */
  async function watch(jobId) {
    currentJob = jobId
    attempts = 0
    running.value = true
    disconnected.value = false
    error.value = ''
    events.value = [] // 服务端会回放，故每次都重建（见文件头注释）
    return connect()
  }

  async function connect() {
    const jobId = currentJob
    try {
      const res = await fetch(`/api/packs/generate/${jobId}/events`)
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let sawTerminal = false

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })

        let idx
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const block = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          // 心跳是 SSE 注释帧（`: keepalive`），parseSseChunk 会忽略它——正常路径。
          for (const { event, data } of parseSseChunk(block)) {
            if (event !== 'progress') continue
            const payload = JSON.parse(data)
            events.value.push(payload)
            if (isTerminal(payload)) sawTerminal = true
          }
        }
      }

      if (sawTerminal) {
        running.value = false
        return await snapshot(jobId) // 终态快照带 result/cost
      }

      // 流断了但没有终态事件：任务在服务端可能还在跑，重连（会重新回放）
      if (attempts < MAX_RECONNECT) {
        attempts += 1
        events.value = [] // 重连即重建，避免回放造成重复行
        await new Promise((r) => setTimeout(r, RECONNECT_DELAY_MS))
        return connect()
      }
      disconnected.value = true
      running.value = false
      error.value = '进度连接已断开（任务仍在服务端运行，可刷新页面重新接上）'
      return await snapshot(jobId)
    } catch (e) {
      if (attempts < MAX_RECONNECT) {
        attempts += 1
        events.value = []
        await new Promise((r) => setTimeout(r, RECONNECT_DELAY_MS))
        return connect()
      }
      disconnected.value = true
      running.value = false
      error.value = `进度流中断：${e?.message || e}`
      return null
    }
  }

  function stop() {
    currentJob = null
    attempts = MAX_RECONNECT // 让进行中的重连循环放弃
    running.value = false
  }

  return { events, running, disconnected, error, watch, stop, snapshot }
}
