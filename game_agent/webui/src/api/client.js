/**
 * 后端 API 的薄封装（唯一一处知道端点形状的地方）。
 *
 * 为什么值得单独一个文件：组件里散落 `fetch("/api/...")` 时，端点改名只有运行时
 * 才会发现；集中在这里之后，`tests/test_webui_build.py` 可以直接核对
 * "这里声明的端点集"与 `web.py` 实际提供的路由是否一致。
 *
 * 约定：所有方法失败时抛 `ApiError`（带后端的 `detail` 原文）。后端在 400 里
 * 给的是**可行动**的理由（"未知的世界包" / "存档与当前剧本不是同一份内容"），
 * 前端一律原样呈现给玩家，不吞掉也不改写成"操作失败"。
 */

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function jsonOrThrow(res, what) {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      if (body && body.detail) detail = body.detail
    } catch {
      /* 非 JSON 错误体：保留 HTTP 状态码 */
    }
    throw new ApiError(`${what}失败：${detail}`, res.status)
  }
  return res.json()
}

const post = (url, body) =>
  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })

const del = (url) => fetch(url, { method: 'DELETE' })

/** 端点清单（与 web.py 的路由一一对应；守卫会核对）。 */
export const ENDPOINTS = Object.freeze({
  config: 'GET /api/config',
  catalog: 'GET /api/catalog',
  saves: 'GET /api/saves',
  sessions: 'GET /api/sessions',
  newGame: 'POST /api/new',
  meta: 'GET /api/{sid}/meta',
  status: 'GET /api/{sid}/status',
  actions: 'GET /api/{sid}/actions',
  cost: 'GET /api/{sid}/cost',
  turn: 'POST /api/{sid}/turn',
  save: 'POST /api/{sid}/save',
  load: 'POST /api/{sid}/load',
  // N7：存档点 / 回退 / 分支
  timeline: 'GET /api/{sid}/timeline',
  history: 'GET /api/{sid}/history',
  rewind: 'POST /api/{sid}/rewind',
  // 创作工作台（N2a）：生成 → 进度 → 草稿 → 发布
  generate: 'POST /api/packs/generate',
  jobs: 'GET /api/packs/generate',
  job: 'GET /api/packs/generate/{job_id}',
  jobEvents: 'GET /api/packs/generate/{job_id}/events',
  cancelJob: 'POST /api/packs/generate/{job_id}/cancel',
  drafts: 'GET /api/packs/drafts',
  publish: 'POST /api/packs/publish',
  deleteDraft: 'DELETE /api/packs/drafts/{name}',
  packCost: 'GET /api/packs/{name}/cost',
  packInjection: 'GET /api/packs/{name}/injection',
  // C5：质量门禁按钮（服务端强制成本前置确认）
  runGate: 'POST /api/packs/{name}/gate',
  gates: 'GET /api/gates',
  gate: 'GET /api/gates/{gate_id}',
  // 创作者 Agent（N6）：拿现成的卡来改 → 对话式改人物设定与世界书
  fork: 'POST /api/packs/fork',
  creatorState: 'GET /api/creator/{name}',
  creatorChat: 'POST /api/creator/{name}/chat',
  creatorReset: 'DELETE /api/creator/{name}',
})

export const api = {
  /** 前端配置（自由输入文案来自引擎常量，避免前后端各写一份而漂移）。 */
  config: () => fetch('/api/config').then((r) => jsonOrThrow(r, '读取配置')),

  catalog: () => fetch('/api/catalog').then((r) => jsonOrThrow(r, '读取世界包目录')),
  saves: () => fetch('/api/saves').then((r) => jsonOrThrow(r, '读取存档列表')),
  sessions: () => fetch('/api/sessions').then((r) => jsonOrThrow(r, '读取会话列表')),

  /** 开局：pack_id 选卡，mode 选"跟主线走 / 自由探索"，draft 试玩未发布的草稿。
   *
   *  `draft` 与 `pack_id` 互斥（后端优先 draft）。草稿试玩**必须**走这个入参而不是
   *  `pack_id`：`/api/catalog` 里根本没有草稿，硬塞进 pack_id 会被后端以"未知的世界包"拒绝。
   */
  newGame: (packId, mode, draft = null) =>
    post('/api/new', { pack_id: packId, mode, draft }).then((r) => jsonOrThrow(r, '开局')),

  meta: (sid) => fetch(`/api/${sid}/meta`).then((r) => jsonOrThrow(r, '读取会话信息')),
  status: (sid) => fetch(`/api/${sid}/status`).then((r) => jsonOrThrow(r, '读取状态')),
  actions: (sid) => fetch(`/api/${sid}/actions`).then((r) => jsonOrThrow(r, '读取行动')),
  cost: (sid) => fetch(`/api/${sid}/cost`).then((r) => jsonOrThrow(r, '读取成本')),

  save: (sid, path) => post(`/api/${sid}/save`, { path }).then((r) => jsonOrThrow(r, '存档')),
  load: (sid, path) => post(`/api/${sid}/load`, { path }).then((r) => jsonOrThrow(r, '读档')),

  // ---- N7：存档点 / 回退 / 分支 ----

  /** 本局时间线（每个 revision：回合/天数/标签/分支/是否当前）。 */
  timeline: (sid) => fetch(`/api/${sid}/timeline`).then((r) => jsonOrThrow(r, '读取时间线')),
  /** 本局叙事历史——刷新页面或回退之后重建正文栏（正文此前只活在前端内存里）。 */
  history: (sid) => fetch(`/api/${sid}/history`).then((r) => jsonOrThrow(r, '读取历史正文')),
  /** 回到某一版：后端产生**新版本 + 新分支**（旧分支原地保留），并返回重建后的历史。 */
  rewind: (sid, rev) => post(`/api/${sid}/rewind`, { rev }).then((r) => jsonOrThrow(r, '回退')),

  // ---- 创作工作台（N2a）----

  /** 起一个后台生成任务（立刻返回 job_id；进度看 SSE）。会**真的花钱**，除非 offline。 */
  generate: (body) => post('/api/packs/generate', body).then((r) => jsonOrThrow(r, '起生成任务')),
  /** 任务列表（新的在前）——刷新页面后靠它找回正在跑的任务。 */
  jobs: () => fetch('/api/packs/generate').then((r) => jsonOrThrow(r, '读取任务列表')),
  /** 任务终态快照：`result`（stages/repairs/summary…）与 `cost` 只有它带得全。 */
  job: (jobId) => fetch(`/api/packs/generate/${jobId}`).then((r) => jsonOrThrow(r, '读取任务')),
  cancelJob: (jobId) =>
    post(`/api/packs/generate/${jobId}/cancel`).then((r) => jsonOrThrow(r, '取消任务')),

  /** 草稿列表（含未通过校验的，带 `error` 原文）。**已发布的卡不在这里**。 */
  drafts: () => fetch('/api/packs/drafts').then((r) => jsonOrThrow(r, '读取草稿列表')),
  /** 发布到已发布区。**闸门 = check_worldpack**，不过则 400 且 detail 是报错原文。 */
  publish: (name) => post('/api/packs/publish', { name }).then((r) => jsonOrThrow(r, '发布')),
  deleteDraft: (name) =>
    del(`/api/packs/drafts/${encodeURIComponent(name)}`).then((r) => jsonOrThrow(r, '删除草稿')),
  /** N5/C3：**按卡累计**成本（游玩 + 改卡都在内，因为两边带同一个 pack 轴）。 */
  packCost: (name) =>
    fetch(`/api/packs/${encodeURIComponent(name)}/cost`).then((r) => jsonOrThrow(r, '读取这张卡的成本')),
  /** C4：**注入提醒**扫描（不是门禁——后端 `is_gate: false`，UI 据此只标黄）。 */
  packInjection: (name) =>
    fetch(`/api/packs/${encodeURIComponent(name)}/injection`)
      .then((r) => jsonOrThrow(r, '读取注入提醒')),

  /** C5：跑一道质量门。**会花钱**，所以必须先拿到成本说明再带 confirm=true 调。 */
  runGate: (name, kind, confirm = false) =>
    post(`/api/packs/${encodeURIComponent(name)}/gate`, { kind, confirm })
      .then((r) => jsonOrThrow(r, '起门禁')),
  gates: () => fetch('/api/gates').then((r) => jsonOrThrow(r, '读取门禁记录')),
  gate: (id) => fetch(`/api/gates/${encodeURIComponent(id)}`).then((r) => jsonOrThrow(r, '读取门禁结果')),

  // ---- 创作者 Agent（N6）----

  /** 把**已发布**的卡复制成草稿——"改一张现成的卡"的第一步（原版只读，不会被动）。 */
  fork: (name) => post('/api/packs/fork', { name }).then((r) => jsonOrThrow(r, '复制成草稿')),
  /** 创作会话现状（对话记录 + diff + 校验结论）。刷新页面靠它接上。 */
  creatorState: (name) =>
    fetch(`/api/creator/${encodeURIComponent(name)}`).then((r) => jsonOrThrow(r, '读取创作会话')),
  /** 清空对话上下文（**不动草稿内容**）。 */
  creatorReset: (name) =>
    del(`/api/creator/${encodeURIComponent(name)}`).then((r) => jsonOrThrow(r, '重置对话')),

  /** 回合请求体（kind 决定后端分发哪个 Game 方法）。 */
  turnBody: {
    say: (text) => ({ kind: 'say', text }),
    pick: (index) => ({ kind: 'pick', index }),
    act: (actionId) => ({ kind: 'act', action_id: actionId }),
    endDay: () => ({ kind: 'end_day' }),
  },
}
