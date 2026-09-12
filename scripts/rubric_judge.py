"""rubric 评委（spec §7）：选优 / 打分 / 成对比较 / 探针校准 / 自然度质检，一个入口多种模式。

纪律（spec §7.3）：
- 评委一律 **temp=0**（判定类；演绎才是 0.8~1.0）；
- 成对比较 = 匿名 + **位置交换 ×3 重复 = 每对 6 次**调用（spec §10.2 口径）；
- 探针检出口径——打分制 = 探针的**对应维度**落 0 分档；成对比较 = 探针（坏）与干净样本配对须判负；
- 评委只评形态，**永不输出标签**（标签在卡上）。

预算：`MAX_TOKENS = budgets.MIN_CALL_TOKENS`（**单一真源**，不写死数字）。
思考模型会把推理链算进 `max_tokens`，给 200 会被思考吃光 → 空判词（retro §5.3 那批 bug）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import random
import re
import sys

from game_agent.budgets import MIN_CALL_TOKENS, complete_checked

from scripts.scenario_factory.cards import PreservePoint

DIMS = ("保真", "简洁", "结构", "流畅")
SCORE_TEMPERATURE = 0.0
PAIRWISE_REPEATS = 3  # ×2 位置 = 6 次调用
# 评委单次预算：**不得低于 budgets.MIN_CALL_TOKENS（500）**——硬规则（budgets.py:20）。
# 走单一真源、不写死数字，避免两处漂移。
MAX_TOKENS = MIN_CALL_TOKENS

RUBRIC_SYSTEM = (
    "你是压缩摘要的质量评委。对照【材料】与【必保全要点】，按四维量规给【待评摘要】打 0/1/2 分，"
    "只输出一行 JSON，不要任何额外文字。\n"
    "保真：2=要点全在且无材料外事实；1=缺 1 个要点或 1 处轻微走样；0=缺 ≥2 个要点或出现材料外事实。\n"
    "简洁：2=无复述冗余；1=轻微冗余；0=大段照搬或兜圈。\n"
    "结构：2=时间与因果清楚；1=轻微跳跃；0=支离破碎。\n"
    "流畅：2=自然书面中文；1=有语病不妨碍理解；0=难以卒读。\n"
    '输出格式：{"保真": 0, "简洁": 0, "结构": 0, "流畅": 0}'
)
PAIRWISE_SYSTEM = (
    "你是压缩摘要评委。对照【材料】与【必保全要点】，按保真>简洁>结构>流畅的优先级"
    "判断候选 A / 候选 B 谁更好。只输出一行 JSON："
    '{"winner": "A"} 或 {"winner": "B"} 或 {"winner": "tie"}。'
)

PROBE_KINDS = ("删要点", "注入虚构", "打乱结构")
_PROBE_DIM = {"删要点": "保真", "注入虚构": "保真", "打乱结构": "结构"}
_FABRICATED = ["北冥真人", "天外星舰", "幽冥鬼市"]  # 必不在任何材料内的虚构专名


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[^{}]*\}", text or "", re.S)
        if not m:
            raise ValueError(f"评委输出不含 JSON: {(text or '')[:80]!r}")
        return json.loads(m.group(0))


def _complete(llm, system: str, user: str, *, purpose: str) -> str:
    text, finish = complete_checked(
        llm, [{"role": "system", "content": system}, {"role": "user", "content": user}],
        purpose=purpose, max_tokens=MAX_TOKENS, temperature=SCORE_TEMPERATURE)
    if finish != "stop":
        raise ValueError(f"评委输出截断: finish={finish}")
    return text


def _points_text(preserve_points: list[PreservePoint]) -> str:
    return "；".join(p.text for p in preserve_points) or "（无）"


def score(llm, *, summary: str, material: str,
          preserve_points: list[PreservePoint],
          purpose: str = "rubric_score") -> dict[str, int]:
    """打分模式：四维各 0/1/2。评委输出异常 → ValueError（调用方计批次异常）。

    `purpose` 只作 usage 记账标签（成本回填要能拆开"评测打分"与"拒绝采样选优"），
    不改变路由（`model_for()` 只认 judge/compress，其余回退主模型）。
    """
    user = (f"【材料】\n{material}\n\n【必保全要点】\n{_points_text(preserve_points)}"
            f"\n\n【待评摘要】\n{summary}")
    d = _extract_json(_complete(llm, RUBRIC_SYSTEM, user, purpose=purpose))
    out = {k: int(d[k]) for k in DIMS}
    if any(v not in (0, 1, 2) for v in out.values()):
        raise ValueError(f"维度分越界: {out}")
    return out


def select(llm, *, candidates: list[str], material: str,
           preserve_points: list[PreservePoint]) -> int:
    """选优模式（拒绝采样用，spec §6）：总分最高者，同分取先。"""
    best, best_i = -1, 0
    for i, c in enumerate(candidates):
        total = sum(score(llm, summary=c, material=material, purpose="rubric_select",
                          preserve_points=preserve_points).values())
        if total > best:
            best, best_i = total, i
    return best_i


def _judge_once(llm, *, first: str, second: str, material: str,
                preserve_points: list[PreservePoint]) -> str:
    user = (f"【材料】\n{material}\n\n【必保全要点】\n{_points_text(preserve_points)}"
            f"\n\n【候选 A】\n{first}\n\n【候选 B】\n{second}")
    return str(_extract_json(
        _complete(llm, PAIRWISE_SYSTEM, user, purpose="rubric_pairwise")).get("winner"))


def pairwise(llm, *, a: str, b: str, material: str,
             preserve_points: list[PreservePoint]) -> str:
    """成对模式：返回 'A' | 'B' | 'tie'。匿名 + 位置交换 ×3 重复，多数票；平票 → tie。"""
    votes = {"A": 0, "B": 0, "tie": 0}
    for _ in range(PAIRWISE_REPEATS):
        w1 = _judge_once(llm, first=a, second=b, material=material,
                         preserve_points=preserve_points)
        votes[w1 if w1 in votes else "tie"] += 1
        w2 = _judge_once(llm, first=b, second=a, material=material,
                         preserve_points=preserve_points)
        votes[{"A": "B", "B": "A"}.get(w2, "tie")] += 1  # 换位的胜者映射回原始
    top = max(votes.values())
    winners = [k for k, v in votes.items() if v == top]
    return winners[0] if len(winners) == 1 else "tie"


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[。！？；\n])", text) if s]


def make_probe(summary: str, preserve_points: list[PreservePoint], *,
               kind: str, rng: random.Random) -> str:
    """探针模式·程序改坏（spec §7.3）：返回一份已知缺陷样本。"""
    if kind == "删要点":
        anchor = preserve_points[0].anchors[0]
        kept = [s for s in _sentences(summary) if anchor not in s]
        return "".join(kept) if kept else "（要点已删）"
    if kind == "注入虚构":
        return summary + f"后来{rng.choice(_FABRICATED)}现身，接管了一切。"
    if kind == "打乱结构":
        parts = _sentences(summary)
        if len(parts) < 2:
            return summary[::-1]
        shuffled = parts[:]
        while shuffled == parts:
            rng.shuffle(shuffled)
        return "".join(shuffled)
    raise ValueError(f"未知探针坏法: {kind}")


def probe_detected(scores: dict[str, int], kind: str) -> bool:
    """打分制检出口径（spec §7.3）：探针的对应维度分落入 0 分档。"""
    return scores.get(_PROBE_DIM[kind]) == 0


# ---- §7.4 数据质检员（第三处评委用法）：抽检自然度，防演绎器退化回模板 ----
NATURALNESS_SYSTEM = (
    "你是数据质检员。只判断这段中文文本的**自然度/模板味**，不评判内容对错。"
    "2=像人写的自然叙事；1=略有拼凑感但不刺眼；0=明显填空式模板或复读机。"
    '只输出一行 JSON：{"自然度": 0}'
)


def naturalness(llm, *, text: str) -> int:
    """抽检单条文本的自然度（0~2）。§7.4：<1 剔除并重造。"""
    d = _extract_json(_complete(llm, NATURALNESS_SYSTEM, f"【待检文本】\n{text}",
                                purpose="rubric_quality"))
    v = int(d["自然度"])
    if v not in (0, 1, 2):
        raise ValueError(f"自然度分越界: {v}")
    return v


def quality_sample(llm, samples: list[dict], *, rate: float = 0.20,
                   seed: int = 20260912) -> list[str]:
    """按 rate 抽检合成样本，返回自然度 <1 的 id（调用方剔除并重造）。**常驻关卡**，非一次性。"""
    if not samples:
        return []
    rng = random.Random(seed)
    k = min(max(1, math.ceil(len(samples) * rate)), len(samples))
    bad: list[str] = []
    for i in sorted(rng.sample(range(len(samples)), k=k)):  # 升序消费，结果与顺序无关
        s = samples[i]
        if naturalness(llm, text=s.get("input") or s.get("narration") or "") < 1:
            bad.append(s["id"])
    return bad


# ---------------------------------------------------------------------------
# 轨道 2 批跑（spec §7.2/§7.3：掺探针 → 打分 → 检出率门）
# ---------------------------------------------------------------------------

PROBE_MIN_RATE = 0.10   # 探针掺入 ≥10%
DETECT_MIN = 0.90       # 检出率 <90% → 当批成绩全部作废
PROBE_SEED = 20260912


def prompt_version() -> str:
    """rubric 提示词指纹（spec §6.4/§9.2：报告必须自证口径）。评委提示词一改即换新值。"""
    return hashlib.sha256(
        (RUBRIC_SYSTEM + PAIRWISE_SYSTEM + NATURALNESS_SYSTEM).encode("utf-8")
    ).hexdigest()[:16]


def budget_policy() -> str:
    """预算策略指纹（budgets.py 的 sha 前 12 位）——改常量即整套重测（spec §9.2 继承项）。"""
    import game_agent.budgets as _b
    return "budgets.py@" + hashlib.sha256(
        pathlib.Path(_b.__file__).read_bytes()).hexdigest()[:12]


def probe_positions(n: int, rate: float, seed: int = PROBE_SEED) -> list[int]:
    """探针抽样位置（**与 run_eval 同源**：测试据此构造夹具、报告据此留痕）。"""
    if n <= 0:
        return []
    k = min(max(1, math.ceil(n * rate)), n)
    return sorted(random.Random(seed).sample(range(n), k=k))


def _restore_points(sample: dict) -> list[PreservePoint]:
    return [PreservePoint(text=p["text"], anchors=p["anchors"])
            for p in sample.get("preserve_points", [])]


def run_eval(llm, samples: list[dict], *, probe_rate: float = PROBE_MIN_RATE,
             seed: int = PROBE_SEED, meta: dict | None = None) -> dict:
    """对 compress 样本批跑打分轨。探针替换法：被抽中的样本以其改坏版送入评委，
    成绩只计入探针检出统计，不混入干净样本的分数分布（防污染报告口径）。

    `meta`：调用方注入溯源指纹（prompt_version / budget_policy / endpoint / judge_model），
    满足 spec §6.4 的报告 schema —— **缺指纹的报告不予采信**。
    """
    probe_idx = set(probe_positions(len(samples), probe_rate, seed))
    kind_rng = random.Random(seed + 1)   # 与位置抽样**解耦**：改其一不影响另一
    rows, probes = [], []
    for i, s in enumerate(samples):
        pps = _restore_points(s)
        if i in probe_idx:
            kind = kind_rng.choice(PROBE_KINDS)
            bad = make_probe(s["output"], pps, kind=kind, rng=kind_rng)
            sc = score(llm, summary=bad, material=s["input"], preserve_points=pps)
            probes.append({"id": s["id"], "kind": kind,
                           "detected": probe_detected(sc, kind)})
        else:
            sc = score(llm, summary=s["output"], material=s["input"],
                       preserve_points=pps)
            rows.append({"id": s["id"], **sc})
    det = (sum(p["detected"] for p in probes) / len(probes)) if probes else 1.0
    return {"scores": rows, "probes": probes, "probe_detection": det,
            "probe_indices": sorted(probe_idx),
            "batch_valid": det >= DETECT_MIN, **(meta or {})}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="rubric 轨道 2 批跑（spec §7）")
    p.add_argument("--samples", required=True, help="compress.jsonl（出库产物）")
    p.add_argument("--probe-rate", type=float, default=PROBE_MIN_RATE)
    p.add_argument("--report", default=None, help="报告输出路径（json）")
    args = p.parse_args(argv)
    from game_agent.config import load_settings
    from game_agent.endpoint import fingerprint_for
    from game_agent.llm import LLMClient
    from game_agent.usage import UsageTracker

    samples = [json.loads(line) for line in
               pathlib.Path(args.samples).read_text(encoding="utf-8").splitlines() if line]
    settings = load_settings()
    if not settings.has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY（.env）")
        return 1
    tracker = UsageTracker("reports/usage-rubric.jsonl")
    llm = LLMClient.from_settings(settings, [], tracker=tracker)
    rep = run_eval(llm, samples, probe_rate=args.probe_rate, meta={
        "prompt_version": prompt_version(),
        "budget_policy": budget_policy(),
        "judge_model": llm.model_for("aux"),
        "endpoint": fingerprint_for(settings, "aux"),
        "sample_set": str(args.samples),
    })
    out = json.dumps(rep, ensure_ascii=False, indent=2)
    if args.report:
        pathlib.Path(args.report).write_text(out, encoding="utf-8")
    print(f"探针检出率 {rep['probe_detection']:.0%}（门 {DETECT_MIN:.0%}）；"
          f"有效样本 {len(rep['scores'])} 条 · prompt_version={rep['prompt_version']}")
    if not rep["batch_valid"]:
        print("[✗] 探针检出率不达标——当批成绩全部作废（报告已带 batch_valid=false 留痕），"
              "修评委提示词后整批重评")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
