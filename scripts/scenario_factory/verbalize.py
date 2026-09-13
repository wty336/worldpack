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

import math
import re
from dataclasses import dataclass, field as dc_field

from game_agent.budgets import complete_checked

from .cards import CHUNK_TOKENS, ScenarioCard

VERBALIZE_SYSTEM = (
    "你是文本演绎器。把给定的场景卡（JSON）演绎成一段自然中文叙事。"
    "纪律：①卡面 facts 的 anchors 专名/数字必须原样出现；②只写叙事正文——"
    "不得输出标签、不得列事实清单、不得在末尾总结；③不得新增卡面没有的事实性专名"
    "与数字；④语体/长度/脏度按用户指令（允许口语碎句与闲笔）；"
    # ⑤ 是发现⑩（2026-09-13）补的：judge 批 10% 的演绎在**念卡面 JSON**，两条直接念出标签 ✗✗
    "⑤**你写的是故事文本，不是任务说明、参数报告或卡面播报**——"
    "不得出现字段名/键名/数值标签/角色卡结构/任务术语"
    "（例如 in_material、expect、corruption、权重、节点数、卡面、场景卡、facts 数组）。"
)

# 元叙述硬杀词（发现⑩，2026-09-13 人读初审 + 全批检测取证）：命中即判"这不是叙事"。
# 取证：judge 批 59 条里 **6 条（10%）** 命中 ≥2 个元词，其中两条**直接念出标签**
# （`expect 栏标的是：问题类型，虚构事实`；`target_fact 指向 facts 数组的第 0 项`）✗✗ ——
# 而四道既有门全放行：anchors 在位 ✓ / 原词不在 ✓ / hook_gate 不撞 ✓ / 标签校验还判"要点成立" ✓
# ⇒ **没有一道问"这到底是不是一段叙事"**；漏掉的这一维正是决策 16 人读通路抓到的。
META_HARD = (
    "in_material", "target_fact", "old_summary", "new_summary", "corruption",
    "expect 栏", "问题类型", "虚构事实", "设定矛盾", "facts 数组", "场景卡",
    "枚举集合", "category 是", "scale 上", "nodes 记", "npcs 记",
)
# 温和词：技术/科幻题材里**叙事内**使用可能合法（如黑客黑话"权限节点"、"他把节点数了一遍"）
# → **只记不杀**，进样本字段供批次级观察（人读初审提的"元概念高频 → 判官学到捷径"的伪相关风险靠它盯）。
# 为什么把它们从硬杀里挪出来：误杀合法叙事要付吞吐代价（每张卡 1~4 次调用），
# 而"卡面/节点数/好感度"这类词在技术题材的**合法**叙事里确实会出现（2026-09-13 收紧口径）。
META_SOFT = ("卡面", "节点数", "好感度", "参数", "权重", "锚点", "实体列表",
             "缓冲区", "接口", "字段", "样本流")


def _meta_narration(text: str) -> list[str]:
    """**硬**元叙述词命中（空 = 通过）。"""
    return [t for t in META_HARD if t in text]


def _meta_soft(text: str) -> list[str]:
    """温和元词命中（只记录，不判定）。"""
    return [t for t in META_SOFT if t in text]


def _name_confusables(card: ScenarioCard, text: str) -> list[str]:
    """**近误人名报告**（发现⑩-D）：包内 NPC 名与文本中同长窗口的"一字之差"比对。

    人读初审实测：包内 NPC「沈清秋」被写成了「沈青秋」（另 3 条性别/人设漂移）。
    **只报不杀**——一字之差也可能是另一个真实人名；计数进样本，供批次级观察。
    """
    if not (card.pack and card.material):
        return []
    from .materialize import load_pack

    pack = load_pack(card.pack)
    out: list[str] = []
    for spec in pack.npcs.values():
        name = spec.name
        if len(name) < 3 or name in text:
            continue
        for i in range(max(0, len(text) - len(name) + 1)):
            win = text[i:i + len(name)]
            if sum(a != b for a, b in zip(win, name)) == 1:
                out.append(f"{name}→{win}")
                break
    return out


def _violations(card: ScenarioCard, text: str) -> list[str]:
    """演绎文本的**程序校验**（缺一即不合格）：anchors 在位 / 原词不出现 / **不是元叙述**。"""
    return (_missing_anchors(card, text) + _originals_present(card, text)
            + [f"元叙述:{t}" for t in _meta_narration(text)])
VERBALIZE_TEMPERATURE = 0.9  # 演绎要多样性（0.8~1.0 档）；标注/评判类仍 temp=0
MAX_ATTEMPTS = 2             # 缺要素 → 重演 1 次 → 仍缺则丢弃并计数


@dataclass
class VerbalizeResult:
    text: str
    attempts: int
    dropped: bool = False
    violations: list[str] = dc_field(default_factory=list)   # 丢弃原因明细（元叙述/缺 anchor…）


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


def _speaker_staging(card: ScenarioCard) -> tuple[str, str]:
    """judge 卡：说话人的**展示名**与其**角色卡全文**（取不到就退化为 id / 空串）。

    结构性修复（发现⑨，2026-09-13）：判定"问题类型是否真的成立"必须落在**被声明的说话人**身上，
    而原先的提示**根本没告诉演绎器说话人是谁**（更没给角色卡）→ 实测 narration 常常是别人在说话、
    或说话人沉默寡言 ⇒ 无论重演几次都判不出 OOC（标签门禁只能丢卡：judge 批 −32%、类别失衡）。
    """
    npc = (card.material.present[0] if card.material and card.material.present else "")
    if not (npc and card.pack):
        return npc, ""
    from .materialize import load_pack, speaker_card_text

    pack = load_pack(card.pack)
    spec = pack.npcs.get(npc)
    return (spec.name if spec is not None else npc), speaker_card_text(pack, npc)


def _judge_hint(card: ScenarioCard) -> str:
    """矛盾自然化指令——**按 category 分派**（一句话指令无法同时适配三类）。

    原稿只有一句"「」内的值必须出现、被改写事实的原词不得出现"，对 confab 是**反的**
    （confab 的 anchor 恰恰必须出现），会把 confab 演绎引到错方向。

    **2026-09-13 补舞台与硬要求**（发现⑨）：把说话人姓名 + 角色卡喂进去，并明确要求
    ① 出现其**直接引语**、② 矛盾**发生在他自己的话里**（不得只由旁人转述、不得用模糊指代省掉专名）。
    这三条正是 §7.5 标签校验的判据 —— **生成指令与验收判据对齐**，否则门禁只能一路丢卡。
    """
    c = card.corruptions[0]
    npc, voice = _speaker_staging(card)
    who = f"「{npc}」" if npc else "该说话人"
    stage = (
        f"\n\n**说话人与舞台（硬要求）**：矛盾的说话人是 {who}，其角色卡如下：\n{voice or '（未取到角色卡）'}\n"
        f"叙事里**必须**：① 出现 {who} 的**直接引语**（至少一句，标明是他说的话）；"
        f"② 该矛盾**发生在他/她自己的话里**（或叙述者对其处境的直接陈述里），"
        "**不得**只由旁人转述，也不得用「那个东西」这类模糊指代把关键专名省掉。"
        # 下面两条是"舞台文本反过来伤到别的门禁"的补丁（2026-09-13 探针实测）：
        # ① 把角色卡喂给模型后，它会**逐字照抄**卡里的句子 → 触发 `hook_gate`（≥4 字连续重合 = 多开一条通路）✗
        # ② 约束一多，模型会顾着演说话人而**漏掉卡面 anchors** → 演绎丢弃回升 ✗
        "③ 角色卡只用来**把握语气**，**不得逐字照抄**卡里的句子（照抄会让该卡失去它要考的那条通路）；"
        "④ 无论怎么改，卡面 facts 的 anchors（专名/数字）**必须原样出现**。")
    if c.category == "setting":
        hint = ("把被命中事实换个说法，**由 {who} 说出或认下**：detail 中「」内的**新值必须出现**，"
                "**原值不得出现**（这是要考的设定矛盾）。")
    elif c.category == "confab":
        hint = ("把 detail 提到的那件事**当作既成事实直接断言**——**由 {who} 亲口说出**最好"
                "（材料里从未有过它，这正是要考的点）；不得写成「听说 / 可能 / 似乎」，也不得只由旁人转述。")
    else:
        hint = ("让 {who} 的**台词明显违背其角色卡**（语气 / 底线 / 禁忌任选其一，且违背要一眼看得出）——"
                "判据是「说了与其人设相冲突的话」，而**不是**「没展现其风格」（沉默寡言/风格平淡不算 OOC）。")
    return stage + f"\n矛盾自然化：{hint.format(who=who)}\n（category={c.category}；detail：{c.detail}）"


def _chunk_plan(target: int) -> list[int]:
    """把长卡的总目标拆成每块目标（块数 = ceil(target / CHUNK_TOKENS)，余数摊到前几块）。"""
    n = max(1, math.ceil(target / CHUNK_TOKENS))
    base, rem = divmod(target, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def _continuation_hint(k: int, n: int, tail: str, missing: list[str] | None = None) -> str:
    """续写指令：回喂上一块的**结尾原文**并要求"紧接、不重复、不总结"（防接缝）。"""
    hint = (f"\n\n**续写第 {k + 1}/{n} 段**：紧接下面这段的结尾继续写（同一场景、同一语体、同一批人物），"
            f"不要重复、不要总结、不要另起开头：\n…{tail}")
    if missing:
        hint += ("\n本段请**自然地**再提到这些内容（专名/数字**原样写出**，不要列清单）："
                 + "、".join(missing))
    return hint


def _style_rule(card: ScenarioCard) -> str:
    """语体补充约束（发现⑩-A）：`技术术语` 体实测是**元叙述重灾区**（judge 批占 34%，元词命中集中于此）
    → 明确要求术语**只在叙事内**用。人读初审原话：「语体轴只写'技术术语'，没说术语只作叙事内比喻」。"""
    if card.axes.style == "技术术语":
        return ("\n**语体细则**：「技术术语」指**叙事之内**的术语（人物 / 机构 / 设备 / 流程的对话与描写），"
                "**不得**拿它来谈论这张卡、这套场景、你的任务，或任何字段 / 参数 / 数据结构本身。")
    return ""


def _chunked_user(card: ScenarioCard, per: int, k: int, n: int) -> str:
    """分块续写的首/次块用户指令：**把长度目标按块说清**。

    实测（2026-09-13 探针）：只给"全篇约 12000"时每块只写 1.3~2.6K 字（全篇 5.3~10.5K，达标 3/4）；
    根因是**没告诉模型本段的长度**——补上"本段约 {per} 字"。
    """
    user = (f"语体：{card.axes.style}；**本段**长度约 {per} 字"
            f"（全篇共 {n} 段、合计约 {card.history_spec.target_tokens} 字）；"
            f"可掺入的闲笔：{card.history_spec.noise}\n场景卡 JSON：\n"
            + card.model_dump_json())
    user += _style_rule(card)
    if card.module == "judge":
        user += _judge_hint(card)
    return user


def _verbalize_chunked(llm, card: ScenarioCard, *, purpose: str) -> str:
    """长卡**分块续写**（2026-09-13 实测重定档位后新增）。

    为什么必须分块：单次调用写不出一万二千字——实测 20K 档最长只产出 4.3K token / 12K 档实测最长 6.9K 字，
    而生产端被压缩的历史段实测达 **12,179 / 15,557 字**（真实存档），压缩质量又**只在这个长端才重要**。

    做法：每块目标 ≤ `CHUNK_TOKENS`（给单次输出上限留余量）；块间把上一块**结尾原文**回喂要求续写；
    拼完对**整段**做 anchors 校验，缺漏时**只补最后一块**（把缺的 anchors 明写进指令）——
    而不是整卡重演（对长卡那要 ×4~5 倍调用）。任一块写不出来（空/截断）→ 整卡失败。
    """
    chunks = _chunk_plan(card.history_spec.target_tokens)
    n = len(chunks)
    parts: list[str] = []
    for k, per in enumerate(chunks):
        base = _chunked_user(card, per, k, n)
        turn = base if k == 0 else base + _continuation_hint(k, n, parts[-1][-200:])
        text, finish = complete_checked(
            llm, [{"role": "system", "content": VERBALIZE_SYSTEM}, {"role": "user", "content": turn}],
            purpose=purpose, max_tokens=int(per * 1.2), temperature=VERBALIZE_TEMPERATURE)
        if not text.strip() or finish == "length":
            return ""
        parts.append(text.strip())
    joined = "\n".join(parts)
    missing = _missing_anchors(card, joined) + _originals_present(card, joined)
    if missing:  # 收尾补漏：只补一块，代价是 1 次调用而不是整卡重演
        text, finish = complete_checked(
            llm, [{"role": "system", "content": VERBALIZE_SYSTEM},
                  {"role": "user", "content": _chunked_user(card, chunks[-1], n, n)
                   + _continuation_hint(n, n, joined[-200:], missing)}],
            purpose=purpose, max_tokens=int(chunks[-1] * 1.2), temperature=VERBALIZE_TEMPERATURE)
        if text.strip() and finish != "length":
            joined = joined + "\n" + text.strip()
    return joined


def verbalize_card(llm, card: ScenarioCard, *, purpose: str = "aux") -> VerbalizeResult:
    """卡 → 自然文本。缺要素重演一次，仍缺则 dropped=True（调用方计数）。

    ``purpose`` **只作 usage 记账标签**（Task 11 成本回填要能把"演绎"开销拆到
    extract/judge/compress 三个模块），**不改变行为**：`LLMClient.model_for()` 只认
    judge/compress 两个键，其余一律回退主模型；`no_thinking_side_channel` 与调用预算
    也都不按 purpose 分派（预算由调用方显式传 max_tokens）。三条性质由
    `tests/test_scenario_factory.py::test_usage_purpose_labels_are_routing_neutral` 钉住。
    """
    user = (
        f"语体：{card.axes.style}；长度约 {card.history_spec.target_tokens} token；"
        f"可掺入的闲笔：{card.history_spec.noise}\n场景卡 JSON：\n"
        + card.model_dump_json()
    )
    user += _style_rule(card)
    if card.module == "judge":
        user += _judge_hint(card)
    if card.history_spec.target_tokens > CHUNK_TOKENS:
        # 长卡走分块续写（见 `_verbalize_chunked`）：**不整卡重演**（一次重演 = 再烧 4~5 次调用），
        # 块内已有"收尾补漏"，故这里只给一次机会，仍缺 anchors 就丢卡。
        text = _verbalize_chunked(llm, card, purpose=purpose)
        if text and not (v := _violations(card, text)):
            return VerbalizeResult(text=text, attempts=1)
        return VerbalizeResult(text="", attempts=1, dropped=True, violations=v if text else ["空文本"])
    msgs = [{"role": "system", "content": VERBALIZE_SYSTEM},
            {"role": "user", "content": user}]
    max_tokens = int(card.history_spec.target_tokens * 1.2)  # spec §4 的 ×1.2 上限
    last_v: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text, finish = complete_checked(llm, msgs, purpose=purpose,
                                        max_tokens=max_tokens,
                                        temperature=VERBALIZE_TEMPERATURE)
        if finish == "length":  # 截断丢弃（complete_checked 已升预算重试过一次）
            last_v = ["截断"]
            continue
        if not (last_v := _violations(card, text)):
            return VerbalizeResult(text=text, attempts=attempt)
    return VerbalizeResult(text="", attempts=MAX_ATTEMPTS, dropped=True, violations=last_v)
