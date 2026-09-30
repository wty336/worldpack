<script setup>
/** 创作工作台（N2a）：素材 → 进度 → 校验报告 → 试玩 → 发布。
 *
 *  **这一屏的范围是刻意的**（`docs/roadmap.md` §1.1）：它只做"共享外壳"——
 *  生成、看进度、看报告、试玩、发布。**不做手写字段编辑器**：编辑权归创作者
 *  Agent（N6），右栏只**呈现**。
 *
 *  为什么不做表单（三条，见 `plan-tavern-shaped-product.md` §4.2）：
 *  同一个方案里既论证"结构化 CRUD 交给 Agent 比表单好"、又排一个表单工作台是自相矛盾；
 *  需求原话是"与 Agent 对话…修改人物设定和世界书"；而且表单会和草稿抢同一份真值——
 *  Agent 改完草稿，表单里显示的是它上次读到的值。
 *
 *  **为什么"离线试跑"默认勾上**：真实生成一次约 ¥0.1–0.3（`with_corpus` 还要多 12 次调用）。
 *  作者第一次点进来多半只是想看这条链通不通，默认让他花钱是错的默认值。
 *  取消勾选时会有一句醒目的成本提示，不会让人不知不觉花掉钱。
 */

import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { api } from '../api/client'
import { useJobStream } from '../composables/useJobStream'

const emit = defineEmits(['notice', 'playtest', 'back', 'published'])
const props = defineProps({
  busy: { type: Boolean, default: false },
})

const drafts = ref([])
const jobs = ref([])
const pickedDraft = ref(null)
const pickedJobId = ref('')
const jobSnapshot = ref(null)
const elapsed = ref(0)

const form = ref({
  name: '',
  source_text: '',
  rounds: 4,
  with_corpus: false,
  offline: true, // 见文件头：默认不花钱
})

const { events, running, disconnected, error: streamError, watch } = useJobStream()

let timer = null

const pickedJob = computed(() => jobs.value.find((j) => j.job_id === pickedJobId.value) || null)
const repairLines = computed(() =>
  events.value.filter((e) => e.stage === 'repair').map((e) => e.message),
)
const report = computed(() => (jobSnapshot.value && jobSnapshot.value.result) || null)
const canSubmit = computed(() => form.value.name.trim() && form.value.source_text.trim())

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
    const fin = await watch(d.job_id)
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

/** 刷新页面后接上正在跑（或刚跑完）的任务——N1 的事件留档就是为这件事。 */
async function resume(jobId) {
  pickedJobId.value = jobId
  jobSnapshot.value = null
  elapsed.value = 0
  startTimer()
  const fin = await watch(jobId)
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

function pickDraft(d) {
  pickedDraft.value = d
  // 顺带把它对应的那次生成报告翻出来（同名任务的最近一次）
  const j = jobs.value.find((x) => x.pack_name === d.id)
  if (j) {
    pickedJobId.value = j.job_id
    jobSnapshot.value = j.status === 'done' ? j : null
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
          @click="pickDraft(d)"
        >
          <span class="pname">{{ d.name || d.id }}</span>
          <span class="pera">{{ d.playable ? '✓ 通过校验，可发布' : '✗ 未通过校验' }}</span>
          <span class="pmeta">{{ d.id }} · {{ d.npcs }} 角色 · {{ d.nodes }} 节点</span>
        </button>
      </div>
    </aside>

    <!-- 中栏：素材输入 + 进度时间线（动作与过程） -->
    <section class="col col-mid">
      <div class="col-body">
        <h3 class="sec">从素材生成一张卡</h3>
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
          <p class="t">内容指纹 {{ (pickedDraft.digest || '').slice(0, 16) }}…</p>
          <p class="t">{{ pickedDraft.path }}</p>

          <div class="save-row">
            <button :disabled="!pickedDraft.playable" @click="playtest">试玩这一版</button>
            <button :disabled="!pickedDraft.playable" @click="publish">发布</button>
            <button @click="remove">删除草稿</button>
          </div>
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
      </div>
    </aside>
  </div>
</template>
