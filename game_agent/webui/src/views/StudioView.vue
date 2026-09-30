<script setup>
/** 创作工作台（N2a 共享外壳 + N6 创作者 Agent）：素材/对话 → 进度 → 校验报告 → 试玩 → 发布。
 *
 *  **中栏是两个模式，对应写作的两种起点**：
 *  ① 「从素材生成」——手上有一篇小说/大纲，让它长出结构（N2a）；
 *  ② 「和 Agent 改这一版」——已经有一张卡，用对话改人物设定与世界书（N6）。
 *
 *  **右栏始终是"呈现"**：字段、计数、校验结论、diff。**没有任何输入框**——
 *  这一条是刻意的（`docs/roadmap.md` §1.1 / `plan-tavern-shaped-product.md` §4.2）：
 *  编辑权归 Agent，人负责"说清要改什么"和"看了 diff 决定发不发"。
 *  做了手写表单就会和草稿抢同一份真值（Agent 改完草稿，表单里还是它上次读到的值）。
 *
 *  **为什么"离线试跑"默认勾上**：真实生成一次约 ¥0.1–0.3（`with_corpus` 还要多 12 次
 *  调用）。作者第一次点进来多半只是想看这条链通不通，默认让他花钱是错的默认值。
 *  取消勾选时会有一句醒目的成本提示，不会让人不知不觉花掉钱。
 *  （创作者 Agent 没有离线模式——它改的是真草稿，对话会真的调用模型。）
 */

import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { api } from '../api/client'
import { useCreatorStream } from '../composables/useCreatorStream'
import { useJobStream } from '../composables/useJobStream'

const emit = defineEmits(['notice', 'playtest', 'back', 'published'])
const props = defineProps({
  busy: { type: Boolean, default: false },
  packs: { type: Array, default: () => [] },
})

const drafts = ref([])
const jobs = ref([])
const pickedDraft = ref(null)
const pickedJobId = ref('')
const jobSnapshot = ref(null)
const elapsed = ref(0)
const tab = ref('generate') // generate | agent
const forkFrom = ref('')
const chatInput = ref('')
const chatBox = ref(null)
const packCost = ref(null) // N5/C3：这张卡的累计成本
const injection = ref(null) // C4：注入提醒（null = 没有命中）
const ackedInjection = ref(false) // 作者已知悉（**只影响按钮文案，不阻止发布**）
const gate = ref(null) // C5：当前/最近一道门禁的结果
const gateConfirm = ref('') // 待确认的门禁类型（两步确认的第一步）

const form = ref({
  name: '',
  source_text: '',
  rounds: 4,
  with_corpus: false,
  offline: true, // 见文件头：默认不花钱
})

const { events, running, disconnected, error: streamError, watch: watchJob } = useJobStream()
const creator = useCreatorStream()

let timer = null

const pickedJob = computed(() => jobs.value.find((j) => j.job_id === pickedJobId.value) || null)
const repairLines = computed(() =>
  events.value.filter((e) => e.stage === 'repair').map((e) => e.message),
)
const report = computed(() => (jobSnapshot.value && jobSnapshot.value.result) || null)
const canSubmit = computed(() => form.value.name.trim() && form.value.source_text.trim())
const canChat = computed(
  () => !!pickedDraft.value && chatInput.value.trim() && !creator.running.value,
)
/** diff 只显示"看得见变化"的部分；空 diff 要明确说"还没改"，不能给一个空框让人猜。 */
const diffText = computed(() => {
  const r = creator.result.value
  return (r && r.diff) || ''
})

function say(message, isError = false) {
  emit('notice', { message, isError })
}

async function refresh() {
  try {
    const [d, j] = await Promise.all([api.drafts(), api.jobs()])
    drafts.value = d.drafts || []
    jobs.value = j.jobs || []
    // 选中的草稿可能刚被发布/删除掉，选中态要跟着失效（否则右栏显示的是幽灵）
    if (pickedDraft.value && !drafts.value.some((x) => x.id === pickedDraft.value.id)) {
      pickedDraft.value = null
    } else if (pickedDraft.value) {
      pickedDraft.value = drafts.value.find((x) => x.id === pickedDraft.value.id)
    }
  } catch (e) {
    say(e.message, true)
  }
}

/** 起一个生成任务，然后立刻订阅它的进度流。 */
async function submit() {
  if (!canSubmit.value) return
  try {
    const d = await api.generate({
      name: form.value.name.trim(),
      source_text: form.value.source_text,
      rounds: Number(form.value.rounds) || 4,
      with_corpus: form.value.with_corpus,
      offline: form.value.offline,
    })
    pickedJobId.value = d.job_id
    jobSnapshot.value = null
    elapsed.value = 0
    startTimer()
    say(`已起任务 ${d.job_id}（${form.value.offline ? '离线试跑' : '真机生成'}）`)
    await refresh()
    const fin = await watchJob(d.job_id)
    stopTimer()
    jobSnapshot.value = fin
    await refresh()
    if (fin && fin.status === 'done') {
      pickedDraft.value = drafts.value.find((x) => x.id === fin.pack_name) || null
      say(
        fin.result && fin.result.repairs
          ? `生成完成，修了 ${fin.result.repairs} 轮——左栏草稿已可试玩或发布。`
          : '生成完成——左栏草稿已可试玩或发布。',
      )
    } else if (fin && fin.status === 'cancelled') {
      say('任务已取消（草稿可能停在半成品状态，试玩前先看校验报告）。')
    } else {
      say(`任务结束：${(fin && fin.status) || '未知'} ${(fin && fin.error) || ''}`, true)
    }
  } catch (e) {
    stopTimer()
    say(e.message, true)
  }
}

/** 把已发布的卡复制成草稿——"改一张现成的卡"的第一步。 */
async function fork() {
  const name = forkFrom.value
  if (!name) return
  try {
    await api.fork(name)
    say(`已把《${name}》复制成草稿——现在可以用 Agent 改它了（原版没有被改动）。`)
    forkFrom.value = ''
    await refresh()
    pickedDraft.value = drafts.value.find((x) => x.id === name) || null
    tab.value = 'agent'
  } catch (e) {
    say(e.message, true)
  }
}

/** 发一句话给创作者 Agent。 */
async function chat() {
  if (!canChat.value) return
  const name = pickedDraft.value.id
  const text = chatInput.value.trim()
  chatInput.value = ''
  await scrollChat()
  const done = await creator.send(name, text)
  await refresh()
  await scrollChat()
  // Agent 从素材起了一张新卡（start_generation）→ **自动切到进度页签并订阅那条流**。
  // 这一步是"对话式做卡"体验的关键：否则作者只知道"任务起了"，却不知道去哪看。
  const job = creator.startedJob.value
  if (job && job.job_id) {
    tab.value = 'generate'
    await resume(job.job_id)
    say(`Agent 已从素材起了一张新卡（${job.pack_name}）——进度在上面，跑完会出现在左栏草稿里。`)
    return
  }
  if (done) {
    if (!done.validate_ok) {
      say('Agent 改完之后 check-worldpack 没过——右栏有报错原文，可以接着让它修。', true)
    } else if (done.changed && done.changed.length) {
      say(`本版已改动：${done.changed.join('、')}（右栏看 diff，确认后发布）`)
    }
  } else if (creator.error.value) {
    say(creator.error.value, true)
  }
}

async function resetChat() {
  if (!pickedDraft.value) return
  try {
    await api.creatorReset(pickedDraft.value.id)
    creator.hydrate(null)
    say('已清空对话上下文（草稿内容未动）。')
  } catch (e) {
    say(e.message, true)
  }
}

/** 切到某张草稿：拉它的创作会话现状（对话 + diff）+ **这张卡的累计成本**。 */
async function openDraft(d) {
  pickedDraft.value = d
  tab.value = 'agent'
  const j = jobs.value.find((x) => x.pack_name === d.id)
  if (j) {
    pickedJobId.value = j.job_id
    jobSnapshot.value = j.status === 'done' ? j : null
  }
  try {
    creator.hydrate(await api.creatorState(d.id))
  } catch (e) {
    creator.hydrate(null)
    say(e.message, true)
  }
  await loadPackCost(d.id)
  await loadInjection(d.id)
  await scrollChat()
}

/** N5/C3：这张卡**累计**花了多少钱（游玩 + 改卡 + 生成都在内，同一个 pack 轴）。 */
async function loadPackCost(name) {
  packCost.value = null
  try {
    const d = await api.packCost(name)
    packCost.value = d.totals || null
  } catch {
    packCost.value = null  // 成本读不出来不该挡住别的功能
  }
}

/** C4：注入**提醒**（不是门禁）。读不到就当没有——它是提醒，不能挡住任何操作。 */
async function loadInjection(name) {
  injection.value = null
  ackedInjection.value = false
  try {
    const d = await api.packInjection(name)
    const findings = [...(d.injection || []), ...(d.forbidden_overlap || [])]
    injection.value = findings.length ? findings : null
  } catch {
    injection.value = null
  }
}

/** 有提醒时，发布要点两次：第一次是"我看了"，第二次才真发。**这不阻止发布。** */
function publishClick() {
  if (injection.value && !ackedInjection.value) {
    ackedInjection.value = true
    say('已知悉注入提醒——再点一次「确认发布」就会发布（提醒不会阻止发布）。')
    return
  }
  publish()
}

// ---- C5：质量门禁按钮（会花钱，所以先确认再跑）----

/** 起一道门。**第一次点击只用来拿成本说明并要求确认**（服务端也会拦一次）。 */
async function gateClick(kind) {
  if (!pickedDraft.value) return
  if (gateConfirm.value !== kind) {
    gateConfirm.value = kind
    say(`「${gateKindLabel(kind)}」会真机调模型（${gateCost(kind)}）——再点一次就开始。`)
    return
  }
  gateConfirm.value = ''
  try {
    const d = await api.runGate(pickedDraft.value.id, kind, true)
    gate.value = { gate_id: d.gate_id, kind, status: 'running', output: '' }
    say(`已起「${d.label}」（${d.cost}）——跑完会在这里显示结果。`)
    await pollGate(d.gate_id)
  } catch (e) {
    say(e.message, true)
  }
}

async function pollGate(id) {
  for (let i = 0; i < 200; i += 1) {   // 最多等 ~10 分钟
    await new Promise((r) => setTimeout(r, 3000))
    try {
      const g = await api.gate(id)
      gate.value = g
      if (g.status !== 'running') {
        say(g.status === 'done'
          ? `门禁通过：${gateKindLabel(g.kind)}`
          : `门禁未通过（退出码 ${g.exit_code}）——结果在右栏。`, g.status !== 'done')
        return
      }
    } catch (e) {
      say(e.message, true)
      return
    }
  }
  say('门禁还在跑（超过 10 分钟）——刷新页面后可以在右栏看到结果。', true)
}

function gateKindLabel(kind) {
  return kind === 'e1' ? 'E1 判官灵敏度' : '真机冒烟（通关 + 审计）'
}

function gateCost(kind) {
  return kind === 'e1' ? '约 ¥0.1–0.3' : '约 ¥0.05–0.2'
}

function money(v) {
  return v == null ? '—' : `¥${Number(v).toFixed(3)}`
}

function purposeLabel(p) {
  return {
    turn: '游玩回合', judge: '语义校验', factcheck: '事实核查', extract: '事实提取',
    compress: '历史压缩', reflect: '关系洞察', dedup: '语义去重', plan: '计划',
    import_world: '生成·世界', import_npcs: '生成·角色', import_schedule: '生成·日程',
    import_nodes: '生成·主线', import_events: '生成·事件', import_endings: '生成·结局',
    creator: '改卡对话', aux: '辅助',
  }[p] || p
}

async function scrollChat() {
  await nextTick()
  const el = chatBox.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(chatBox, scrollChat)

/** 刷新页面后接上正在跑（或刚跑完）的任务——N1 的事件留档就是为这件事。 */
async function resume(jobId) {
  pickedJobId.value = jobId
  jobSnapshot.value = null
  elapsed.value = 0
  startTimer()
  const fin = await watchJob(jobId)
  stopTimer()
  jobSnapshot.value = fin
  await refresh()
}

async function cancel() {
  if (!pickedJobId.value) return
  try {
    await api.cancelJob(pickedJobId.value)
    say('已请求取消——当前这次模型调用会跑完，之后在块之间停下。')
    await refresh()
  } catch (e) {
    say(e.message, true)
  }
}

function pickJob(j) {
  pickedJobId.value = j.job_id
  jobSnapshot.value = j.status === 'done' ? j : null
}

/** 发布：闸门在后端（check_worldpack）。被拒时把**报错原文**显示出来。 */
async function publish() {
  if (!pickedDraft.value) return
  try {
    const r = await api.publish(pickedDraft.value.id)
    say(`已发布《${r.pack.name}》——现在它会出现在选卡屏里。`)
    pickedDraft.value = null
    await refresh()
    emit('published')
  } catch (e) {
    // 后端把 check_worldpack 的原文放在 detail 里，这里原样呈现（它就是拿去喂模型修的燃料）
    say(e.message, true)
  }
}

async function remove() {
  if (!pickedDraft.value) return
  const id = pickedDraft.value.id
  try {
    await api.deleteDraft(id)
    say(`已删除草稿 ${id}。`)
    pickedDraft.value = null
    await refresh()
  } catch (e) {
    say(e.message, true)
  }
}

function playtest() {
  if (!pickedDraft.value) return
  emit('playtest', { draft: pickedDraft.value.id })
}

function startTimer() {
  stopTimer()
  const t0 = Date.now()
  timer = setInterval(() => {
    elapsed.value = Math.round((Date.now() - t0) / 1000)
  }, 1000)
}
function stopTimer() {
  if (timer) clearInterval(timer)
  timer = null
}

function stageMark(stage) {
  if (stage === 'done') return '✓'
  if (stage === 'error' || stage === 'warn') return '!'
  if (stage === 'repair') return '↻'
  return '·'
}

onMounted(async () => {
  await refresh()
  // 有正在跑的任务就自动接上（刷新页面后最需要的行为）
  const live = jobs.value.find((j) => j.status === 'running' || j.status === 'queued')
  if (live) await resume(live.job_id)
})

onBeforeUnmount(stopTimer)
</script>

<template>
  <div class="columns studio">
    <!-- 左栏：任务与草稿（对象列表） -->
    <aside class="col col-left">
      <div class="col-head">
        <button class="t" @click="emit('back')">← 回到选卡</button>
      </div>
      <div class="col-body">
        <h3 class="sec">任务</h3>
        <p v-if="!jobs.length" class="t">还没有生成任务。</p>
        <button
          v-for="j in jobs"
          :key="j.job_id"
          class="pack"
          :class="{ current: j.job_id === pickedJobId }"
          @click="pickJob(j)"
        >
          <span class="pname">{{ j.pack_name }}</span>
          <span class="pera">{{ j.status }}<template v-if="j.status === 'running'"> · 进行中</template></span>
          <span class="pmeta">{{ j.job_id }}</span>
        </button>

        <h3 class="sec">草稿（未发布）</h3>
        <p v-if="!drafts.length" class="t">
          还没有草稿。生成的包先落这里，过 check-worldpack 才允许发布。
        </p>
        <button
          v-for="d in drafts"
          :key="d.id"
          class="pack"
          :class="{ broken: !d.playable, current: pickedDraft && pickedDraft.id === d.id }"
          @click="openDraft(d)"
        >
          <span class="pname">{{ d.name || d.id }}</span>
          <span class="pera">{{ d.playable ? '✓ 通过校验，可发布' : '✗ 未通过校验' }}</span>
          <span class="pmeta">{{ d.id }} · {{ d.npcs }} 角色 · {{ d.nodes }} 节点</span>
        </button>

        <h3 class="sec">改一张现成的卡</h3>
        <p class="t">
          把库里已发布的卡**复制**成草稿再改——原版原地不动（Agent 只改草稿）。
        </p>
        <div class="field row">
          <select v-model="forkFrom" :disabled="props.busy">
            <option value="">（选一张已发布的卡）</option>
            <option v-for="p in packs" :key="p.id" :value="p.id" :disabled="!p.playable">
              {{ p.name || p.id }}
            </option>
          </select>
          <button :disabled="!forkFrom || props.busy" @click="fork">复制成草稿</button>
        </div>
      </div>
    </aside>

    <!-- 中栏：两个模式（从素材生成 / 和 Agent 改这一版） -->
    <section class="col col-mid">
      <div class="col-body">
        <div class="tabs">
          <button :class="{ chosen: tab === 'generate' }" @click="tab = 'generate'">
            从素材生成一张卡
          </button>
          <button :class="{ chosen: tab === 'agent' }" @click="tab = 'agent'">
            和 Agent 改这一版
          </button>
        </div>

        <!-- 模式 ①：从素材生成（N2a） -->
        <template v-if="tab === 'generate'">
        <p class="t">
          小说 / 剧本 / 大纲的正文都行（超长会自动截断到 30 万字）。
          <strong>接口只收文本、不收文件路径</strong>——这是安全边界，不是偷懒。
        </p>

        <div class="field">
          <label>包名（只能是目录名：中文/字母/数字/下划线/连字符）</label>
          <input v-model="form.name" type="text" placeholder="例如 江湖旧梦_修订">
        </div>

        <div class="field">
          <label>素材正文</label>
          <textarea v-model="form.source_text" rows="8" placeholder="把小说/大纲/设定粘进来…" />
        </div>

        <div class="field row">
          <label>修复轮次 <input v-model="form.rounds" type="text" class="tiny"></label>
          <label><input v-model="form.with_corpus" type="checkbox"> 同时生成 Judge 语料（多 12 次调用，更贵）</label>
        </div>

        <div class="field row">
          <label><input v-model="form.offline" type="checkbox"> 离线试跑（内嵌假模型，不花钱、不联网）</label>
        </div>

        <p v-if="form.offline" class="t">
          离线试跑产出的是一个**假世界**（"离线测试世界"），只用来看这条链通不通。
          要真的从素材做出能玩的卡，取消上面的勾。
        </p>
        <p v-else class="cost">
          ⚠️ 真实生成会调用模型：一次约 <strong>¥0.1–0.3</strong>；勾了语料还要再贵一些。
          生成是后台任务，可以边跑边看，也可以取消（取消点在块与块之间）。
        </p>

        <div class="save-row">
          <button :disabled="!canSubmit || running || props.busy" @click="submit">开始生成</button>
          <button v-if="running" @click="cancel">取消任务</button>
          <span v-if="running" class="gen">已等 {{ elapsed }} 秒…</span>
        </div>

        <h3 class="sec">进度</h3>
        <p v-if="!pickedJobId" class="t">起一个任务，或在左栏点一个任务/草稿看它的进度。</p>
        <template v-else>
          <p class="t">
            任务 {{ pickedJobId }} ·
            <template v-if="running">进行中（已等 {{ elapsed }} 秒）</template>
            <template v-else-if="jobSnapshot">{{ jobSnapshot.status }}</template>
            <template v-else>未取到终态快照</template>
          </p>
          <p v-if="disconnected" class="cost">⚠️ {{ streamError }}</p>
          <div class="log">
            <div v-for="(e, i) in events" :key="i" class="log-line">
              <span class="mark">{{ stageMark(e.stage) }}</span>
              <span class="stage">{{ e.stage }}</span>
              <span class="msg">{{ e.message }}</span>
            </div>
            <p v-if="!events.length" class="t">（还没有事件）</p>
          </div>
        </template>
        </template>

        <!-- 模式 ②：和创作者 Agent 对话改这一版（N6） -->
        <template v-else>
          <p v-if="!pickedDraft" class="t">
            先在左栏**选一张草稿**，或把一张已发布的卡复制成草稿。
            创作 Agent 只改草稿——原始包在结构上只读。
          </p>
          <template v-else>
            <p class="t">
              正在改：<strong>{{ pickedDraft.name || pickedDraft.id }}</strong>
              · Agent 会先读再改，每改一处都会跑 check-worldpack，报错会被它自己拿去修。
              改完的 diff 在右栏，确认无误再发布。
            </p>
            <p v-if="creator.summary.value" class="t">
              Agent 看到的现状：{{ creator.summary.value }}
            </p>
            <div class="field row">
              <button :disabled="creator.running.value" @click="resetChat">
                清空对话上下文（不动内容）
              </button>
            </div>

            <div ref="chatBox" class="chat">
              <p v-if="!creator.messages.value.length" class="t">
                （还没有对话）说你想改什么，比如「把沈清秋的性格改得更外冷内热，说话更短」
                「加一条关于城南诊所的世界书设定」。
              </p>
              <div
                v-for="(m, i) in creator.messages.value"
                :key="i"
                class="chat-msg"
                :class="m.role"
              >
                <span class="who">{{ m.role === 'user' ? '你' : 'Agent' }}</span>
                <span class="what">{{ m.content }}</span>
              </div>
              <!-- 本轮过程：工具调用与中间话（只显示当前这一轮，不堆历史） -->
              <div v-for="(s, i) in creator.steps.value" :key="'s' + i" class="chat-step">
                <template v-if="s.type === 'tool'">
                  <span class="mark">{{ s.status === 'ok' ? '⚙' : '⚠' }}</span>
                  {{ s.name }}
                  <span class="t">{{ s.status === 'ok' ? '' : `（${s.status}）` }}</span>
                </template>
                <template v-else-if="s.type === 'text'">
                  <span class="mark">💬</span>{{ s.text }}
                </template>
                <template v-else>
                  <span class="mark">✗</span>{{ s.text }}
                </template>
              </div>
              <p v-if="creator.running.value" class="gen">Agent 正在处理…</p>
            </div>

            <div class="save-row">
              <input
                v-model="chatInput"
                type="text"
                placeholder="说你想改什么…"
                :disabled="creator.running.value"
                @keyup.enter="chat"
              >
              <button :disabled="!canChat" @click="chat">发送</button>
            </div>
            <p v-if="creator.error.value" class="cost">⚠️ {{ creator.error.value }}</p>
          </template>
        </template>
      </div>
    </section>

    <!-- 右栏：只读呈现（真值面）——**没有输入框**，见文件头 -->
    <aside class="col col-right">
      <div class="col-head">校验报告（只读）</div>
      <div class="col-body">
        <p v-if="!pickedDraft && !report" class="t">
          选一张草稿看它的校验报告。这一栏是<strong>呈现</strong>，不是编辑器——
          改内容由创作者 Agent 负责（对话式），人负责确认与发布。
        </p>

        <template v-if="pickedDraft">
          <h3 class="sec">{{ pickedDraft.name || pickedDraft.id }}</h3>
          <p v-if="pickedDraft.playable" class="ok">✓ 通过 check-worldpack</p>
          <template v-else>
            <p class="err">✗ 未通过 check-worldpack（不能发布）</p>
            <pre class="errbox">{{ pickedDraft.error }}</pre>
          </template>

          <div class="kv">
            <div><span>角色</span><b>{{ pickedDraft.npcs }}</b></div>
            <div><span>主线节点</span><b>{{ pickedDraft.nodes }}</b></div>
            <div><span>结局</span><b>{{ pickedDraft.endings }}</b></div>
            <div><span>世界书条目</span><b>{{ pickedDraft.lore }}</b></div>
            <div><span>地点</span><b>{{ pickedDraft.locations }}</b></div>
          </div>

          <!-- N5/C3：按卡累计成本（游玩 + 改卡 + 生成都在内） -->
          <h3 class="sec">这张卡的成本</h3>
          <p v-if="!packCost" class="t">（还没有记账——生成、游玩或改卡之后会出现）</p>
          <template v-else>
            <p class="ok">
              累计 <strong>{{ money(packCost.cost_offpeak) }}</strong>
              · {{ packCost.calls }} 次调用
            </p>
            <div class="kv">
              <div
                v-for="(row, p) in packCost.by_purpose"
                :key="p"
              >
                <span>{{ purposeLabel(p) }}</span>
                <b>{{ row.calls }} 次 · {{ money(row.cost) }}</b>
              </div>
            </div>
            <p v-if="packCost.unknown_price_calls" class="cost">
              ⚠️ 有 {{ packCost.unknown_price_calls }} 次调用用的模型不在价格表里，
              上面这个数**偏低**。
            </p>
          </template>
          <p class="t">内容指纹 {{ (pickedDraft.digest || '').slice(0, 16) }}…</p>
          <p class="t">{{ pickedDraft.path }}</p>

          <!-- C4：注入**提醒**（不是门禁——只标黄、要求确认，不阻止发布） -->
          <template v-if="injection">
            <h3 class="sec">⚠️ 注入提醒</h3>
            <p class="cost">
              下面这些**作者内容会进系统提示**，而它们看起来像"对模型下的指令"。
              <strong>这只是提醒，不是门禁</strong>——命中了也能发布；
              正则必然有误报，请自己看原文片段判断。
            </p>
            <pre v-for="(h, i) in injection" :key="'inj' + i" class="errbox">{{
              `${h.where} · ${h.field}\n${h.kind || ('写了本包禁用元素：' + h.token)}\n…${h.snippet}…`
            }}</pre>
          </template>

          <div class="save-row">
            <button :disabled="!pickedDraft.playable" @click="playtest">试玩这一版</button>
            <button :disabled="!pickedDraft.playable" @click="publishClick">
              {{ injection && !ackedInjection ? '确认发布（先看上面的提醒）' : '发布' }}
            </button>
            <button @click="remove">删除草稿</button>
          </div>

          <!-- C5：质量门禁（手动、会花钱、成本前置确认） -->
          <h3 class="sec">质量门禁</h3>
          <p class="t">
            这两道门都要**真机调模型**（不同于 `check-worldpack`——那个在加载期跑、免费，
            已经作为发布闸门）。"想上精选"时再点。
          </p>
          <div class="save-row">
            <button
              :disabled="!pickedDraft.playable || !!gateConfirm && gateConfirm !== 'e1'"
              @click="gateClick('e1')"
            >
              {{ gateConfirm === 'e1' ? `确认跑 E1（${gateCost('e1')}）` : 'E1 判官灵敏度' }}
            </button>
            <button
              :disabled="!pickedDraft.playable || !!gateConfirm && gateConfirm !== 'smoke'"
              @click="gateClick('smoke')"
            >
              {{ gateConfirm === 'smoke' ? `确认跑冒烟（${gateCost('smoke')}）` : '真机冒烟（通关+审计）' }}
            </button>
          </div>
          <template v-if="gate">
            <p v-if="gate.status === 'running'" class="gen">门禁运行中…（真机跑模型，几分钟）</p>
            <p v-else-if="gate.status === 'done'" class="ok">
              ✓ {{ gateKindLabel(gate.kind) }} 通过
            </p>
            <p v-else class="err">
              ✗ {{ gateKindLabel(gate.kind) }} 未通过（退出码 {{ gate.exit_code }}）
            </p>
            <pre v-if="gate.output" class="diff">{{ gate.output }}</pre>
          </template>
          <p class="t">
            试玩用**未发布**的草稿开局（会打「未发布」水印）。发布 = 把它变成别人也能玩的卡。
          </p>
        </template>

        <template v-if="report">
          <h3 class="sec">这次生成</h3>
          <div class="kv">
            <div><span>修复轮次</span><b>{{ report.repairs }}</b></div>
            <div><span>语料条数</span><b>{{ report.corpus_written }}</b></div>
            <div><span>走过阶段</span><b>{{ (report.stages || []).length }}</b></div>
          </div>
          <p class="t">{{ report.summary }}</p>
          <p v-if="jobSnapshot && jobSnapshot.cost" class="t">
            成本：{{ jobSnapshot.cost }}
          </p>

          <template v-if="repairLines.length">
            <h3 class="sec">修复轮报错原文</h3>
            <p class="t">这些是 check-worldpack 当时拒绝的理由（工作台会拿它去喂模型修）。</p>
            <pre v-for="(r, i) in repairLines" :key="i" class="errbox">{{ r }}</pre>
          </template>
        </template>

        <!-- Agent 改完之后：校验结论 + 工作版 vs 基线 —— 人靠这个决定发不发 -->
        <template v-if="pickedDraft && creator.result.value">
          <h3 class="sec">Agent 这一版的校验</h3>
          <p v-if="creator.result.value.validate_ok" class="ok">✓ check-worldpack 通过</p>
          <template v-else>
            <p class="err">✗ check-worldpack 未通过（不能发布）</p>
            <pre class="errbox">{{ creator.result.value.validate_text }}</pre>
            <p class="t">继续和 Agent 说「按这个报错修」——它拿到的就是这段原文。</p>
          </template>

          <h3 class="sec">工作版 vs 会话基线</h3>
          <p v-if="!diffText" class="t">本次会话还没有改动。</p>
          <template v-else>
            <p class="t">改了：{{ (creator.result.value.changed || []).join('、') }}</p>
            <pre class="diff">{{ diffText }}</pre>
          </template>
        </template>
      </div>
    </aside>
  </div>
</template>
