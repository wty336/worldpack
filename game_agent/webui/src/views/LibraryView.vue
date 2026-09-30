/** 选卡屏（E-3/E-4）：选一张卡 + 选模式 + 读档。
 *
 *  坏包**也列出来**（后端 `/api/catalog` 会返回 `playable=false` + `error`）——
 *  让玩家看见"这张卡坏了"，而不是整块界面白屏或静默少一张卡。
 */
<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  packs: { type: Array, default: () => [] },
  saves: { type: Array, default: () => [] },
  defaultMode: { type: String, default: 'story' },
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['start', 'load', 'refresh'])

const picked = ref(null)
const mode = ref(props.defaultMode)

const playable = computed(() => props.packs.filter((p) => p.playable))
const usableSaves = computed(() => props.saves.filter((s) => !s.error))
const savePath = ref('')

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
  // 读档也要知道用哪张卡/哪个模式来承载状态；优先用玩家选中的卡
  emit('load', { path, packId: picked.value?.id || null, mode: mode.value })
}
</script>

<template>
  <div class="library">
    <h2>选一张卡</h2>
    <p class="t">
      共 {{ packs.length }} 张卡（可玩 {{ playable.length }}）。
      <button class="t" :disabled="busy" @click="emit('refresh')">重新读取</button>
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
        <span class="pmeta">{{ p.playable ? label(p) : p.id }}</span>
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
  </div>
</template>
