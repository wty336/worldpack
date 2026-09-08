"""LLM-Judge 语义校验（M2b）：OOC / 设定矛盾 / confabulation 倾向。

设计（plan-m2.md §3）：
- 每 N 回合（默认 5）用一次轻量调用检查最新叙事；
- 判定失败 → 注入「校验反馈」到历史，让模型在**下一轮叙事中自然修正**
  （相对"当轮重生成"的偏差：流式输出下坏文本已到达玩家，重生成会造成
  两次显示；下轮修正符合叙事游戏的体验，且与反重复提示机制同构）；
- 校验是侧信道：调用失败静默降级，不影响主线。
"""

from __future__ import annotations

JUDGE_SYSTEM = (
    "你是游戏叙事的质量校验员。对照给定材料，检查最新一轮叙事是否存在以下问题：\n"
    "① OOC：角色说话做事违背其人设、语气、底线；\n"
    "② 设定矛盾：与世界观、既定事实、已发生事件、关键事实冲突；\n"
    "③ 虚构事实：编造从未发生的约定、承诺、事件（尤其注意把泛泛之语升级成具体承诺）。\n"
    "若没有问题，只输出：通过\n"
    "若有问题，输出：问题类型：具体描述（引用叙事原文），最多列 2 条。"
)

JUDGE_MAX_TOKENS = 500  # 规则：任何 LLM 调用预算 ≥ 500（M2a 复盘 #3）
JUDGE_EMPTY_RETRY_TOKENS = 2000  # 空响应升级预算：思考模式偶发烧光预算，空 = 未知，不得静默放行
JUDGE_TEMPERATURE = 0.0  # E1（P0）：判定类调用固定温度 0，保证质量门禁结果可复现


def parse_verdict(output: str) -> tuple[bool, str]:
    """解析判定输出。True = 通过。空输出按"通过"降级（见 check 的升级重试说明）。"""
    text = (output or "").strip()
    if not text:
        return True, ""
    head = text[:10].replace(" ", "")
    return head.startswith("通过"), text


class JudgeSystem:
    def __init__(self, llm):
        self.llm = llm

    def _judge_call(self, narration: str, materials: str, max_tokens: int) -> str:
        return self.llm.complete(
            [
                {"role": "system", "content": JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": f"<材料>\n{materials}\n</材料>\n\n<最新叙事>\n{narration}\n</最新叙事>",
                },
            ],
            max_tokens=max_tokens,
            temperature=JUDGE_TEMPERATURE,
            purpose="judge",
        )

    def check(self, narration: str, materials: str) -> tuple[bool, str]:
        """检查一轮叙事。返回 (是否通过, 判定原文)。调用失败时返回 (True, '')（静默降级）。

        B（素材导入工具）发现：思考模式偶发把 500 预算烧在推理链上导致空输出，
        而 parse_verdict('') 会静默放行——空 = 未知，不是"通过"。空输出时升级预算
        重试一次（同温度 0，推理完成即可产出判定）。
        """
        try:
            output = self._judge_call(narration, materials, JUDGE_MAX_TOKENS)
            if not output.strip():
                output = self._judge_call(narration, materials, JUDGE_EMPTY_RETRY_TOKENS)
        except Exception:  # noqa: BLE001
            return True, ""
        return parse_verdict(output)
