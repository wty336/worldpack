/** 候选项 / 关键抉择（两种状态）+ 结局卡 + 生成指示器。
 *
 *  关键契约（来自引擎，不是前端约定）：`choice_prompt` 非空 = 关键抉择，
 *  此时按钮走 `pick(index)`；为空 = 日常选项，走 `say(text)`。
 *  错接的症状是"点日常选项被引擎拒绝"（后端有守卫钉住这条路由）。
 *
 *  「自由输入」那一项**只聚焦输入框**，不把提示文案当发言发出去。
 */
<script setup>
import { computed } from 'vue'

const props = defineProps({
  /** _view() 的载荷 */
  view: { type: Object, default: () => ({}) },
  /** 引擎的"自由输入"入口文案（由 /api/config 注入，单一真源） */
  freeInput: { type: String, default: '' },
  streaming: { type: Boolean, default: false },
  elapsed: { type: Number, default: 0 },
})
const emit = defineEmits(['pick', 'say', 'focus-input'])

const critical = computed(() => !!props.view.choice_prompt)
const choices = computed(() => props.view.choices || [])
const ending = computed(() => props.view.ending || null)

function onChoice(text, index) {
  if (critical.value) emit('pick', index)
  else if (text === props.freeInput) emit('focus-input')
  else emit('say', text)
}
</script>

<template>
  <div>
    <div v-if="streaming" class="gen">
      模型思考中（已等 {{ elapsed }} 秒，回答将逐字流式输出）
    </div>

    <div v-if="critical" class="prompt-card">
      <span class="label">【关键抉择】</span>{{ view.choice_prompt.prompt }}
    </div>
    <div v-else-if="view.briefing" class="prompt-card">
      <span class="label">【本轮背景】</span>{{ view.briefing }}
    </div>

    <div v-if="ending" class="prompt-card ending">
      <span class="label">『{{ ending.title }}』</span>{{ ending.text }}
    </div>

    <div v-if="!ending && choices.length" class="choices">
      <button v-for="(c, i) in choices" :key="i" :disabled="streaming" @click="onChoice(c, i)">
        {{ c }}
      </button>
    </div>
  </div>
</template>
