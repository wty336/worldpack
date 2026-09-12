"""数据集装配（spec §9.1）：样本构建三分支 + 质量门/门禁/配额/去重/出库。

标签纪律：labels/expect/preserve_points 全部取自卡面，永不从文本反推（spec §2）。

**输入模板必须逐字对齐生产调用**（spec §4.2 开头的硬纪律，也是 spec §9.2「五模块输入契约
继承不动」的落地）——提示词一律从引擎 import，**不得另写**，否则训练分布 ≠ 推理分布：

- extract  模板引自 `game.py:_extract_facts`（同一行格式见 `extract_eval.py:159-166`）
- compress 模板引自 `game.py:_compress_history`

契约由守卫测试 `test_*_input_matches_production_template` 钉住，防两处漂移。

compress 拒绝采样三档（spec §6）：off=单次直出；long=仅长输入档 n=4；all=全量 n=4。
程序先杀三项：虚构（摘要「」词不在历史）/缺要点/超长。
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass, field as dc_field

from game_agent.budgets import COMPRESS_MAX_TOKENS, complete_checked
from game_agent.compression import COMPRESS_SYSTEM, SUMMARY_MAX_TARGET
from game_agent.memory import EXTRACT_SYSTEM
from game_agent.worldpack import WorldPack
from scripts.rubric_judge import select

from .cards import REPO_ROOT, ScenarioCard
from .materialize import MaterializeError, build_material, load_pack
from .verbalize import verbalize_card

# card_hook_check 不是包成员（脚本层）：注入 scripts/ 后 import
# （脚本层先例见 scripts/diag_turn.py:17；tests 层不用此法）
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from card_hook_check import CARD_FIELDS, card_hook  # noqa: E402

SAMPLE_VERSION = "route-a-v1"
LONG_INPUT_TOKENS = 8000   # spec §6「长输入档」阈值（计划口径；spec 原话为"20K 级"）
CANDIDATES_N = 4
SUMMARY_TEMPERATURE = 0.7  # 候选需多样性（spec §6 的 temp 0.8 档）；评委仍 temp=0


def extract_messages(card: ScenarioCard, turn: str) -> list[dict]:
    """生产同款 extract 输入（逐步对齐 `game.py:_extract_facts`）。"""
    existing = "；".join(card.existing)
    return [
        {"role": "system", "content": EXTRACT_SYSTEM},
        {"role": "user",
         "content": f"已有事实：{existing}\n\n<回合内容>\n{turn}\n</回合内容>"},
    ]


def compress_messages(card: ScenarioCard, history: str) -> list[dict]:
    """生产同款 compress 输入（逐步对齐 `game.py:_compress_history`）。"""
    return [
        {"role": "system", "content": COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET)},
        {"role": "user",
         "content": f"<旧摘要>\n{card.old_summary}\n</旧摘要>\n\n<新增历史>\n"
                    f"{history}\n</新增历史>"},
    ]


@dataclass
class BuildResult:
    sample: dict | None
    dropped_reason: str | None = None


def build_extract_sample(llm, card: ScenarioCard) -> BuildResult:
    r = verbalize_card(llm, card)
    if r.dropped:
        return BuildResult(None, "演绎丢弃")
    return BuildResult({
        "id": card.card_id, "module": "extract", "version": SAMPLE_VERSION,
        "genre": card.axes.genre,
        # 生产同款 user 段（含 `已有事实：` 与 `<回合内容>` 包裹）——不是裸文本
        "input": extract_messages(card, r.text)[1]["content"],
        "existing": list(card.existing),
        "expect_empty": not card.facts,   # facts 空 = 该回合无新事实 = 标签「无」（负例）
        "labels": [{"type": f.type, "text": f.text, "importance": f.importance}
                   for f in card.facts],
    })


def build_judge_sample(llm, card: ScenarioCard) -> BuildResult:
    try:
        material = build_material(card)
    except MaterializeError as e:
        return BuildResult(None, f"材料装配失败: {e}")
    r = verbalize_card(llm, card)
    if r.dropped:
        return BuildResult(None, "演绎丢弃")
    c = card.corruptions[0]
    sample = {
        "id": card.card_id, "module": "judge", "version": SAMPLE_VERSION,
        "genre": card.axes.genre, "pack": card.pack, "material": material,
        "narration": r.text, "expect": c.expect, "category": c.category,
        "speaker": card.material.present[0],
    }
    # **出厂门禁接在产线上**（原稿定义了 hook_gate 却从未调用 = 门禁不存在）：
    # confab 撞说话人角色卡会多开一条「设定矛盾」通路，拦截率虚高、跨包不可比。
    if hooked := hook_gate(sample, load_pack(card.pack)):
        return BuildResult(None, f"confab 撞卡: {'/'.join(hooked)}")
    return BuildResult(sample)


def _fabrication_hit(summary: str, history: str) -> str | None:
    """程序先杀①：摘要的「」引用词不在历史中 → 虚构。"""
    for w in re.findall(r"「([^」]+)」", summary):
        if w not in history:
            return w
    return None


def _program_kill(summary: str, history: str, card: ScenarioCard) -> str | None:
    if len(summary) > SUMMARY_MAX_TARGET:   # 生产目标长度（compression.SUMMARY_MAX_TARGET=800）
        return "超长"
    if w := _fabrication_hit(summary, history):
        return f"虚构:{w}"
    for p in card.preserve_points:  # 程序先杀②：缺要点
        if not any(a in summary for a in p.anchors):
            return f"缺要点:{p.text}"
    return None


def _summarize_once(llm, history: str, card: ScenarioCard) -> str:
    """生产同款 compress 调用（含 `COMPRESS_MAX_TOKENS=4000` 预算；截断即弃）。"""
    text, finish = complete_checked(llm, compress_messages(card, history),
                                    purpose="compress", max_tokens=COMPRESS_MAX_TOKENS,
                                    temperature=SUMMARY_TEMPERATURE)
    if finish != "stop":
        raise ValueError("摘要截断")
    return text


def _sampling_on(card: ScenarioCard, sampling: str) -> bool:
    return sampling == "all" or (
        sampling == "long" and card.history_spec.target_tokens >= LONG_INPUT_TOKENS)


def build_compress_sample(llm, card: ScenarioCard, *, sampling: str = "off"
                          ) -> BuildResult:
    hist = verbalize_card(llm, card)
    if hist.dropped:
        return BuildResult(None, "历史演绎丢弃")
    # **单一素材真源**：模型看到的就是这一段（含 <旧摘要>/<新增历史> 包裹），
    # 故样本 input、程序先杀的虚构素材、评委的【材料】三处都用它 —— 早先版本只有
    # hist.text（裸历史），造成三处后果：① 样本 input 与生产模板不一致（spec §4.2 硬纪律）；
    # ② 增量合并档的旧摘要根本不进样本（25% 的卡，模型学不到"读旧摘要→合并"）；
    # ③ 虚构判定以裸历史为素材，旧摘要里的「」引用词会被误判成编造 → 候选被误杀。
    source = compress_messages(card, hist.text)[1]["content"]
    n = CANDIDATES_N if _sampling_on(card, sampling) else 1
    cands, killed = [], []
    for _ in range(n):
        try:
            s = _summarize_once(llm, hist.text, card)
        except ValueError as e:
            killed.append(str(e))
            continue
        if why := _program_kill(s, source, card):
            killed.append(why)
            continue
        cands.append(s)
    if not cands:
        return BuildResult(None, f"候选全杀: {killed}")
    best = select(llm, candidates=cands, material=source,
                  preserve_points=card.preserve_points) if len(cands) > 1 else 0
    return BuildResult({
        "id": card.card_id, "module": "compress", "version": SAMPLE_VERSION,
        "genre": card.axes.genre,
        # 生产同款 user 段（与 extract 侧对称：extract 也存带模板包裹的 user 段）
        "input": source, "output": cands[best],
        "sampling": sampling, "candidates": len(cands), "killed": killed,
        "long_input": card.history_spec.target_tokens >= LONG_INPUT_TOKENS,
        "preserve_points": [{"text": p.text, "anchors": p.anchors}
                            for p in card.preserve_points],
    })


# ---------------------------------------------------------------------------
# 质量门与出厂门禁（spec §9.2：card_hook_check 复用不重写）
# ---------------------------------------------------------------------------

GATE_MAX_DROP_RATE = 0.30   # 丢弃率超阈 → 批作废（先停产线，不硬凑量）
QUALITY_SAMPLE_RATE = 0.20  # §7.4 质检员抽检比例（常驻关卡）


@dataclass
class BatchStats:
    built: int = 0
    dropped: int = 0
    reasons: Counter = dc_field(default_factory=Counter)


def hook_gate(sample: dict, pack: WorldPack) -> list[str]:
    """confab 出厂门禁：narration × 说话人角色卡（`CARD_FIELDS`）的**词面**撞卡 → 返回撞词。

    依据 `card_hook_check.py` 文首实证：confab 撞上说话人角色卡（底线/禁忌/说话风格）会让
    判官多一条「设定矛盾」短路 —— 拦截率虚高、跨包不可比（实测同族用例可被抬到 88%）。
    本门禁只查 **confab**；词面之外的部分（语义撞卡、决策 16 的"断言不得由材料已有事实
    组合推出"）**不可程序化**，走人读清单 `manual_review_row()`。
    """
    if sample.get("category") != "confab":
        return []
    spec = pack.npcs.get(sample["speaker"])
    card_text = (" ".join(str(spec.model_dump().get(f)) for f in CARD_FIELDS)
                 if spec else "")
    return card_hook(sample["narration"], card_text)


def quality_gate(stats: BatchStats) -> str | None:
    """批级质量门：丢弃率超阈 → 返回原因（调用方作废该批，先停产线不硬凑量）。"""
    total = stats.built + stats.dropped
    if total and stats.dropped / total > GATE_MAX_DROP_RATE:
        return f"丢弃率 {stats.dropped}/{total} 超 {GATE_MAX_DROP_RATE:.0%}"
    return None


def manual_review_row(sample: dict) -> dict:
    """confab 人读清单行（决策 16 的**不可程序化**检查）：

    「断言不得由材料已有事实组合推出」只能人读 —— 逐行核对 material 与 narration。
    随交付，不替代抽检。
    """
    return {"id": sample["id"], "material": sample["material"],
            "narration": sample["narration"], "expect": sample["expect"]}
