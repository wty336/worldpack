/** 非阻塞提示条。
 *
 *  替换了 Stage A 的 `alert()`：阻塞弹窗会打断阅读，而玩家实测反馈里
 *  "不知道是卡了还是模型在思考""点了没反应"都属于**反馈缺失**这一类问题。
 *  规则：成功 4 秒自动消失，错误常驻直到下一次操作（错误需要被读到）。
 */
<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  message: { type: String, default: '' },
  isError: { type: Boolean, default: false },
})
const visible = ref(false)
let timer = null

watch(
  () => props.message,
  (m) => {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
    visible.value = !!m
    if (m && !props.isError) timer = setTimeout(() => (visible.value = false), 4000)
  },
)
</script>

<template>
  <div v-if="visible" class="notice" :class="{ err: isError }">{{ message }}</div>
</template>
