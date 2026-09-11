"""Phase 1 前奏：compress / reflect 零样本补测（双模型同输入对照）。

Phase 0 未能测到这两个窄模块（长局没跑起来 / 反思门控记忆不足），本脚本用历史存档
直接构造生产同款输入，零门槛补测：

- compress：旧摘要 + 新增历史 → 合并摘要（COMPRESS_SYSTEM，生产不传温度）
  · 增量合并样本：存档内既有【剧情摘要】 + 其后的窗口段
  · 首压样本：压缩前长档（memory-regression/baseline）的连续回合段，旧摘要为空
  · 指标：长度合规（≤800 目标）、关键实体保全率（数字/专名/约定类关键词）、与 flash 对照
- reflect：NPC 近期记忆（≥8 条）→ 关系洞察（REFLECT_SYSTEM，temp=0）
  · 样本：memory-regression 长档 NPC 记忆桶切片（12/8 条与中段切片）
  · 指标：格式合法（洞察|来源编号）、来源编号有效、洞察条数、人工抽查文本

用法：
  uv run python scripts/phase1_probe.py [--dry-run] [--flash-only]
      [--local-base-url URL --local-model NAME]
产出：reports/phase1-probe-<ts>.json + usage 落 reports/usage-phase1-probe.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI

from game_agent.compression import (
    COMPRESS_SYSTEM,
    SUMMARY_MAX_TARGET,
    SUMMARY_MARK,
    history_text,
    locate_summary,
)
from game_agent.config import DEFAULT_BASE_URL, DEFAULT_MODEL, Settings
from game_agent.llm import LLMClient
from game_agent.memory import REFLECT_MIN_MEMORIES, REFLECT_MATERIAL, REFLECT_SYSTEM, parse_insights
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

REPORTS_DIR = Path("reports")


def build_settings(args, flash: bool) -> Settings:
    if flash:
        env = dotenv_values(Path(".env"))
        return Settings(
            api_key=(env.get("DEEPSEEK_API_KEY") or "").strip(),
            base_url=(env.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).strip(),
            model=(env.get("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip(),
        )
    base_url = args.local_base_url or os.environ.get("LOCAL_BASE_URL", "")
    model = args.local_model or os.environ.get("LOCAL_MODEL", "")
    if not base_url or not model:
        print("[✗] 本地端点未配置：需 --local-base-url 与 --local-model")
        raise SystemExit(1)
    return Settings(api_key="sk-local", base_url=base_url, model=model)


def make_llm(settings: Settings, tracker: UsageTracker, flash: bool) -> LLMClient:
    client = OpenAI(
        api_key=settings.api_key, base_url=settings.base_url,
        max_retries=5 if flash else 2, timeout=180.0 if flash else 300.0,
    )
    return LLMClient(client, settings.model, [], tracker=tracker)


# ----------------------------------------------------------------------
# compress 样本构造
# ----------------------------------------------------------------------

def _key_entities(text: str, npc_names: list[str]) -> list[str]:
    """从新增历史段抽取关键串：数字、引号/书名号专名、NPC 名、约定类关键词片段。"""
    entities: list[str] = []
    for pat in (r"『[^』]+』", r"「[^」]{2,12}」", r"《[^》]+》"):
        entities.extend(re.findall(pat, text))
    entities.extend(re.findall(r"\d+(?:\.\d+)?(?:[两十百千]|年|月|日|两|枚|两银子)?", text))
    for name in npc_names:
        if name and name in text:
            entities.append(name)
    for kw in ("剑名", "师承", "约定", "承诺", "暗号", "答应", "托付", "婚约", "欠"):
        for m in re.finditer(kw, text):
            entities.append(text[max(0, m.start() - 4): m.end() + 12])
    # 去重 + 去超长
    seen, out = set(), []
    for e in entities:
        e = e.strip()
        if e and e not in seen and len(e) <= 30:
            seen.add(e)
            out.append(e)
    return out[:40]


def build_compress_samples() -> list[dict]:
    """从存档构造 compress 输入。返回 [{id, old_summary, new_text, npc_names}]。"""
    samples: list[dict] = []
    # --- 增量合并：既有摘要 + 其后窗口 ---
    for save, pack_path in [
        ("saves/longrun-xianxia_wendao.json", "world-packs/xianxia_wendao"),
        ("saves/smoke-flash-xianxia_wendao.json", "world-packs/xianxia_wendao"),
        ("saves/smoke-flash-urban_neon.json", "world-packs/urban_neon"),
    ]:
        data = json.loads(Path(save).read_text(encoding="utf-8"))
        hist = data.get("history") or []
        s = locate_summary(hist)
        if s < 0:
            continue
        pack = load_worldpack(pack_path)
        names = [n.name for n in pack.npcs.values()]
        old = str(hist[s].get("content") or "").replace(SUMMARY_MARK, "").strip()
        new_text = history_text(hist[s + 1:])
        if len(new_text) > 400:
            samples.append({
                "id": f"compress/merge/{Path(save).stem}",
                "old_summary": old, "new_text": new_text, "npc_names": names,
            })
    # --- 首压（无旧摘要）：压缩前长档连续段（baseline-checkpoint 为 A-2 前污染档，不用）---
    for save, pack_path, seg in [
        ("saves/memory-regression-checkpoint.json", "world-packs/baseline_probe",
         (40, 52)),   # 回合边界区间取段（消息索引由回合边界推）
        ("saves/memory-regression-checkpoint.json", "world-packs/baseline_probe",
         (10, 22)),   # 早期段（短而干净）
        ("saves/smoke-urban_neon.json", "world-packs/urban_neon", None),  # 全段（短档）
    ]:
        data = json.loads(Path(save).read_text(encoding="utf-8"))
        hist = data.get("history") or []
        if not hist:
            continue
        pack = load_worldpack(pack_path)
        names = [n.name for n in pack.npcs.values()]
        if seg is None:
            new_text = history_text(hist)
        else:
            # 按"无 name 的 user 消息"数出第 a~b 个回合的切片
            turn_pos = [i for i, m in enumerate(hist)
                        if m.get("role") == "user" and "name" not in m]
            lo, hi = seg
            if len(turn_pos) <= hi:
                hi = len(turn_pos) - 1
            lo = min(lo, max(0, hi - 12))
            new_text = history_text(hist[turn_pos[lo]: turn_pos[hi]])
        if len(new_text) > 1500:
            samples.append({
                "id": f"compress/first/{Path(save).stem}@{seg}",
                "old_summary": "", "new_text": new_text, "npc_names": names,
            })
    return samples


def eval_compress(sample: dict, output: str) -> dict:
    text = (output or "").strip()
    entities = _key_entities(sample["new_text"], sample["npc_names"])
    preserved = [e for e in entities if e in text]
    return {
        "len_chars": len(text),
        "len_ok": len(text) <= SUMMARY_MAX_TARGET,
        "n_entities": len(entities),
        "preserved": preserved,
        "preserve_rate": round(len(preserved) / len(entities), 3) if entities else None,
    }


# ----------------------------------------------------------------------
# reflect 样本构造
# ----------------------------------------------------------------------

def build_reflect_samples() -> list[dict]:
    """从存档 NPC 记忆桶构造 reflect 输入（≥8 条）。返回 [{id, npc_name, facts[]}]。"""
    samples: list[dict] = []
    for save, pack_path in [
        ("saves/memory-regression-checkpoint.json", "world-packs/baseline_probe"),
        ("saves/smoke-flash-xianxia_wendao.json", "world-packs/xianxia_wendao"),
        ("saves/smoke-flash-urban_neon.json", "world-packs/urban_neon"),
    ]:
        data = json.loads(Path(save).read_text(encoding="utf-8"))
        pack = load_worldpack(pack_path)
        # 旧格式（M2a）state 嵌套在 "state" 字段；新格式在顶层
        state = data.get("state", data)
        buckets = state.get("npc_memories") or {}
        for npc_id, entries in buckets.items():
            facts = [e["fact"] for e in entries if isinstance(e, dict) and e.get("fact")]
            if len(facts) < REFLECT_MIN_MEMORIES:
                continue
            name = pack.npcs[npc_id].name if npc_id in pack.npcs else npc_id
            for label, sl in (("last12", facts[-REFLECT_MATERIAL:]),
                              ("last8", facts[-8:]),
                              ("mid12", facts[-16:-4])):
                if len(sl) >= REFLECT_MIN_MEMORIES:
                    samples.append({
                        "id": f"reflect/{Path(save).stem}/{npc_id}/{label}",
                        "npc_name": name, "facts": sl,
                    })
    return samples


def eval_reflect(facts: list[str], output: str) -> dict:
    insights = parse_insights(output or "")
    n = len(facts)
    valid = []
    for text, indices in insights:
        valid.append({
            "text": text,
            "indices": list(indices),
            "indices_ok": all(1 <= i <= n for i in indices),
        })
    return {
        "n_insights": len(insights),
        "insights": valid,
        "format_ok": all(v["indices_ok"] for v in valid),
    }


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="compress/reflect 零样本补测")
    p.add_argument("--dry-run", action="store_true", help="只构造样本并打印，零 API")
    p.add_argument("--flash-only", action="store_true")
    p.add_argument("--local-base-url", default=None)
    p.add_argument("--local-model", default=None)
    args = p.parse_args(argv)

    comp_samples = build_compress_samples()
    refl_samples = build_reflect_samples()
    print(f"[样本] compress={len(comp_samples)} reflect={len(refl_samples)}")
    for s in comp_samples:
        print(f"  {s['id']}: 旧摘要={len(s['old_summary'])}字 新增={len(s['new_text'])}字")
    for s in refl_samples:
        print(f"  {s['id']}: {s['npc_name']} {len(s['facts'])} 条记忆")
    if args.dry_run:
        return 0

    tracker = UsageTracker(REPORTS_DIR / "usage-phase1-probe.jsonl")
    flash_settings = build_settings(args, flash=True)
    local_settings = None if args.flash_only else build_settings(args, flash=False)
    llm_f = make_llm(flash_settings, tracker, flash=True)
    llm_l = make_llm(local_settings, tracker, flash=False) if local_settings else None

    results: dict = {"compress": [], "reflect": []}
    for s in comp_samples:
        msgs = [
            {"role": "system", "content": COMPRESS_SYSTEM.format(target=SUMMARY_MAX_TARGET)},
            {"role": "user", "content": f"<旧摘要>\n{s['old_summary']}\n</旧摘要>\n\n"
                                        f"<新增历史>\n{s['new_text']}\n</新增历史>"},
        ]
        t0 = time.perf_counter()
        out_f = llm_f.complete(msgs, max_tokens=2000, purpose="compress")
        el_f = round(time.perf_counter() - t0, 2)
        out_l = llm_l.complete(msgs, max_tokens=2000, purpose="compress") if llm_l else None
        el_l = round(time.perf_counter() - t0 - el_f, 2) if llm_l else None
        results["compress"].append({
            "id": s["id"],
            "flash": {"output": out_f, "eval": eval_compress(s, out_f), "elapsed_s": el_f},
            "local": ({"output": out_l, "eval": eval_compress(s, out_l), "elapsed_s": el_l}
                      if out_l is not None else None),
        })
        print(f"[compress] {s['id']} "
              f"flash={len(out_f)}字/{results['compress'][-1]['flash']['eval']['preserve_rate']} "
              f"local={len(out_l or '')}字"
              f"/{results['compress'][-1]['local']['eval']['preserve_rate'] if out_l else '-'}")

    for s in refl_samples:
        numbered = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(s["facts"]))
        msgs = [
            {"role": "system", "content": REFLECT_SYSTEM},
            {"role": "user", "content": f"<角色>{s['npc_name']}</角色>\n\n"
                                        f"<近期记忆>\n{numbered}\n</近期记忆>"},
        ]
        out_f = llm_f.complete(msgs, max_tokens=200, temperature=0.0, purpose="reflect")
        out_l = (llm_l.complete(msgs, max_tokens=200, temperature=0.0, purpose="reflect")
                 if llm_l else None)
        results["reflect"].append({
            "id": s["id"],
            "flash": {"output": out_f, "eval": eval_reflect(s["facts"], out_f)},
            "local": ({"output": out_l, "eval": eval_reflect(s["facts"], out_l)}
                      if out_l is not None else None),
        })
        r = results["reflect"][-1]
        print(f"[reflect] {s['id']} flash={r['flash']['eval']['n_insights']}条/"
              f"{r['flash']['eval']['format_ok']} "
              f"local={r['local']['eval']['n_insights'] if r['local'] else '-'}条"
              f"/{r['local']['eval']['format_ok'] if r['local'] else '-'}")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"phase1-probe-{ts}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "models": {"flash": flash_settings.model, "local": local_settings.model if local_settings else None},
        **results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存 → {out}")
    print(tracker.cost_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
