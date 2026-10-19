/** 选卡屏（E-3/E-4）：选一张卡 + 选模式 + 读档，以及进入创作工作台（N2a）。
 *
 *  坏包**也列出来**（后端 `/api/catalog` 会返回 `playable=false` + `error`）——
 *  让玩家看见"这张卡坏了"，而不是整块界面白屏或静默少一张卡。
 *
 *  **草稿刻意不出现在这里**：它们是未发布的半成品。要试玩草稿，走创作工作台
 *  （那里有"试玩这一版"），而不是把它们混进"选一张卡"——玩家会点到没做完的卡。
 */
<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  packs: { type: Array, default: () => [] },
  saves: { type: Array, default: () => [] },
  // N13：盘上可以继续的局（服务重启之后仍然在——会话活内存、时间线活磁盘）
  timelines: { type: Array, default: () => [] },
  // N14：按卡统计（服务端算好给前端，避免"两处口径"）
  packCounts: { type: Object, default: () => ({}) },
  defaultMode: { type: String, default: 'story' },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['start', 'load', 'refresh', 'studio', 'resume'])

const picked = ref(null)
const mode = ref(props.defaultMode)
const showAllGames = ref(false)   // N14：默认只列最近 5 局

const playable = computed(() => props.packs.filter((p) => p.playable))
const usableSaves = computed(() => props.saves.filter((s) => !s.error))
/** N14：**按卡筛**——点了某张卡就只看这张卡的局（"按卡列全部局"）。 */
const gamesForPicked = computed(() => {
  if (!picked.value) return []
  const id = picked.value.id
  return props.timelines.filter(
    (t) => t.pack_id === id || (!t.pack_id && t.pack_name === (picked.value.name || picked.value.id)),
  )
})
const visibleGames = computed(() => {
  const list = picked.value ? gamesForPicked.value : props.timelines
  return showAllGames.value ? list : list.slice(0, 5)
})
const savePath = ref('')

/** 这张卡玩过的局数（用服务端口径；没有就不显示徽标）。 */
function gamesOf(p) {
  return props.packCounts[p.id] || 0
}

/** 续玩一行：世界名 + 跑到第几回合 + 什么时候 + 模式（+ 已结束就标出来）。 */
function recentLabel(t) {
  const when = t.mtime ? new Date(t.mtime * 1000).toLocaleString() : ''
  const modeText = t.mode === 'free' ? '自由探索' : '跟着主线'
  const who = t.pack_name || t.pack_id || '（未知的卡）'
  const end = t.ended ? ` · 已结束（${t.ending_title || '结局'}）` : ''
  return `《${who}》 · ${modeText} · 第 ${t.turn} 回合（第 ${t.day} 天）${end} · ${when}`
}

function label(p) {
  const bits = []
  if (p.npcs) bits.push(`${p.npcs} 名角色`)
  if (p.nodes) bits.push(`${p.nodes} 个主线节点`)
  bits.push(`${p.endings} 个结局`)
  return bits.join(' · ')
}
function saveLabel(s) {
  const when = s.mtime ? new Date(s.mtime * 1000).toLocaleString() : ''
  // 有身份戳就用卡 id；**旧档没有戳**，退到状态里自报的世界名——
  // 否则一列全是"（旧档无身份戳）"，玩家根本认不出哪个档属于哪张卡，
  // 只能靠"读了被拒"来试（这正是实测里那次 500 的由来）。
  const who = s.pack ? s.pack.id : (s.pack_name ? `旧档·${s.pack_name}` : '旧档·无标识')
  return `${s.path} — ${who} · 第 ${s.day ?? '?'} 天 · ${s.turn_count ?? '?'} 回合 · ${when}`
}
function start() {
  if (picked.value) emit('start', { packId: picked.value.id, mode: mode.value })
}
function load() {
  const path = savePath.value || (usableSaves.value[0] && usableSaves.value[0].path)
  if (!path) return
  // 读档也要知道用哪张卡/哪个模式来承载状态。
  // **N4：用存档自己那张卡，而不是玩家刚点选的那张**——存档按卡分目录之后，
  // "属于哪张卡"是档自己的属性（`dir` 比身份戳更可靠：戳可能缺失，目录不会撒谎）。
  // 拿 A 卡开局去读 B 卡的档，后端会正当拒绝；与其让玩家撞一次墙，不如按档来开。
  const pickedSave = usableSaves.value.find((s) => s.path === path)
  const packId =
    (pickedSave && (pickedSave.dir || (pickedSave.pack && pickedSave.pack.id))) ||
    (picked.value && picked.value.id) ||
    null
  emit('load', { path, packId, mode: mode.value })
}
</script>

<template>
  <div class="library">
    <h2>选一张卡</h2>
    <p class="t">
      共 {{ packs.length }} 张卡（可玩 {{ playable.length }}）。
      <button class="t" :disabled="busy" @click="emit('refresh')">重新读取</button>
      <button :disabled="busy" @click="emit('studio')">创作工作台：从素材做一张新卡 →</button>
    </p>

    <div class="pack-list grid">
      <button
        v-for="p in packs"
        :key="p.id"
        class="pack"
        :class="{ broken: !p.playable, current: picked && picked.id === p.id }"
        :disabled="!p.playable || busy"
        @click="picked = p"
      >
        <span class="pname">{{ p.name || p.id }}</span>
        <span class="pera">{{ p.playable ? (p.era || '') : `无法加载：${p.error}` }}</span>
        <span class="pmeta">
          {{ p.playable ? label(p) : p.id }}
          <template v-if="gamesOf(p)"> · 玩过 {{ gamesOf(p) }} 局</template>
        </span>
      </button>
    </div>

    <div class="mode-row">
      <span class="t">游玩模式：</span>
      <label><input v-model="mode" type="radio" value="story"> 跟着主线走（关键节点由引擎接管）</label>
      <label><input v-model="mode" type="radio" value="free"> 自由探索（不触发主线节点）</label>
    </div>

    <div class="save-row">
      <button :disabled="!picked || busy" @click="start">
        {{ picked ? `开始《${picked.name || picked.id}》` : '开始这一局' }}
      </button>
      <span class="t">或读取存档：</span>
      <select v-model="savePath" :disabled="!usableSaves.length || busy">
        <option v-if="!usableSaves.length" value="">（还没有存档）</option>
        <option v-for="s in usableSaves" :key="s.path" :value="s.path">{{ saveLabel(s) }}</option>
      </select>
      <button :disabled="!usableSaves.length || busy" @click="load">读取</button>
    </div>
    <p v-if="saves.length && !usableSaves.length" class="t">存档目录里没有可读的档。</p>

    <!-- N13：**接着上次玩**。会话活在内存里（关掉服务就没了），但每一版都记在磁盘上，
         所以"关掉终端、第二天回来"不该等于"那一局消失了"。
         N14：点了某张卡就只看**这张卡**的局；默认只列最近 5 局，可展开全部。 -->
    <div v-if="timelines.length" class="recent-games">
      <h3 class="sec">
        {{ picked ? `这张卡玩过的局（${gamesForPicked.length}）` : '接着上次玩' }}
      </h3>
      <p class="t">
        这些局记在本地磁盘上——即使服务重启过，也能从**最后一版**继续（不是回到存档点）。
        <template v-if="picked && !gamesForPicked.length">
          这张卡还没有玩过的局。
        </template>
      </p>
      <div v-for="t in visibleGames" :key="t.sid" class="recent-row">
        <button
          class="recent-item"
          :class="{ ended: t.ended }"
          :disabled="busy || !t.resumable"
          @click="emit('resume', { sid: t.sid })"
        >
          <span class="pname">{{ recentLabel(t) }}</span>
          <span v-if="!t.resumable" class="pera">不能继续：{{ t.reason }}</span>
          <span v-else-if="t.legacy" class="pera">{{ t.reason }}</span>
          <span v-else class="pera">
            rev {{ t.rev }} · 分支 {{ t.branch }}
            <template v-if="t.ended"> · 已打完</template>
          </span>
        </button>
      </div>
      <button
        v-if="(picked ? gamesForPicked.length : timelines.length) > 5"
        class="t more-games"
        @click="showAllGames = !showAllGames"
      >
        {{ showAllGames ? '只看最近 5 局' : `显示全部 ${picked ? gamesForPicked.length : timelines.length} 局` }}
      </button>
    </div>
  </div>
</template>
