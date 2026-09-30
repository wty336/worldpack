/** 正文流：追加式分段日志 + 流式草稿 + 生成计时 + 恢复痕迹。
 *
 *  三个保留自 Stage A 的玩家反馈点：
 *  - **默认只看本轮**（跨天不再清空剧情，但一屏只放当前段）；
 *  - **生成计时**（"不知道是卡了还是模型在思考"）；
 *  - **恢复痕迹落在故事分段里**（不能写进生成指示器——它在 endGen 时被清空）。
 */
<script setup>
import { computed, nextTick, ref, watch } from 'vue'

const props = defineProps({
  /** 已提交的分段（终稿正文），每项 { text, recovery } */
  entries: { type: Array, required: true },
  /** 正在流出的草稿（delta 累积；done 到达后由 entries 接管） */
  draft: { type: String, default: '' },
  streaming: { type: Boolean, default: false },
  /** 本轮用过的恢复手段（引擎口径的 key 列表） */
  recovered: { type: Array, default: () => [] },
  subTurns: { type: Number, default: 0 },
})

const fullHistory = ref(false)
const box = ref(null)

const RECOVERY_LABELS = {
  overflow_recovered: '本轮上下文超限，已压缩历史后重试成功',
  critique: '本轮初稿未通过自检，已重写',
  meltdown: '本轮多次未达协议，已跳过（效果未生效）',
}
/** 恢复痕迹的中性文案：只描述机制，不叙述剧情（引擎层不含内容文案）。 */
function recoveryNote(list, subTurns) {
  const parts = (list || []).map((r) => RECOVERY_LABELS[r] || r)
  if (!parts.length) return ''
  const sub = subTurns > 1 ? `，共生成 ${subTurns} 次` : ''
  return `（${parts.join('；')}${sub}）`
}
const liveNote = computed(() => recoveryNote(props.recovered, props.subTurns))

function scrollToBottom() {
  if (box.value && fullHistory.value) box.value.scrollTop = box.value.scrollHeight
}
// 流式期间跟随；非流式时"只看本轮"保持顶部（从头读本轮）
watch(() => props.draft, () => nextTick(scrollToBottom))
watch(() => props.entries.length, () => nextTick(scrollToBottom))
defineExpose({ fullHistory })
</script>

<template>
  <div class="col-body" ref="box">
    <div class="prose" :class="{ 'only-current': !fullHistory }">
      <div v-for="(e, i) in entries" :key="i" class="entry">
        <span>{{ e.text }}</span>
        <span v-if="e.recovery" class="recovery">&#10;&#10;{{ e.recovery }}</span>
      </div>
      <!-- 流式草稿：独立一段，done 到达后由 entries 接管（草稿可能属于被作废的一稿） -->
      <div v-if="streaming && draft" class="entry">
        <span>{{ draft }}</span><span class="caret"></span>
      </div>
      <!-- 无叙事但有恢复痕迹（如熔断兜底轮）也要说清 -->
      <div v-else-if="streaming && liveNote" class="entry recovery">{{ liveNote }}</div>
    </div>

    <!-- 回顾切换单独一类，**不叫 input-dock**：真机冒烟里
         `#app .input-dock button` 第一个命中的就是它，而不是「发言」——
         复用类名会让"选中的元素"与"想选的元素"悄悄错位（这是选择器层面的事故，
         不是布局问题）。 -->
    <div class="history-bar">
      <button :class="{ chosen: fullHistory }" @click="fullHistory = !fullHistory">
        {{ fullHistory ? '只看本轮' : '剧情回顾' }}
      </button>
    </div>
  </div>
</template>
