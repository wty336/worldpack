/** 输入区：自由输入 + 发言 + 存档 + 换卡。
 *
 *  发言按钮与回车同权；生成期间禁用（防重复提交）。
 *  「存档」不再硬编码 `web.json`——后端会拒绝非法路径，但前端本来就不该写死文件名
 *  （多局会互相覆盖）。存档名由 `PlayView` 按"卡 + 模式"生成。
 */
<script setup>
import { ref } from 'vue'

const props = defineProps({
  streaming: { type: Boolean, default: false },
})
const emit = defineEmits(['say', 'save', 'leave'])

const text = ref('')
const input = ref(null)

function submit() {
  const t = text.value.trim()
  if (!t || props.streaming) return
  text.value = ''
  emit('say', t)
}
function focus() {
  input.value?.focus()
}
defineExpose({ focus })
</script>

<template>
  <div class="input-dock">
    <input
      ref="input"
      v-model="text"
      type="text"
      placeholder="说些什么……（回车发送）"
      :disabled="streaming"
      @keydown.enter="submit"
    >
    <button :disabled="streaming" @click="submit">发言</button>
    <button :disabled="streaming" @click="emit('save')">存档</button>
    <button :disabled="streaming" @click="emit('leave')">换一张卡</button>
  </div>
</template>
