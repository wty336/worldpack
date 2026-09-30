/**
 * 回合流（POST + SSE）——Stage B 里最关键的一个文件。
 *
 * **为什么不能用 `EventSource`**（两条，第二条会真的出 bug）：
 *
 * 1. `EventSource` 只支持 **GET**，而 `/api/{sid}/turn` 是 **POST**（要带 body）；
 * 2. 更严重：`EventSource` 断线会**自动重连**。对一个"跑一个回合"的端点，
 *    自动重连 = **把回合重跑一遍**——重复扣费、重复落盘。
 *
 * 所以用 `fetch` + `ReadableStream` 手动解析 SSE 帧。
 *
 * **两个必须记住的线上契约**（踩过就知道有多隐蔽）：
 *
 * - `delta` 与 `error` 的 `data` 是**裸 JSON 字符串**，不是对象：
 *   后端是 `deltas.put(("delta", piece))` 再 `json.dumps(payload)`
 *   （`web.py` 的 `_make_stream_sink` / `_turn_stream`），
 *   所以 `JSON.parse(data)` 得到的是 `"那女子立于阶下…"`，**不是** `{text: ...}`。
 *   `done` 才是对象（`_view()` 的字典）。写成 `payload.text` 会静默拿到 undefined，
 *   表现为"正文一直空白、但状态栏正常更新"。
 *
 * - **`done.narration` 才是真值，累积的 deltas 只是"活着的感觉"**：
 *   引擎会在回合内部作废重来（`_txn_rollback`：empty_narration / meltdown /
 *   critique_regenerate），已经推给前端的增量可能属于一份**被丢弃的草稿**。
 *   故 `done` 到达时必须用 `narration` 覆盖草稿。
 */

import { ref, shallowRef } from 'vue'

/** 把一段 SSE 文本切成 [{event, data}]。后端每帧只有一行 data（json.dumps 会转义换行）。 */
export function parseSseChunk(text) {
  const out = []
  for (const block of text.split('\n\n')) {
    if (!block.trim()) continue
    let event = 'message'
    let data = ''
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data += line.slice(5).trim()
    }
    if (!data) continue
    out.push({ event, data })
  }
  return out
}

export function useTurnStream() {
  const streaming = ref(false)
  const draft = shallowRef('') // 流式草稿（仅展示用）
  const error = shallowRef('')
  const recovered = shallowRef([]) // 本轮用过的恢复手段（引擎口径）
  const subTurns = shallowRef(0)

  /**
   * 发一个回合。`view` 是 `done` 的载荷（`_view()`），交给调用方渲染。
   * 返回 done 的载荷；失败返回 null（错误已写进 `error`）。
   */
  async function send(sid, body) {
    streaming.value = true
    draft.value = ''
    error.value = ''
    recovered.value = []
    subTurns.value = 0
    try {
      const res = await fetch(`/api/${sid}/turn`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      let view = null

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })

        let idx
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const frames = parseSseChunk(buf.slice(0, idx))
          buf = buf.slice(idx + 2)
          for (const { event, data } of frames) {
            const payload = JSON.parse(data)
            if (event === 'delta') {
              draft.value += payload // ← 裸字符串
            } else if (event === 'done') {
              view = payload
              draft.value = payload.narration || '' // ← 以终稿为准
              recovered.value = payload.recovered || []
              subTurns.value = payload.sub_turns || 0
            } else if (event === 'error') {
              error.value = payload // ← 也是裸字符串
            }
          }
        }
      }
      return view
    } catch (e) {
      error.value = String(e?.message || e)
      return null
    } finally {
      streaming.value = false
    }
  }

  return { streaming, draft, error, recovered, subTurns, send }
}
