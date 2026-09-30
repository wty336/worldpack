/** 应用骨架：拿配置 → 拿目录/存档 → 选卡开局（或读档）→ 进入三栏游戏界面。
 *
 *  为什么开局前要一次性并发三个请求（config/catalog/saves）：
 *  选卡屏需要的全部数据都在这里，分三次串行取会让首屏白等；
 *  而它们互不依赖，并发是免费的。
 */
<script setup>
import { computed, onMounted, ref } from 'vue'

import NoticeBar from './components/NoticeBar.vue'
import LibraryView from './views/LibraryView.vue'
import PlayView from './views/PlayView.vue'
import { api } from './api/client'

const config = ref({ free_input: '', default_mode: 'story' })
const packs = ref([])
const saves = ref([])
const session = ref(null) // 非空 = 在游戏中
const busy = ref(false)
const notice = ref({ message: '', isError: false })

const modeLabel = computed(() =>
  !session.value ? '' : session.value.mode === 'free' ? '（自由探索）' : '（跟着主线）',
)

function say(message, isError = false) {
  notice.value = { message, isError }
}

async function refreshLists() {
  try {
    const [cat, sv] = await Promise.all([api.catalog(), api.saves()])
    packs.value = cat.packs || []
    saves.value = sv.saves || []
  } catch (e) {
    say(e.message, true)
  }
}

async function boot() {
  try {
    const cfg = await api.config()
    config.value = { ...config.value, ...cfg }
  } catch (e) {
    say(`读取前端配置失败：${e.message}`, true)
  }
  await refreshLists()
}

async function start({ packId, mode }) {
  busy.value = true
  try {
    const d = await api.newGame(packId, mode)
    // openingView 必须一路带到 PlayView：开局那一屏（开场叙事 + 候选项 +
    // 常常还有第一个关键抉择）就在这个载荷里，丢掉它 = 玩家开局看到三栏空白。
    session.value = {
      sid: d.sid,
      pack_id: d.pack_id,
      name: d.name,
      mode: d.mode,
      openingView: d.view,
    }
    saves.value = (await api.saves()).saves || []
    say('')
  } catch (e) {
    say(`开局失败：${e.message}`, true)
  } finally {
    busy.value = false
  }
}

async function loadSave({ path, packId, mode }) {
  busy.value = true
  try {
    // 读档需要一个会话承载状态：先用同一张卡（或默认）开一局，再把档读进来。
    // 服务端会在剧本身份不符时**明确拒绝**并给出理由，这里原样呈现。
    if (!session.value) {
      const d = await api.newGame(packId, mode)
      session.value = {
        sid: d.sid,
        pack_id: d.pack_id,
        name: d.name,
        mode: d.mode,
        openingView: d.view,
      }
    }
    const r = await api.load(session.value.sid, path)
    say(`已读档：${r.path}`)
    await refreshLists()
  } catch (e) {
    say(e.message, true)
  } finally {
    busy.value = false
  }
}

function leave() {
  session.value = null
  say('')
  refreshLists()
}

onMounted(boot)
</script>

<template>
  <div class="app">
    <header class="topbar">
      <h1>{{ session ? session.name : '文字养成游戏' }}</h1>
      <span v-if="session" class="mode">{{ modeLabel }}</span>
      <span v-else class="t">选一张卡开始，或读取存档</span>
    </header>

    <NoticeBar :message="notice.message" :is-error="notice.isError" />

    <PlayView
      v-if="session"
      :session="session"
      :free-input="config.free_input"
      :saves="saves"
      @notice="({ message, isError }) => say(message, isError)"
      @leave="leave"
      @saved="refreshLists"
      @ended="() => say('这一局结束了——可以「换一张卡」再开一局。')"
    />
    <LibraryView
      v-else
      :packs="packs"
      :saves="saves"
      :default-mode="config.default_mode || 'story'"
      :busy="busy"
      @start="start"
      @load="loadSave"
      @refresh="refreshLists"
    />
  </div>
</template>
