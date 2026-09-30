/** 右栏：状态栏 + 日程行动。
 *
 *  **这一栏是这个项目区别于同类产品的卖点**：它渲染的是引擎**注入给模型的同一份**
 *  `status_text()`（场景 / 身份锚点 / 属性好感 / 计划块 / 约定 / 在场角色卡），
 * 也就是 README 说的"代码掌握真值的可视化"。
 *  参照实现做不到这一点——它的正文与状态来自两个不同的 Agent，玩家看到的是投影。
 *
 *  关键抉择期间行动区禁用并说明原因：此前"行动被拒绝但不解释"是玩家实测的困惑点。
 */
<script setup>
defineProps({
  status: { type: String, default: '' },
  actions: { type: Object, default: () => ({}) },
  streaming: { type: Boolean, default: false },
})
const emit = defineEmits(['act', 'end-day'])
</script>

<template>
  <div class="col-head">
    状态栏<span class="t">（引擎真值 · 与模型看到的同源）</span>
  </div>
  <div class="col-body">
    <div class="status-text">{{ status || '（等待开局……）' }}</div>

    <div v-if="actions.critical" class="status-actions">
      <div class="t">[关键抉择进行中] 行动暂不可用——先用中间区域的固定选项完成剧情。</div>
    </div>
    <div v-else-if="actions.day" class="status-actions">
      <div class="t">
        [第 {{ actions.day }} 天 · 行动点 {{ actions.action_points_left }}]
        今日行动（消耗行动点 = 推进时间；新剧情按天数条件自动触发）：
      </div>
      <div>
        <button
          v-for="a in actions.actions || []"
          :key="a.id"
          :disabled="streaming"
          @click="emit('act', a.id)"
        >
          {{ a.label }}
        </button>
        <button :disabled="streaming" @click="emit('end-day')">结束今天 →</button>
      </div>
      <div v-if="actions.action_points_left <= 0" class="t">
        行动点已用完——点「结束今天」进入下一天。
      </div>
    </div>
  </div>
</template>
