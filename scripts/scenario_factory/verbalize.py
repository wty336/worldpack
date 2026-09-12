"""演绎器：卡 → 自然文本（spec §4）。只写表面形态；标签永不从文本反推。

程序校验（spec §4）——**按 corruption 的 category 分批**，三类的方向并不相同：

| category | 被命中事实的 anchors | corruption「」引用值 |
| --- | --- | --- |
| setting  | **须缺席**（原词不得出现） | 「新值」须在位 |
| confab   | **须在位**（叙事必须把编造说出来） | 被断言的 anchor 须在位 |
| ooc      | 须在位（不涉具体值） | 无引用 |

早先一版把 confab 也按 setting 处理（同一条 anchor 既要求"在位"、又列为"不得出现"）
→ **confab 卡恒被丢弃**（占 judge 配额 ≥40%，属重大静默损失），故此处按 category 分派。

缺失 → 重演 1 次 → 仍缺则丢弃该样本并计数。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from game_agent.budgets import complete_checked

from .cards import ScenarioCard

VERBALIZE_SYSTEM = (
    "你是文本演绎器。把给定的场景卡（JSON）演绎成一段自然中文叙事。"
    "纪律：①卡面 facts 的 anchors 专名/数字必须原样出现；②只写叙事正文——"
    "不得输出标签、不得列事实清单、不得在末尾总结；③不得新增卡面没有的事实性专名"
    "与数字；④语体/长度/脏度按用户指令（允许口语碎句与闲笔）。"
)
VERBALIZE_TEMPERATURE = 0.9  # 演绎要多样性（0.8~1.0 档）；标注/评判类仍 temp=0
MAX_ATTEMPTS = 2             # 缺要素 → 重演 1 次 → 仍缺则丢弃并计数


@dataclass
class VerbalizeResult:
    text: str
    attempts: int
    dropped: bool = False


def _corruption_swap(card: ScenarioCard) -> tuple[int | None, list[str], list[str]]:
    """按 category 返回 (被命中事实下标, **须在位**的值, **须缺席**的原值)。

    三类语义不同，早先一版把它们当成同一件事，导致 confab 卡**恒被丢弃**：

    - setting：原值须缺席、新值须在位（detail 格式 `「原值」演绎为「新值」`）
    - confab ：被断言的 anchor **须在位**（叙事必须把编造说出来），无缺席要求
    - ooc    ：不涉及具体值 → hit_idx=None（全部事实 anchors 须在位）
    """
    if card.module != "judge" or not card.corruptions:
        return None, [], []
    c = card.corruptions[0]
    quoted = re.findall(r"「([^」]+)」", c.detail)
    if c.category == "setting" and c.target_fact is not None:
        absent = [a for a in card.facts[c.target_fact].anchors if a in quoted]
        return c.target_fact, [q for q in quoted if q not in absent], absent
    if c.category == "confab":
        return c.target_fact, quoted, []
    return None, quoted, []


def _missing_anchors(card: ScenarioCard, text: str) -> list[str]:
    hit_idx, present, _ = _corruption_swap(card)
    missing = [a for i, f in enumerate(card.facts) if i != hit_idx
               for a in f.anchors if a not in text]
    missing += [p for p in present if p not in text]
    return missing


def _originals_present(card: ScenarioCard, text: str) -> list[str]:
    _, _, absent = _corruption_swap(card)
    return [a for a in absent if a in text]


def _judge_hint(card: ScenarioCard) -> str:
    """矛盾自然化指令——**按 category 分派**（一句话指令无法同时适配三类）。

    原稿只有一句"「」内的值必须出现、被改写事实的原词不得出现"，对 confab 是**反的**
    （confab 的 anchor 恰恰必须出现），会把 confab 演绎引到错方向。
    """
    c = card.corruptions[0]
    if c.category == "setting":
        hint = ("把被命中事实换个说法写进叙事：detail 中「」内的**新值必须出现**，"
                "**原值不得出现**（这是要考的设定矛盾）。")
    elif c.category == "confab":
        hint = ("把 detail 提到的那件事**当作既成事实直接断言**（材料里从未有过它，"
                "这正是要考的点）；不得写成「听说 / 可能 / 似乎」。")
    else:
        hint = "改写说话人的语气或底线，使其贴合 detail 的描述（不涉及具体专名）。"
    return f"\n矛盾自然化：{hint}\n（category={c.category}；detail：{c.detail}）"


def verbalize_card(llm, card: ScenarioCard) -> VerbalizeResult:
    """卡 → 自然文本。缺要素重演一次，仍缺则 dropped=True（调用方计数）。"""
    user = (
        f"语体：{card.axes.style}；长度约 {card.history_spec.target_tokens} token；"
        f"可掺入的闲笔：{card.history_spec.noise}\n场景卡 JSON：\n"
        + card.model_dump_json()
    )
    if card.module == "judge":
        user += _judge_hint(card)
    msgs = [{"role": "system", "content": VERBALIZE_SYSTEM},
            {"role": "user", "content": user}]
    max_tokens = int(card.history_spec.target_tokens * 1.2)  # spec §4 的 ×1.2 上限
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text, finish = complete_checked(llm, msgs, purpose="aux",
                                        max_tokens=max_tokens,
                                        temperature=VERBALIZE_TEMPERATURE)
        if finish == "length":  # 截断丢弃（complete_checked 已升预算重试过一次）
            continue
        if not _missing_anchors(card, text) and not _originals_present(card, text):
            return VerbalizeResult(text=text, attempts=attempt)
    return VerbalizeResult(text="", attempts=MAX_ATTEMPTS, dropped=True)
