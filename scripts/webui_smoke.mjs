/**
 * Web 前端真机冒烟（Stage B）：用无头浏览器**驱动真实界面**并断言渲染结果。
 *
 * 为什么需要它：pytest 侧的守卫能读源码、能核对端点、能查构建是否过期，
 * 但**都验证不了"Vue 到底有没有渲染出来"**——一次 `v-if` 写反、一个插槽名写错，
 * 全部测试仍然绿，而浏览器里是一片空白。前端重写最需要验证的恰恰是这件事。
 *
 * 设计取舍：
 * - **自带桩后端**（不依赖 DEEPSEEK_API_KEY，不花钱、不联网、可重复）：
 *   本脚本自己在内存里实现 `/api/*` 的固定应答并托管 `dist/`，因此它验证的是
 *   **前端消费契约的能力**，而不是模型质量——后者由 `scripts/worldpack_smoke.py`
 *   那类要花钱的真机冒烟负责。两者互补，不重复。
 * - **走 CDP 而不是 `--dump-dom`**：`--dump-dom` 只能给一份静态快照，
 *   而"点一张卡 → 开始这一局 → 三栏出现"必须真的点击。Node 22 自带全局
 *   `WebSocket`，因此不引入任何 npm 依赖。
 * - **不断言像素**：只断言"该出现的结构与数据出现在 DOM 里"。
 *   布局细节（grid 列宽）由 `tests/test_web_frontend.py` 在 CSS 层守。
 *
 * 用法：
 *   node scripts/webui_smoke.mjs                 # 自动找 Edge/Chrome
 *   node scripts/webui_smoke.mjs --browser <路径>  # 指定浏览器可执行文件
 *   node scripts/webui_smoke.mjs --keep          # 失败时保留截图与 DOM 便于排查
 */

import { createServer } from 'node:http'
import { readFileSync, existsSync, writeFileSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { extname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const DIST = join(ROOT, 'game_agent', 'webui', 'dist')

const args = process.argv.slice(2)
const keep = args.includes('--keep')
const browserArg = args.indexOf('--browser')
const CANDIDATES = [
  browserArg >= 0 ? args[browserArg + 1] : null,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].filter(Boolean)

// ---------------------------------------------------------------------------
// 桩后端：固定应答 + 托管 dist/
// ---------------------------------------------------------------------------

const FREE_INPUT = '（自己说些什么…）'
const VIEW_OPENING = {
  narration: '你挡在了她身前。她敛衽一礼：多谢公子。',
  choices: ['拱手报上姓名', '只说姓，不提名', FREE_INPUT],
  briefing: '长安城·沈府门前',
  ending: null,
  choice_prompt: { prompt: '几个纨绔正纠缠一位姑娘。你如何解围？' },
  recovered: [],
  sub_turns: 1,
  turn: 1,
}
const VIEW_DAILY = {
  // 真机上的 `done.narration` 就是刚刚流出去的那段正文（引擎先流后提交），
  // 这里照此还原，好让"delta 渲染"与"done 覆盖"两条路径都能被观察到。
  narration: '她侧身让开半步，随即记下了你的名字。天色渐晚。',
  choices: ['去后山修炼', '进城打听消息', FREE_INPUT],
  briefing: null,
  ending: null,
  choice_prompt: null,
  recovered: ['overflow_recovered'],
  sub_turns: 2,
  turn: 2,
}
/** 流式增量：与 done.narration 同源（真实引擎就是这个形状）。 */
const DELTAS = ['她侧身让开半步，', '随即记下了你的名字。', '天色渐晚。']

// ---- 创作工作台（N2a）桩 ----
//
// 为什么这一屏也必须有真机冒烟：工作台的价值全在"Vue 真的把进度流画出来了吗"——
// 端点对不对、源码里有没有 `getReader()`，都拦不住一个写反的 `v-if`。
// 桩里刻意留了一条 `repair` 事件与一份坏草稿，好让"修复轮报错原文"与
// "闸门不过要说清"这两块可被观察。
const JOB_ID = 'job_smoke1'
const JOB_EVENTS = [
  { stage: 'start', message: '开始生成：smoke_draft', ts: 1 },
  { stage: 'extract', message: '生成[world]', ts: 2 },
  { stage: 'extract', message: '生成[npc1]', ts: 3 },
  { stage: 'repair', message: '[修复 1] 主线节点 n2 的 flag 没有任何路径可写', ts: 4 },
  { stage: 'validate', message: '[✓] check-worldpack 通过（修复 1 轮）', ts: 5 },
  { stage: 'done', message: '[✓] 世界包已生成 → world-packs/_drafts/smoke_draft', ts: 6 },
]
const JOB_FINAL = {
  job_id: JOB_ID,
  pack_name: 'smoke_draft',
  status: 'done',
  error: null,
  result: {
    pack_dir: 'world-packs/_drafts/smoke_draft',
    pack_name: 'smoke_draft',
    repairs: 1,
    corpus_written: 0,
    stages: ['start', 'extract', 'repair', 'validate', 'done'],
    summary: '离线测试世界 · 2 角色 · 3 主线节点 · 2 结局',
  },
  cost: '¥0.012（12 次调用）',
}
const mkDraft = (id, name, extra = {}) => ({
  id, name, era: '架空', npcs: 2, nodes: 3, endings: 2, lore: 4, locations: 2,
  digest: 'b'.repeat(16), draft: true, playable: true, error: '',
  path: `world-packs/_drafts/${id}`, ...extra,
})
const DRAFTS = [
  mkDraft('smoke_draft', '草稿·过校验的一版'),
  // 列表里是好的、发布时才发现坏了——这不是构造出来的场景：草稿就在文件系统上，
  // 作者（或创作者 Agent）随时可能在两次请求之间改动它。这一条用来验"被拒时
  // 报错原文有没有真的显示给作者"。
  mkDraft('race_draft', '草稿·发布时才发现坏了'),
  mkDraft('broken_draft', '草稿·没通过校验', {
    playable: false, error: '缺少文件: world-packs/_drafts/broken_draft/mainline.yaml',
  }),
]
const PUBLISH_REJECTED =
  '草稿未通过 check-worldpack，不能发布：\n' +
  '缺少文件: world-packs/_drafts/race_draft/mainline.yaml\n' +
  '（web 创作工作台会把这段报错原文给模型去修；也可以在草稿目录里手工改）'

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript', '.css': 'text/css; charset=utf-8', '.json': 'application/json' }

function serveDist(res, rel) {
  const p = join(DIST, rel)
  if (!existsSync(p)) {
    res.writeHead(404).end('not found')
    return false
  }
  res.writeHead(200, { 'content-type': MIME[extname(p)] || 'application/octet-stream' })
  res.end(readFileSync(p))
  return true
}

function startStub() {
  const state = { turns: 0 }
  const readBody = (req) =>
    new Promise((resolve) => {
      let b = ''
      req.on('data', (c) => (b += c))
      req.on('end', () => {
        try {
          resolve(JSON.parse(b || '{}'))
        } catch {
          resolve({})
        }
      })
    })
  const server = createServer((req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1')
    const path = url.pathname
    const json = (o, code = 200) => {
      res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' })
      res.end(JSON.stringify(o))
    }
    // ---- 创作工作台（N2a）----
    if (path === '/api/packs/drafts' && req.method === 'GET')
      return json({ ok: true, drafts: DRAFTS })
    if (path === '/api/packs/generate' && req.method === 'POST')
      return json({ ok: true, job_id: JOB_ID, pack_name: 'smoke_draft', status: 'queued' }, 202)
    if (path === '/api/packs/generate' && req.method === 'GET')
      return json({ ok: true, jobs: [JOB_FINAL] })
    if (path === `/api/packs/generate/${JOB_ID}`) return json({ ok: true, ...JOB_FINAL })
    if (path === `/api/packs/generate/${JOB_ID}/events`) {
      // 事件之间留间隔：好让"边跑边看"（日志一行行长出来）真的可被观察，
      // 而不是所有事件挤在一帧里——后者会掩盖"其实是攒完再一次性画"的退化。
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store' })
      res.write(`event: start\ndata: ${JSON.stringify({ job_id: JOB_ID, status: 'running' })}\n\n`)
      let i = 0
      const tick = setInterval(() => {
        if (i < JOB_EVENTS.length) {
          res.write(`event: progress\ndata: ${JSON.stringify(JOB_EVENTS[i++])}\n\n`)
          return
        }
        clearInterval(tick)
        res.end()
      }, 100)
      return
    }
    if (path === '/api/packs/publish') {
      void readBody(req).then((body) => {
        if (body.name === 'race_draft') return json({ detail: PUBLISH_REJECTED }, 400)
        json({ ok: true, pack: { ...mkDraft(body.name, body.name), draft: false } })
      })
      return
    }
    if (path.startsWith('/api/packs/drafts/') && req.method === 'DELETE')
      return json({ ok: true, deleted: decodeURIComponent(path.split('/').pop()) })

    if (path === '/') return void serveDist(res, 'index.html')
    if (path.startsWith('/static/')) return void serveDist(res, path.slice('/static/'.length))
    if (path === '/api/config') return json({ ok: true, free_input: FREE_INPUT, default_mode: 'story' })
    if (path === '/api/catalog')
      return json({
        ok: true,
        default: 'demo_world',
        packs: [
          { id: 'demo_world', name: '演示世界', era: '架空古代·长安城', npcs: 2, nodes: 3, endings: 2, lore: 5, locations: 2, digest: 'a'.repeat(16), playable: true, error: '', path: 'world-packs/demo_world' },
          { id: 'broken_world', name: '坏包', era: '', npcs: 0, nodes: 0, endings: 0, lore: 0, locations: 0, digest: '', playable: false, error: '缺少文件: mainline.yaml', path: 'world-packs/broken_world' },
        ],
      })
    if (path === '/api/saves') return json({ ok: true, saves: [] })
    if (path === '/api/sessions') return json({ ok: true, sessions: [] })
    if (path === '/api/new') {
      state.turns = 0
      // 草稿试玩：`draft` 选项（§3.2 ③）——回包里带 `draft: true`，
      // 前端据此在顶栏打「未发布」水印。
      return void readBody(req).then((body) =>
        json({
          sid: 'smoke1',
          name: body.draft ? '草稿·过校验的一版' : '演示世界',
          pack_id: body.draft || 'demo_world',
          mode: body.mode || 'story',
          draft: Boolean(body.draft),
          view: VIEW_OPENING,
        }),
      )
    }
    if (/^\/api\/[^/]+\/status$/.test(path))
      return json({ text: '【场景】长安城·沈府门前\n【属性】charm 10 · martial 21\n【在场角色】沈清秋（好感 16）', ending: false })
    if (/^\/api\/[^/]+\/actions$/.test(path))
      // 开局是关键抉择期（critical=true，行动区说明不可用）；
      // 走过一个回合后抉择已解决 → critical=false，行动按钮出现。
      return json(
        state.turns > 0
          ? { day: 1, action_points_left: 1, critical: false, actions: [{ id: 'cultivate', label: '去后山修炼' }, { id: 'gift', label: '赠礼' }] }
          : { day: 1, action_points_left: 1, critical: true, actions: [{ id: 'cultivate', label: '去后山修炼' }] },
      )
    if (/^\/api\/[^/]+\/meta$/.test(path))
      return json({ ok: true, sid: 'smoke1', pack_id: 'demo_world', name: '演示世界', mode: 'story', turn: 1, day: 1 })
    if (/^\/api\/[^/]+\/cost$/.test(path)) return json({ ok: true, calls: 0, path: 'saves/usage-smoke1.jsonl', report: '' })
    if (/^\/api\/[^/]+\/turn$/.test(path)) {
      // SSE：start → delta…（带间隔，好让冒烟能观察到流式态）→ done
      // 形状与 web.py 一致：delta 的 data 是**裸 JSON 字符串**。
      //
      // 注意这里的回合序号口径：开局那一屏来自 `POST /api/new` 的 `view`，
      // **任何** turn 都返回 VIEW_DAILY。第一版写成 `state.turns > 1 ? DAILY : OPENING`，
      // 于是第一个 turn 又回了开局视图——断言因此"通过"得毫无意义（它匹配到的是
      // 流式草稿，而不是提交后的终稿）。桩自己的状态机错了，比没有桩更危险。
      state.turns += 1
      const view = VIEW_DAILY
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store' })
      res.write(`event: start\ndata: ${JSON.stringify({ kind: 'say' })}\n\n`)
      let i = 0
      const tick = setInterval(() => {
        if (i < DELTAS.length) {
          res.write(`event: delta\ndata: ${JSON.stringify(DELTAS[i++])}\n\n`)
          return
        }
        clearInterval(tick)
        res.write(`event: done\ndata: ${JSON.stringify(view)}\n\n`)
        res.end()
      }, 120)
      return
    }
    json({ detail: `stub 未实现: ${path}` }, 404)
  })
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)))
}

// ---------------------------------------------------------------------------
// 极简 CDP 客户端（Node 22 自带全局 WebSocket，无需依赖）
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function findBrowser() {
  for (const c of CANDIDATES) if (existsSync(c)) return c
  throw new Error(`找不到浏览器，请用 --browser <路径> 指定。已尝试：\n  ${CANDIDATES.join('\n  ')}`)
}

class Cdp {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result)
      }
    })
  }
  send(method, params = {}) {
    const id = ++this.id
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id)
          reject(new Error(`CDP 超时: ${method}`))
        }
      }, 15000)
    })
  }
  /** 在页面里求值并把结果按值返回。 */
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: true,
    })
    if (r.exceptionDetails) throw new Error(`页面内异常: ${r.exceptionDetails.text}`)
    return r.result.value
  }
  /** 轮询直到表达式为真（返回其真值）。 */
  async waitFor(expression, { timeout = 10000, label = expression } = {}) {
    const t0 = Date.now()
    for (;;) {
      if (await this.eval(expression)) return true
      if (Date.now() - t0 > timeout) throw new Error(`等待超时：${label}`)
      await sleep(150)
    }
  }
}

async function connect(port) {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await fetch(`http://127.0.0.1:${port}/json/list`).then((r) => r.json())
      const page = list.find((t) => t.type === 'page')
      if (page?.webSocketDebuggerUrl) {
        const ws = new WebSocket(page.webSocketDebuggerUrl)
        await new Promise((res, rej) => {
          ws.addEventListener('open', res, { once: true })
          ws.addEventListener('error', rej, { once: true })
        })
        return new Cdp(ws)
      }
    } catch {
      /* 浏览器还没起来 */
    }
    await sleep(250)
  }
  throw new Error('连不上浏览器的调试端口')
}

// ---------------------------------------------------------------------------
// 断言
// ---------------------------------------------------------------------------

const results = []
function check(name, ok, extra = '') {
  results.push({ name, ok, extra })
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${name}${extra ? `  — ${extra}` : ''}`)
}

async function main() {
  if (!existsSync(join(DIST, 'index.html'))) {
    throw new Error('缺少 dist/index.html——先在 game_agent/webui/ 下 `npm run build`')
  }
  const browser = await findBrowser()
  const server = await startStub()
  const port = server.address().port
  const userDataDir = mkdtempSync(join(tmpdir(), 'webui-smoke-'))
  const cdpPort = 9333 + Math.floor(Math.random() * 400)

  console.log(`桩后端: http://127.0.0.1:${port}`)
  console.log(`浏览器: ${browser}`)
  const proc = spawn(
    browser,
    [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--disable-extensions', '--hide-scrollbars', '--window-size=1440,900',
      `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${userDataDir}`, 'about:blank',
    ],
    { stdio: 'ignore' },
  )

  let cdp
  try {
    cdp = await connect(cdpPort)
    await cdp.send('Page.enable')
    await cdp.send('Runtime.enable')
    await cdp.send('Page.navigate', { url: `http://127.0.0.1:${port}/` })

    // ---- 选卡屏 ----
    await cdp.waitFor(`!!document.querySelector('#app .library')`, { label: '选卡屏渲染' })
    check('Vue 挂载并渲染出选卡屏', true)
    // 卡片是**异步**取回 `/api/catalog` 之后才渲染的：`.library` 会先出现（空列表），
    // 所以要等卡片本身，而不是等容器。第一版直接数 `.pack`，在真机（8 个包、
    // 首次列举要跑 8 次 load_worldpack）上偶发数到 0——那会把测试的竞态
    // 报成产品缺陷。
    await cdp.waitFor(`document.querySelectorAll('#app .pack').length >= 2`, {
      label: '卡片列表加载完成',
    })
    const packCount = await cdp.eval(`document.querySelectorAll('#app .pack').length`)
    check('卡片列表渲染出全部包（含坏包）', packCount === 2, `渲染 ${packCount} 张`)
    const brokenDisabled = await cdp.eval(
      `!!document.querySelector('#app .pack.broken')?.disabled`,
    )
    check('坏包以禁用态呈现（看得见而不是消失）', brokenDisabled)
    check(
      '模式单选与开始按钮就位',
      await cdp.eval(
        `document.querySelectorAll('#app input[type=radio]').length === 2 &&
         !!document.querySelector('#app .save-row button')`,
      ),
    )

    // ---- 点卡 → 开始 ----
    await cdp.eval(`document.querySelector('#app .pack:not(.broken)').click()`)
    await cdp.eval(`document.querySelector('#app .save-row button').click()`)

    // ---- 三栏游戏屏 ----
    await cdp.waitFor(`!!document.querySelector('#app .col-mid')`, { label: '进入游戏屏' })
    check('进入游戏屏', true)
    const cols = await cdp.eval(
      `['.col-left','.col-mid','.col-right'].filter(s => document.querySelector('#app ' + s)).length`,
    )
    check('三栏（左本局 / 中正文 / 右状态栏）全部渲染', cols === 3, `找到 ${cols}/3`)

    // ---- 正文 + 关键抉择 ----
    const prose = await cdp.eval(`document.querySelector('#app .prose')?.textContent || ''`)
    check('正文流渲染出开局叙事', prose.includes('你挡在了她身前'), prose.slice(0, 40))
    check(
      '关键抉择提示渲染',
      await cdp.eval(`/关键抉择/.test(document.querySelector('#app .col-mid').textContent)`),
    )
    const choices = await cdp.eval(`document.querySelectorAll('#app .choices button').length`)
    check('候选项按钮渲染（含自由输入项）', choices === 3, `渲染 ${choices} 个`)

    // ---- 右栏：引擎真值 ----
    const status = await cdp.eval(`document.querySelector('#app .status-text')?.textContent || ''`)
    check('右栏渲染引擎状态栏原文', status.includes('沈清秋') && status.includes('长安城'), status.slice(0, 30))
    check(
      '关键抉择期行动区说明不可用（而不是静默禁用）',
      await cdp.eval(`/关键抉择进行中/.test(document.querySelector('#app .col-right').textContent)`),
    )
    check(
      '左栏显示本局信息（卡/模式/回合）',
      await cdp.eval(
        `const t = document.querySelector('#app .col-left').textContent;
         t.includes('demo_world') && t.includes('跟着主线')`,
      ),
    )

    // ---- 走一个回合：SSE 的 delta 与 done ----
    // 按按钮**文字**定位，而不是"输入区里的第一个按钮"——真机冒烟第一版按位置选，
    // 结果命中了正文上方的「剧情回顾」，于是测试自己点错了地方、还谎报"回合超时"。
    // 选择器要表达意图，不要依赖 DOM 顺序。
    await cdp.eval(`(() => {
      const inp = document.querySelector('#app .input-dock input[type=text]');
      inp.value = '我上前一步。';
      inp.dispatchEvent(new Event('input', { bubbles: true }));
    })()`)
    const clicked = await cdp.eval(`(() => {
      const b = [...document.querySelectorAll('#app .input-dock button')]
        .find(x => x.textContent.trim() === '发言');
      if (!b) return false;
      b.click();
      return true;
    })()`)
    check('定位到「发言」按钮（按文字而非 DOM 顺序）', clicked)

    // ① 流式态：草稿区带光标，且**部分**正文已经出现（证明 delta 真的渲染了）
    await cdp.waitFor(`!!document.querySelector('#app .prose .caret')`, {
      timeout: 5000,
      label: '流式光标出现',
    })
    check('流式增量渲染（草稿 + 光标）', true)

    // ② 终稿：done.narration 覆盖草稿（引擎口径的真值）
    await cdp.waitFor(
      `/随即记下了你的名字/.test(document.querySelector('#app .prose')?.textContent || '')`,
      { label: '终稿正文渲染' },
    )
    check('一个回合走通（POST+SSE：delta 渲染 → done 提交）', true)
    // 注意：`done` 先把 narration 写进草稿、`streaming` 到 finally 才落回 false，
    // 所以上面那条会在"草稿已含终稿、光标还在"的瞬间命中——必须**等**光标消失，
    // 不能一次性断言（第一版就是这么假红的）。
    check(
      '回合后草稿被终稿接管（光标消失，不留半截文案）',
      await cdp.waitFor(
        `!document.querySelector('#app .prose .caret') &&
         !/她侧身让开半步，$/.test(document.querySelector('#app .prose').textContent.trim())`,
        { timeout: 5000, label: '光标消失且终稿完整' },
      ),
    )
    check(
      '回合后右栏刷新出行动区（关键抉择已过 → 可行动）',
      await cdp.waitFor(`!!document.querySelector('#app .col-right .status-actions button')`, {
        timeout: 5000,
        label: '行动区出现',
      }),
    )
    check(
      '恢复痕迹对玩家可见（本例为溢出恢复）',
      await cdp.eval(`/已压缩历史后重试成功/.test(document.querySelector('#app .prose').textContent)`),
    )

    const dump = await cdp.eval(`document.documentElement.outerHTML`)

    // =====================================================================
    // 创作工作台（N2a）：素材 → 进度 → 校验报告 → 试玩 → 发布
    // =====================================================================
    // 重新载入回到选卡屏（会话是内存态，刷新即回到未开局）
    await cdp.send('Page.navigate', { url: `http://127.0.0.1:${port}/` })
    await cdp.waitFor(`!!document.querySelector('#app .library')`, { label: '重载回选卡屏' })

    const toStudio = await cdp.eval(`(() => {
      const b = [...document.querySelectorAll('#app button')]
        .find(x => x.textContent.includes('创作工作台'));
      if (!b) return false;
      b.click();
      return true;
    })()`)
    check('选卡屏有「创作工作台」入口', toStudio)
    await cdp.waitFor(`!!document.querySelector('#app .studio')`, { label: '工作台渲染' })
    check('工作台渲染', true)
    const studioCols = await cdp.eval(
      `['.col-left','.col-mid','.col-right']
         .filter(s => document.querySelector('#app .studio ' + s)).length`,
    )
    check('工作台是三栏（左：任务/草稿 · 中：素材+进度 · 右：只读报告）', studioCols === 3,
      `找到 ${studioCols}/3`)

    // 草稿列表：好的 + 坏的都在（坏草稿要看得见，而不是消失）
    await cdp.waitFor(`document.querySelectorAll('#app .studio .col-left .pack').length >= 3`, {
      label: '草稿列表加载',
    })
    check(
      '草稿列表渲染出未通过校验的草稿（看得见，且标出失败）',
      await cdp.eval(`!!document.querySelector('#app .studio .col-left .pack.broken')`),
    )

    // **默认不花钱**：这是最该被钉住的一条默认值
    const offlineChecked = await cdp.eval(`(() => {
      const l = [...document.querySelectorAll('#app .studio label')]
        .find(x => x.textContent.includes('离线试跑'));
      return l ? l.querySelector('input[type=checkbox]').checked : null;
    })()`)
    check('离线试跑默认勾选（真实生成一次约 ¥0.1–0.3，默认花钱是错的默认值）',
      offlineChecked === true, String(offlineChecked))

    // ---- 坏草稿：报告栏给原文，发布按钮禁用 ----
    await cdp.eval(`(() => {
      const b = [...document.querySelectorAll('#app .studio .col-left .pack')]
        .find(x => x.textContent.includes('没通过校验'));
      b.click();
    })()`)
    await cdp.waitFor(`/未通过 check-worldpack/.test(document.querySelector('#app .studio .col-right').textContent)`,
      { label: '坏草稿报告' })
    check('闸门不过时右栏明确说清（而不是只灰掉一个按钮）', true)
    check(
      '右栏显示 check-worldpack 的**报错原文**（工作台拿它去喂模型修）',
      await cdp.eval(`/缺少文件/.test(document.querySelector('#app .studio .col-right .errbox')?.textContent || '')`),
    )
    check(
      '坏草稿的「发布」被禁用（闸门在服务端，UI 不给出注定失败的入口）',
      await cdp.eval(`(() => {
        const b = [...document.querySelectorAll('#app .studio .col-right button')]
          .find(x => x.textContent.trim() === '发布');
        return !!b && b.disabled;
      })()`),
    )
    check(
      '右栏是呈现不是表单（没有任何输入框）',
      await cdp.eval(`document.querySelectorAll('#app .studio .col-right input, #app .studio .col-right textarea').length === 0`),
    )

    // ---- 贴素材 → 生成 → 进度流 ----
    await cdp.eval(`(() => {
      const setv = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); };
      setv(document.querySelector('#app .studio .field input[type=text]'), 'smoke_draft');
      setv(document.querySelector('#app .studio textarea'), '# 素材\\n\\n某人在城里醒来，身上只有一张名片。');
    })()`)
    const started = await cdp.eval(`(() => {
      const b = [...document.querySelectorAll('#app .studio button')]
        .find(x => x.textContent.trim() === '开始生成');
      if (!b || b.disabled) return false;
      b.click();
      return true;
    })()`)
    check('填好素材后「开始生成」可用并被点到', started)

    await cdp.waitFor(`document.querySelectorAll('#app .studio .log-line').length >= 1`, {
      timeout: 8000,
      label: '进度日志出现第一行',
    })
    check('进度流渲染出事件行（边跑边看）', true)
    await cdp.waitFor(`/check-worldpack 通过/.test(document.querySelector('#app .studio .log')?.textContent || '')`,
      { timeout: 8000, label: '进度走到校验阶段' })
    const logLines = await cdp.eval(`document.querySelectorAll('#app .studio .log-line').length`)
    check('进度日志逐条累积（不是攒完再一次性画）', logLines >= 4, `${logLines} 行`)

    // ---- 终态：报告 + 修复轮报错原文 ----
    await cdp.waitFor(`!!document.querySelector('#app .studio .col-right .kv')`, {
      timeout: 8000,
      label: '生成报告渲染',
    })
    await cdp.waitFor(`/修复轮报错原文/.test(document.querySelector('#app .studio .col-right').textContent)`,
      { timeout: 8000, label: '修复轮原文渲染' })
    check(
      '修复轮的报错原文出现在报告里（来自事件留档的 repair 事件）',
      await cdp.eval(`/flag 没有任何路径可写/.test(document.querySelector('#app .studio .col-right').textContent)`),
    )
    check(
      '报告显示成本（花钱的事必须回显）',
      await cdp.eval(`/¥0\\.012/.test(document.querySelector('#app .studio .col-right').textContent)`),
    )

    // ---- 发布被拒：原文要出现在提示条里 ----
    await cdp.eval(`(() => {
      const b = [...document.querySelectorAll('#app .studio .col-left .pack')]
        .find(x => x.textContent.includes('发布时才发现坏了'));
      b.click();
    })()`)
    const pubClicked = await cdp.eval(`(() => {
      const b = [...document.querySelectorAll('#app .studio .col-right button')]
        .find(x => x.textContent.trim() === '发布');
      if (!b || b.disabled) return false;
      b.click();
      return true;
    })()`)
    check('可发布的草稿「发布」按钮可用', pubClicked)
    await cdp.waitFor(`/未通过 check-worldpack，不能发布/.test(document.querySelector('#app .notice')?.textContent || '')`,
      { timeout: 8000, label: '发布被拒的提示' })
    check('发布被拒时提示条显示后端原文（不是"发布失败"）', true)

    // ---- 草稿试玩：进游戏屏 + 「未发布」水印 ----
    const playtested = await cdp.eval(`(() => {
      const b = [...document.querySelectorAll('#app .studio .col-right button')]
        .find(x => x.textContent.trim() === '试玩这一版');
      if (!b || b.disabled) return false;
      b.click();
      return true;
    })()`)
    check('草稿有「试玩这一版」入口', playtested)
    await cdp.waitFor(`!!document.querySelector('#app .col-mid')`, { timeout: 8000, label: '进入草稿试玩' })
    check(
      '草稿试玩进入同一套三栏游戏屏且在顶栏打「未发布」水印',
      await cdp.eval(`/未发布/.test(document.querySelector('#app .topbar')?.textContent || '')`),
    )

    if (keep) {
      const p = join(ROOT, 'webui-smoke-dom.html')
      writeFileSync(p, dump)
      console.log(`\nDOM 已保存: ${p}`)
    }
  } catch (e) {
    check('冒烟执行', false, e.message)
  } finally {
    try {
      await cdp?.send('Browser.close')
    } catch {
      /* 忽略 */
    }
    proc.kill()
    server.close()
    // 浏览器刚被杀时 Crashpad 还占着文件，删不掉是正常的——不要让它盖掉真正的结论
    try {
      rmSync(userDataDir, { recursive: true, force: true })
    } catch {
      /* 临时目录残留无害 */
    }
  }

  const failed = results.filter((r) => !r.ok)
  console.log(`\n===== 前端真机冒烟：${results.length - failed.length}/${results.length} 通过 =====`)
  if (failed.length) {
    console.log('失败项：')
    for (const f of failed) console.log(`  - ${f.name}${f.extra ? `: ${f.extra}` : ''}`)
    process.exit(1)
  }
}

main().catch((e) => {
  console.error(`冒烟无法执行：${e.message}`)
  process.exit(1)
})
