/**
 * 创作者 Agent 的对话流（POST + SSE）——N6。
 *
 * 与 `useTurnStream`（叙事回合）同一形状，因为**断线语义相同**：都是一个 POST 触发
 * 一次"跑一段就没了"的工作，所以同样**不能用 `EventSource`**
 * （它只支持 GET，且断线自动重连会把这一轮重跑一遍——对创作者来说就是
 * 把同一句"把她的性格改冷一点"执行两遍，改动重复落盘）。
 *
 * 与 `useJobStream`（后台任务）不同：那个是 GET + 服务端回放，断线重连是安全的；
 * 这里**断线就是不重连**，如实告诉作者"这一轮断了，改动可能只落了一部分，
 * 看 diff 确认"——因为服务端的工作线程仍在跑（锁的生存期与客户端无关），
 * 盲目重连会触发第二轮模型调用，那才是真的花钱重复。
 *
 * 事件（与 `web._creator_stream` 一一对应）：
 * - `tool`：模型调了一次工具（名字 + 成了没有 + 结果摘要）；
 * - `text`：模型说的话（中间态，`done.reply` 才是最终答复）；
 * - `done`：{reply, tools, validate_ok, validate_text, changed, diff, truncated}
 * - `error`：这一轮失败。
 */

import { ref, shallowRef } from 'vue'

import { parseSseChunk } from './useTurnStream'

export function useCreatorStream() {
  const running = ref(false)
  const messages = ref([]) // [{role: 'user'|'assistant', content}]（只留人话）
  const steps = ref([]) // 本轮的 [{type:'tool'|'text', ...}]，给人看过程
  const error = shallowRef('')
  const reply = shallowRef('')
  const summary = shallowRef('') // 工作版摘要（Agent 看到的这张卡是什么样）
  const result = shallowRef(null) // done 的载荷（校验结论 / changed / diff）
  const startedJob = shallowRef(null) // Agent 从素材起的新任务（{job_id, pack_name}）

  /** 发一句话。返回 `done` 的载荷；失败返回 null（错误写进 `error`/`steps`）。 */
  async function send(name, text) {
    running.value = true
    error.value = ''
    reply.value = ''
    result.value = null
    steps.value = []
    startedJob.value = null
    messages.value.push({ role: 'user', content: text })
    try {
      const res = await fetch(`/api/creator/${encodeURIComponent(name)}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      })
      if (!res.ok || !res.body) {
        let detail = `HTTP ${res.status}`
        try {
          const body = await res.json()
          if (body && body.detail) detail = body.detail
        } catch {
          /* 非 JSON 错误体 */
        }
        throw new Error(detail)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let done = null

      for (;;) {
        const { done: streamDone, value } = await reader.read()
        if (streamDone) break
        buf += decoder.decode(value, { stream: true })

        let idx
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const frames = parseSseChunk(buf.slice(0, idx))
          buf = buf.slice(idx + 2)
          for (const { event, data } of frames) {
            const payload = JSON.parse(data)
            if (event === 'tool') {
              steps.value.push({ type: 'tool', ...payload })
            } else if (event === 'text') {
              reply.value = payload.text // 中间态；done.reply 覆盖它
              steps.value.push({ type: 'text', text: payload.text })
            } else if (event === 'job') {
              // Agent 从素材起了一个**新**任务（start_generation 工具）。
              // 不在这里等它——把任务交给调用方，由它切到进度页签去订阅那条流。
              startedJob.value = payload
              steps.value.push({
                type: 'tool',
                name: 'start_generation',
                status: 'ok',
                result: `已起任务 ${payload.job_id}（${payload.pack_name}）`,
              })
            } else if (event === 'done') {
              done = payload
              reply.value = payload.reply || reply.value
              result.value = payload
            } else if (event === 'error') {
              error.value = payload.message || String(payload)
              steps.value.push({ type: 'error', text: error.value })
            }
          }
        }
      }

      if (done) {
        messages.value.push({ role: 'assistant', content: done.reply || '（无输出）' })
      } else if (!error.value) {
        // 流断了且没有 done：**不重连**（会重复花钱、重复改），如实说明。
        error.value = '这一轮连接中断——改动可能只落了一部分，请看右栏 diff 确认后再决定要不要重说。'
      }
      return done
    } catch (e) {
      error.value = String(e?.message || e)
      return null
    } finally {
      running.value = false
    }
  }

  /** 用服务端现状覆盖本地状态（刷新页面 / 选另一张草稿时）。 */
  function hydrate(state) {
    messages.value = (state && state.messages) || []
    steps.value = []
    reply.value = ''
    error.value = ''
    // 摘要也要接过来：它回答"Agent 现在看到的这张卡是什么样"——
    // 这是作者判断"它有没有搞错对象"的第一眼依据（少了它，只有一堆计数可看）。
    summary.value = (state && state.summary) || ''
    result.value =
      state && state.validate_ok !== null
        ? {
            validate_ok: state.validate_ok,
            validate_text: state.validate_text,
            changed: state.changed || [],
            diff: state.diff || '',
            reply: '',
          }
        : null
  }

  return { running, messages, steps, error, reply, summary, result, startedJob, send, hydrate }
}
