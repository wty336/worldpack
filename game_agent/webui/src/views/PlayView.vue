/** 游戏主界面：三栏（左：本局信息/存档 · 中：正文+候选+输入 · 右：状态栏）。
 *
 *  数据流全部是"引擎真值 → 视图"，前端不自己维护剧情状态：
 *  `done` 事件（`_view()`）是唯一权威，`status_text()` 与 `actions` 每次回合后重取。
 */
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import ChoiceList from '../components/ChoiceList.vue'
import InputDock from '../components/InputDock.vue'
import ProseStream from '../components/ProseStream.vue'
import StatusPanel from '../components/StatusPanel.vue'
import { api } from '../api/client'
import { useTurnStream } from '../composables/useTurnStream'

const props = defineProps({
  session: { type: Object, required: true }, // { sid, pack_id, name, mode }
  freeInput: { type: String, default: '' },
  saves: { type: Array, default: () => [] },
})
const emit = defineEmits(['notice', 'leave', 'saved', 'ended'])

const { streaming, draft, error, recovered, subTurns, send } = useTurnStream()

const entries = ref([])          // 已提交分段
const status = ref('')
const actions = ref({})
const elapsed = ref(0)
const view = ref({})
const dock = ref(null)
let timer = null

const modeLabel = computed(() => (props.session.mode === 'free' ? '自由探索' : '跟着主线'))

function recoveryText(list, sub) {
  const LABELS = {
    overflow_recovered: '本轮上下文超限，已压缩历史后重试成功',
    critique: '本轮初稿未通过自检，已重写',
    meltdown: '本轮多次未达协议，已跳过（效果未生效）',
  }
  const parts = (list || []).map((r) => LABELS[r] || r)
  if (!parts.length) return ''
  return `（${parts.join('；')}${sub > 1 ? `，共生成 ${sub} 次` : ''}）`
}

function applyView(v) {
  view.value = v
  const text = (v.briefing && v.choice_prompt ? '' : '') + (v.narration || '')
  const note = recoveryText(v.recovered, v.sub_turns)
  if (text || note) {
    entries.value = [...entries.value, { text, recovery: note }]
    if (entries.value.length > 60) entries.value = entries.value.slice(-60) // 防无界增长
  }
  if (v.ending) emit('ended', v.ending)
}

async function refresh() {
  const sid = props.session.sid
  try {
    const [st, ac] = await Promise.all([api.status(sid), api.actions(sid)])
    status.value = st.text || ''
    actions.value = ac
  } catch (e) {
    emit('notice', { message: e.message, isError: true })
  }
}

onMounted(async () => {
  // **开局的 view 必须被应用**：它带着开场叙事、候选项与（往往第一个）关键抉择。
  // 漏掉它的症状是"三栏都渲染出来了，但正文与候选都是空的、右栏一直显示等待开局"
  // ——结构测试全绿、只有真机冒烟能发现（scripts/webui_smoke.mjs 就是为此存在的）。
  if (props.session.openingView) applyView(props.session.openingView)
  // 状态栏与行动区不在任何流式事件里，必须开局时主动取一次
  await refresh()
})

async function turn(body) {
  if (timer) clearInterval(timer)
  const t0 = Date.now()
  elapsed.value = 0
  timer = setInterval(() => (elapsed.value = Math.round((Date.now() - t0) / 1000)), 1000)
  const done = await send(props.session.sid, body)
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  if (error.value) emit('notice', { message: `[错误] ${error.value}`, isError: true })
  else if (done) applyView(done)
  await refresh()
}

async function doSave() {
  const name = `${props.session.pack_id}${props.session.mode === 'free' ? '-free' : ''}.json`
  try {
    const r = await api.save(props.session.sid, name)
    emit('notice', { message: `已存档：${r.path}`, isError: false })
    emit('saved')
  } catch (e) {
    emit('notice', { message: e.message, isError: true })
  }
}

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
// 引擎可能内部作废重来：草稿只用于展示，done 到达后由 entries 接管（见 useTurnStream）
watch(draft, () => {})

defineExpose({ refresh })
</script>

<template>
  <div class="columns">
    <aside class="col col-left">
      <div class="col-head">本局</div>
      <div class="col-body">
        <p><strong>{{ session.name }}</strong></p>
        <p class="t">世界包：{{ session.pack_id }}</p>
        <p class="t">模式：{{ modeLabel }}</p>
        <p class="t">回合：{{ view.turn ?? 0 }} · 第 {{ actions.day ?? '?' }} 天</p>
        <div v-if="view.turn" class="t">
          <span v-if="view.sub_turns > 1">本轮生成了 {{ view.sub_turns }} 次</span>
        </div>
        <div class="status-actions">
          <button :disabled="streaming" @click="emit('leave')">换一张卡</button>
        </div>
      </div>
    </aside>

    <section class="col col-mid">
      <ProseStream
        :entries="entries"
        :draft="draft"
        :streaming="streaming"
        :recovered="recovered"
        :sub-turns="subTurns"
      />
      <ChoiceList
        :view="view"
        :free-input="freeInput"
        :streaming="streaming"
        :elapsed="elapsed"
        @pick="(i) => turn(api.turnBody.pick(i))"
        @say="(t) => turn(api.turnBody.say(t))"
        @focus-input="() => dock && dock.focus()"
      />
      <InputDock
        ref="dock"
        :streaming="streaming"
        @say="(t) => turn(api.turnBody.say(t))"
        @save="doSave"
        @leave="emit('leave')"
      />
    </section>

    <aside class="col col-right">
      <StatusPanel
        :status="status"
        :actions="actions"
        :streaming="streaming"
        @act="(id) => turn(api.turnBody.act(id))"
        @end-day="() => turn(api.turnBody.endDay())"
      />
    </aside>
  </div>
</template>
