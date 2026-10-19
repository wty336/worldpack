/** 恢复痕迹的**唯一**文案表（引擎侧 `_note_recovery("...")` 的 key → 给玩家看的话）。
 *
 *  **为什么单独一个模块**（N10-2 期间发现的真缺陷）：这张表此前在 `ProseStream.vue`
 *  与 `PlayView.vue` **各存一份**，于是新增一种恢复手段时只改一处，另一处就把
 *  英文 key 直接怼给玩家看（`narration_only` 就是这样露出来的）。两份真值必然分叉，
 *  这不是"注意同步"能解决的，只能合并。
 *
 *  文案纪律：**只描述机制，不叙述剧情**（引擎层不含内容文案），并且必须说清
 *  "数值有没有变"——玩家最需要知道的就是这个。
 */
export const RECOVERY_LABELS = {
  overflow_recovered: '本轮上下文超限，已压缩历史后重试成功',
  critique: '本轮初稿未通过自检，已重写',
  meltdown: '本轮多次未达协议，已跳过（效果未生效）',
  // 模型只写了正文、没按协议调工具 → 叙事被接受，但没有效果落地。
  // 点明"数值未变化"是关键：否则玩家会以为正文里那句"武力大涨"真的生效了。
  narration_only: '本轮模型只写了正文、未调用工具，已按零效果叙事接受（数值与道具未变化）',
}

/** 把 `recovered` 列表翻成一句中性说明（空列表 → 空串）。 */
export function recoveryNote(list, subTurns = 1) {
  const parts = (list || []).map((r) => RECOVERY_LABELS[r] || r)
  if (!parts.length) return ''
  const sub = subTurns > 1 ? `，共生成 ${subTurns} 次` : ''
  return `（${parts.join('；')}${sub}）`
}
