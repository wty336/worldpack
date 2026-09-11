"""ICR 双模型对拍采样（local-14b 实验 Phase 0，docs/plan-local-14b.md §5.3 步骤 3 / §7.4）。

同一回合前缀分别喂 flash（.env 官方配置）与本地 14B（--local-* 覆盖）各产一轮，
逐对统计 A 轴（协议一次通过/熔断/参数非法/脏文本/截断）+ 落 D 轴匿名盲评样本池
+ E 轴每样本耗时。采样/截断/配对校验可 `--dry-run` 离线验证（零 API）。

用法：
  uv run python scripts/icr_sampling.py \\
      [--pack <路径> --save <存档>]...      # 成对传入；缺省按 saves/smoke-<包>.json 推断
      [--per-pack 30] [--seed 42] [--min-turn 2] [--sample-mode even|random]
      [--truncate-tokens N] [--keep-turns 6] [--with-status] [--max-iters 3]
      [--local-base-url URL] [--local-model NAME] [--local-api-key KEY] [--timeout-local 300]
      [--warmup] [--flash-only] [--dry-run]

产出：reports/icr-<ts>.json（含 reveal 映射 + A 轴汇总）、saves/icr-pool-<ts>.json
（匿名盲评池，无模型名）、saves/usage-icr.jsonl（两模型共用，按 model 字段分账）。

纪律：两个模型永远收到同一份输入（含截断后输入）；判定类不在此脚本；单样本隔离，
一次熔断/API 错误不中断整轮。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from dotenv import dotenv_values
from openai import OpenAI

from game_agent.compression import ensure_pairing, find_turn_cut, history_tokens, locate_summary
from game_agent.config import DEFAULT_BASE_URL, DEFAULT_MODEL, Settings
from game_agent.context import ContextBuilder
from game_agent.llm import (
    LLMClient,
    LLMTurnError,
    build_tools,
    clean_narration,
)
from game_agent.memory import MemorySystem
from game_agent.save import load_game, load_history
from game_agent.state import GameState
from game_agent.stats import StatsSystem
from game_agent.storyline import StorylineEngine, _forbidden_tokens
from game_agent.usage import UsageTracker
from game_agent.worldpack import load_worldpack

REPORTS_DIR = Path("reports")
SAVE_DIR = Path("saves")


# ----------------------------------------------------------------------
# 回合边界与采样
# ----------------------------------------------------------------------

def is_turn_start(msg: dict) -> bool:
    """回合起点 = 真实玩家输入（user 无 name）或事件脚本（engine + 【事件】前缀）。

    比 compression.find_turn_cut 严格：不把回合中段的 [引擎提示] 当起点。
    """
    if msg.get("role") != "user":
        return False
    if "name" not in msg:
        return True
    if msg.get("name") == "engine" and str(msg.get("content", "")).startswith("【事件】"):
        return True
    return False


def turn_boundaries(history: list[dict]) -> list[int]:
    return [i for i, m in enumerate(history) if is_turn_start(m)]


def select_indices(n_boundaries: int, n: int, seed: int, mode: str, min_turn: int) -> list[int]:
    """从边界序号中等距分层采样（覆盖早/中/晚期），不足全取。min_turn 跳过开场奇异样本。"""
    candidates = list(range(min_turn, n_boundaries))
    if len(candidates) <= n:
        return candidates
    rng = random.Random(seed)
    if mode == "random":
        return sorted(rng.sample(candidates, n))
    step = len(candidates) / n
    return sorted({candidates[min(int(k * step), len(candidates) - 1)] for k in range(n)})


def truncate_prefix(prefix: list[dict], tokens: int, keep_turns: int) -> tuple[list[dict], bool]:
    """前缀超 tokens 时按压缩规则截断（保留既有摘要 + 近窗），配对不成立则放弃截断。"""
    if history_tokens(prefix) <= tokens:
        return prefix, False
    cut = find_turn_cut(prefix, keep_turns)
    if cut <= 0:
        # 兜底：旧格式档（A-2 前状态栏是无 name 的 user 消息）会让 find_turn_cut 失效，
        # 退回本脚本的回合边界规则取切点；仍不可行才放弃截断。
        bds = turn_boundaries(prefix)
        if len(bds) > keep_turns:
            cut = bds[-keep_turns]
        else:
            return prefix, False
    s = locate_summary(prefix)
    if s >= 0:
        truncated = [prefix[s], *prefix[cut:]]
    else:
        truncated = prefix[cut:]
    if not ensure_pairing(truncated):
        return prefix, False
    return truncated, True


# ----------------------------------------------------------------------
# 双模型 client
# ----------------------------------------------------------------------

def build_flash_settings(env_file: Path) -> Settings:
    """flash 侧：直接读 .env 文件（dotenv_values 不经 os.environ，防 shell 残留污染）。"""
    env = dotenv_values(env_file)
    return Settings(
        api_key=(env.get("DEEPSEEK_API_KEY") or "").strip(),
        base_url=(env.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).strip(),
        model=(env.get("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip(),
        judge_model=(env.get("DEEPSEEK_JUDGE_MODEL") or "").strip(),
        compress_model=(env.get("DEEPSEEK_COMPRESS_MODEL") or "").strip(),
    )


def build_local_settings(args) -> Settings:
    base_url = args.local_base_url or os.environ.get("LOCAL_BASE_URL", "")
    model = args.local_model or os.environ.get("LOCAL_MODEL", "")
    api_key = args.local_api_key or os.environ.get("LOCAL_API_KEY", "") or "sk-local"
    if not base_url or not model:
        print("[✗] 本地端点未配置：需 --local-base-url 与 --local-model（或 LOCAL_* env）")
        raise SystemExit(1)
    return Settings(api_key=api_key, base_url=base_url, model=model)


# ----------------------------------------------------------------------
# 单样本执行与 A 轴信号提取
# ----------------------------------------------------------------------

def run_sample(llm: LLMClient, messages: list[dict], env, max_iters: int) -> dict:
    """跑一个 (模型, 前缀) 样本。单样本异常隔离：熔断/API 错误分桶，绝不中断整轮。"""
    state = env.base_state.copy()
    apply_change = lambda a: env.stats.apply_change(  # noqa: E731
        state, a["target"], a["stat"], a["delta"], a["reason"]
    ).message
    remember = lambda a: env.memory.add(  # noqa: E731
        state, a["target"], a["fact"], a.get("importance")
    )
    t0 = time.perf_counter()
    try:
        result = llm.run_turn(messages, apply_change, max_iters=max_iters, remember=remember)
    except LLMTurnError as e:
        return {"status": "meltdown", "error": str(e), "elapsed_s": round(time.perf_counter() - t0, 2)}
    except Exception as e:  # noqa: BLE001
        return {"status": "api_error", "error": f"{type(e).__name__}: {e}",
                "elapsed_s": round(time.perf_counter() - t0, 2)}
    rec = extract_metrics(result)
    rec["elapsed_s"] = round(time.perf_counter() - t0, 2)
    return rec


def extract_metrics(result) -> dict:
    """从 TurnResult 提取 A 轴信号（信号源全部来自 result.messages 与 iterations）。"""
    msgs = result.messages
    hints = [m for m in msgs if m.get("role") == "user"
             and str(m.get("content", "")).startswith("[引擎提示]")]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    bad = [str(m.get("content", "")) for m in tool_msgs
           if str(m.get("content", "")).startswith("[协议错误]")]
    rejected = [str(m.get("content", "")) for m in tool_msgs
                if str(m.get("content", "")).startswith("[引擎拒绝]")]
    truncated = any("finish_reason=length" in str(h.get("content", "")) for h in hints)

    # 脏文本：最终 submit_narration 的原始 narration 与 clean_narration 不一致
    raw = None
    for m in reversed(msgs):
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if tc.get("function", {}).get("name") == "submit_narration":
                    try:
                        raw = json.loads(tc["function"]["arguments"]).get("narration")
                    except (json.JSONDecodeError, KeyError):
                        raw = None
            if raw is not None:
                break
    dirty = bool(raw is not None and clean_narration(raw) != raw)

    return {
        "status": "ok",
        "iterations": result.iterations,
        "first_pass": result.iterations == 1,
        "hint_count": len(hints),
        "param_illegal": len(bad) + len(rejected),
        "bad_detail": [b.split("：")[-1][:80] for b in bad],
        "rejected_detail": [r.split("：")[-1][:80] for r in rejected],
        "truncated": truncated,
        "dirty": dirty,
        "narration": result.narration,
        "choices": result.choices,
        "plot_signal": result.plot_signal,
    }


def offline_baseline(history: list[dict]) -> dict:
    """原 run 时代的协议失败基线（附录 B.2）：直接在轨迹里数引擎提示/协议错误段。"""
    n_turns = len(turn_boundaries(history))
    n_hint = sum(1 for m in history if m.get("role") == "user"
                 and str(m.get("content", "")).startswith("[引擎提示]"))
    n_err = sum(1 for m in history if m.get("role") == "tool"
                and str(m.get("content", "")).startswith("[协议错误]"))
    n_rej = sum(1 for m in history if m.get("role") == "tool"
                and str(m.get("content", "")).startswith("[引擎拒绝]"))
    return {"turns": n_turns, "hints": n_hint, "protocol_errors": n_err, "rejections": n_rej}


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ICR 双模型对拍采样（local-14b Phase 0）")
    p.add_argument("--pack", action="append", default=[], help="世界包路径（与 --save 成对）")
    p.add_argument("--save", action="append", default=[], help="轨迹存档路径（与 --pack 成对）")
    p.add_argument("--per-pack", type=int, default=30)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--min-turn", type=int, default=2, help="跳过前 N 个回合边界（开场奇异样本）")
    p.add_argument("--sample-mode", choices=["even", "random"], default="even")
    p.add_argument("--truncate-tokens", type=int, default=None, help="前缀 token 上限（超限按压缩规则截断）")
    p.add_argument("--keep-turns", type=int, default=6, help="截断时保留的近窗回合数")
    p.add_argument("--with-status", action="store_true",
                   help="输入末尾追加生产同款状态栏（注意：存档为终局 state，早期前缀会泄漏未来事实）")
    p.add_argument("--max-iters", type=int, default=3)
    p.add_argument("--local-base-url", default=None)
    p.add_argument("--local-model", default=None)
    p.add_argument("--local-api-key", default=None)
    p.add_argument("--timeout-local", type=int, default=300)
    p.add_argument("--warmup", action="store_true", help="批跑前对本地端点发一次预热调用（vLLM lazy init）")
    p.add_argument("--flash-only", action="store_true", help="只跑 flash 侧（本地端点未就绪时）")
    p.add_argument("--dry-run", action="store_true", help="离线验证采样/截断/配对，零 API 调用")
    return p.parse_args(argv)


def resolve_sources(args) -> list[dict]:
    """(--pack, --save) 成对解析；缺省按 saves/smoke-<包>.json → world-packs/<包> 推断。"""
    if args.pack or args.save:
        if len(args.pack) != len(args.save):
            print("[✗] --pack 与 --save 必须成对出现")
            raise SystemExit(1)
        return [{"pack": args.pack[i], "save": args.save[i]} for i in range(len(args.pack))]
    sources = []
    for path in sorted(SAVE_DIR.glob("smoke-*.json")):
        name = path.stem[len("smoke-"):]
        if (Path("world-packs") / name).exists():
            sources.append({"pack": f"world-packs/{name}", "save": str(path)})
    if not sources:
        print("[✗] 未找到可推断的 (smoke-*.json, world-packs/<包>) 对，请用 --pack/--save 显式传入")
        raise SystemExit(1)
    return sources


def make_env(pack, base_state: GameState):
    """回放环境：ContextBuilder + StatsSystem + MemorySystem(dedup 关) + StorylineEngine。

    dedup 关闭：memory.add 的 bigram 命中不触发 dedup 侧信道（保 A 轴纯净性）。
    """
    stats = StatsSystem(pack.schedule)
    memory = MemorySystem(pack, llm=None)
    return SimpleNamespace(
        builder=ContextBuilder.from_pack(pack),
        stats=stats,
        memory=memory,
        story=StorylineEngine(pack, stats),
        base_state=base_state,
    )


def summarize_axis(records: list[dict]) -> dict:
    ok = [r for r in records if r["status"] == "ok"]
    n = len(records)
    melt = sum(1 for r in records if r["status"] == "meltdown")
    api_err = sum(1 for r in records if r["status"] == "api_error")
    first = sum(1 for r in ok if r["first_pass"])
    elapsed = sorted(r["elapsed_s"] for r in records)
    p95 = elapsed[min(int(len(elapsed) * 0.95), len(elapsed) - 1)] if elapsed else None
    return {
        "samples": n,
        "ok": len(ok),
        "meltdown": melt,
        "api_error": api_err,
        "first_pass": first,  # 分母 = ok 数（熔断不计入）
        "first_pass_rate": round(first / len(ok), 4) if ok else None,
        "param_illegal": sum(1 for r in ok if r["param_illegal"]),
        "dirty": sum(1 for r in ok if r["dirty"]),
        "truncated": sum(1 for r in ok if r["truncated"]),
        "iterations_mean": round(sum(r["iterations"] for r in ok) / len(ok), 3) if ok else None,
        "elapsed_mean_s": round(sum(r["elapsed_s"] for r in records) / n, 2) if n else None,
        "elapsed_p95_s": round(p95, 2) if p95 is not None else None,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sources = resolve_sources(args)
    rng = random.Random(args.seed)

    if not args.dry_run:
        env_file = Path(".env")
        if not env_file.exists():
            print("[✗] 缺少 .env（flash 侧配置）")
            return 1
        flash_settings = build_flash_settings(env_file)
        local_settings = None if args.flash_only else build_local_settings(args)
        tracker = UsageTracker(SAVE_DIR / "usage-icr.jsonl")

    # ---- 离线阶段：加载轨迹 → 按包分组分配配额 → 采样、截断 ----
    plan: list[dict] = []  # 每个元素 = 一个采样前缀
    per_pack_meta: dict[str, dict] = {}
    loaded: list[dict] = []
    for src in sources:
        pack = load_worldpack(src["pack"])
        hist = load_history(src["save"])
        name = pack.root.name
        if not hist:
            print(f"[!] {src['save']} 无 history 字段，跳过")
            continue
        base_state = None
        try:
            base_state = load_game(src["save"])
        except (ValueError, KeyError) as e:
            print(f"[!] {src['save']} 状态恢复失败（{e}），回退 from_pack")
            base_state = GameState.from_pack(pack)
        loaded.append({
            **src, "pack": pack, "hist": hist, "name": name,
            "save_stem": Path(src["save"]).stem, "base_state": base_state,
            "boundaries": turn_boundaries(hist), "quota": 0,
        })

    # 每包配额 = min(per_pack, 包内总边界数)，按源边界数最大余数法分配（跨源合并配额）
    groups: dict[str, list[dict]] = {}
    for item in loaded:
        groups.setdefault(item["name"], []).append(item)
    for name, items in groups.items():
        total_b = sum(len(it["boundaries"]) for it in items)
        quota = min(args.per_pack, total_b)
        if quota <= 0:
            continue
        raw = [quota * len(it["boundaries"]) / total_b for it in items]
        alloc = [int(r) for r in raw]
        order = sorted(range(len(items)), key=lambda i: raw[i] - alloc[i], reverse=True)
        for k in order[: quota - sum(alloc)]:
            alloc[k] += 1
        for it, n in zip(items, alloc):
            it["quota"] = max(0, min(n, len(it["boundaries"])))

    for it in loaded:
        name = it["name"]
        # 污染过滤：前缀含 [协议错误]/[引擎拒绝] tool 消息的回合跳过——坏 JSON tool_calls
        # 残迹会让 vLLM 模板解析历史消息时报 400（DeepSeek 端点宽容、本地端点不宽容），
        # 属于输入数据污染而非模型能力（阶段 5 实测发现）。
        err_idx = [i for i, m in enumerate(it["hist"]) if m.get("role") == "tool"
                   and str(m.get("content", "")).startswith(("[协议错误]", "[引擎拒绝]"))]
        clean_bounds = [bi for bi in it["boundaries"]
                        if not any(e < bi for e in err_idx)]
        n_dirty = len(it["boundaries"]) - len(clean_bounds)
        idxs = select_indices(len(clean_bounds), it["quota"], args.seed,
                              args.sample_mode, args.min_turn)
        idxs = [clean_bounds[i] for i in idxs]
        env = make_env(it["pack"], it["base_state"])
        n_trunc = 0
        for bi in idxs:  # bi 已是消息索引（见上方 clean_bounds 映射）
            prefix = it["hist"][: bi + 1]
            truncated = False
            if args.truncate_tokens:
                prefix, truncated = truncate_prefix(prefix, args.truncate_tokens, args.keep_turns)
            plan.append({
                "pack_name": name,
                "pack": it["pack"],
                "source": it["save_stem"],
                "turn_index": bi,
                "prefix": prefix,
                "tokens": history_tokens(prefix),
                "truncated": truncated,
                "env": env,
            })
            if truncated:
                n_trunc += 1
        meta = per_pack_meta.setdefault(name, {
            "saves": [], "turn_boundaries": 0, "sampled": 0,
            "truncated": 0, "offline_baseline": None,
        })
        meta["saves"].append(it["save"])
        meta["turn_boundaries"] += len(it["boundaries"])
        meta["sampled"] += len(idxs)
        meta["truncated"] += n_trunc
        if meta["offline_baseline"] is None:
            meta["offline_baseline"] = offline_baseline(it["hist"])
        print(f"[pack] {name}@{it['save_stem']}: 边界={len(it['boundaries'])} "
              f"污染剔除={n_dirty} 配额={it['quota']} 采样={len(idxs)} 截断={n_trunc}")

    if not plan:
        print("[✗] 无可采样前缀")
        return 1

    if args.dry_run:
        toks = sorted(p["tokens"] for p in plan)
        q = lambda xs, qq: xs[min(int(len(xs) * qq), len(xs) - 1)]  # noqa: E731
        print(f"\n[dry-run] 共 {len(plan)} 前缀：token 中位={q(toks, .5)} p75={q(toks, .75)} "
              f"p90={q(toks, .9)} max={q(toks, 1)}")
        print("[dry-run] 采样/截断/配对校验通过（未发任何 API 调用）")
        return 0

    # ---- 在线阶段：双模型对拍 ----
    if local_settings is not None and args.warmup:
        t0 = time.perf_counter()
        try:
            OpenAI(api_key=local_settings.api_key, base_url=local_settings.base_url,
                   max_retries=2, timeout=args.timeout_local).chat.completions.create(
                model=local_settings.model,
                messages=[{"role": "user", "content": "预热：回复 1"}],
                max_tokens=1,
            )
            print(f"[warmup] 本地端点预热完成（{time.perf_counter() - t0:.1f}s）")
        except Exception as e:  # noqa: BLE001
            print(f"[!] 本地端点预热失败：{type(e).__name__}: {e}（继续，样本层会分桶）")

    tools_cache: dict[str, list[dict]] = {}


    def clients_for(pack):
        if pack.root.name not in tools_cache:
            tools_cache[pack.root.name] = build_tools(pack.schedule)
        tools = tools_cache[pack.root.name]
        llm_f = LLMClient(
            OpenAI(api_key=flash_settings.api_key, base_url=flash_settings.base_url,
                   max_retries=5, timeout=180.0),
            flash_settings.model, tools,
            models={"judge": flash_settings.model_for("judge"),
                    "compress": flash_settings.model_for("compress")},
            tracker=tracker,
        )
        llm_l = None
        if local_settings is not None:
            llm_l = LLMClient(
                OpenAI(api_key=local_settings.api_key, base_url=local_settings.base_url,
                       max_retries=2, timeout=args.timeout_local),
                local_settings.model, tools, tracker=tracker,
            )
        return llm_f, llm_l

    samples: dict[tuple[str, int], dict] = {}
    pairs: list[dict] = []
    reveal: list[dict] = []
    for i, item in enumerate(plan, 1):
        env = item["env"]
        node = env.story.active_node(env.base_state)
        messages = env.builder.build_messages(env.base_state, item["prefix"], node)
        if not args.with_status:
            # 存档只有终局 state：状态栏会向早期前缀泄漏未来事实（Phase 0 默认关闭）
            messages = [messages[0], *messages[1:-1]]
        llm_f, llm_l = clients_for(item["pack"])
        key = (item["pack_name"], item["source"], item["turn_index"])
        rec_f = run_sample(llm_f, messages, env, args.max_iters)
        rec_l = run_sample(llm_l, messages, env, args.max_iters) if llm_l is not None else None
        samples[key] = {"flash": rec_f, "local": rec_l}
        f_iter = f"(i={rec_f['iterations']})" if rec_f["status"] == "ok" else ""
        l_iter = f"(i={rec_l['iterations']})" if rec_l and rec_l["status"] == "ok" else ""
        print(f"[{i}/{len(plan)}] {key[0]}@{key[1]}/turn-{key[2]} "
              f"flash={rec_f['status']}{f_iter} "
              f"local={rec_l['status'] if rec_l else 'skipped'}{l_iter} "
              f"toks={item['tokens']}{'(截断)' if item['truncated'] else ''}")
        if rec_l is not None and rec_f["status"] == "ok" and rec_l["status"] == "ok":
            a_is_flash = rng.random() < 0.5
            a, b = (rec_f, rec_l) if a_is_flash else (rec_l, rec_f)
            tokens = _forbidden_tokens(item["pack"].world)
            scan = lambda rec: [t for t in tokens if t in rec["narration"] or any(t in c for c in rec["choices"])]  # noqa: E731
            pairs.append({
                "pair_id": f"p{i:03d}",
                "source": f"{key[0]}@{key[1]}",
                "turn_index": key[2],
                "a": {"narration": a["narration"], "choices": a["choices"],
                      "plot_signal": a["plot_signal"], "iterations": a["iterations"]},
                "b": {"narration": b["narration"], "choices": b["choices"],
                      "plot_signal": b["plot_signal"], "iterations": b["iterations"]},
                "prefix": item["prefix"],
                "forbidden_scan": {"a": scan(a), "b": scan(b)},
            })
            reveal.append({"pair_id": f"p{i:03d}", "a": "flash" if a_is_flash else "local",
                           "b": "local" if a_is_flash else "flash"})

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    REPORTS_DIR.mkdir(exist_ok=True)
    SAVE_DIR.mkdir(exist_ok=True)
    all_f = [s["flash"] for s in samples.values()]
    all_l = [s["local"] for s in samples.values() if s["local"] is not None]
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": {
            "flash": {"model": flash_settings.model, "base_url": flash_settings.base_url},
            "local": ({"model": local_settings.model, "base_url": local_settings.base_url}
                      if local_settings else None),
            "per_pack": args.per_pack, "seed": args.seed, "max_iters": args.max_iters,
            "min_turn": args.min_turn, "sample_mode": args.sample_mode,
            "truncate_tokens": args.truncate_tokens, "keep_turns": args.keep_turns,
            "with_status": args.with_status,
        },
        "packs": per_pack_meta,
        "a_axis": {"flash": summarize_axis(all_f),
                   "local": summarize_axis(all_l) if all_l else None},
        "samples": [{"sample_id": f"{k[0]}@{k[1]}/turn-{k[2]}", **v} for k, v in samples.items()],
        "pairs": reveal,
        "usage_summary": tracker.summarize(),
    }
    out = REPORTS_DIR / f"icr-{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pool_out = SAVE_DIR / f"icr-pool-{ts}.json"
    pool_out.write_text(json.dumps({"pairs": pairs}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存 → {out}（{len(pairs)} 对盲评池 → {pool_out}）")
    print(tracker.cost_report())
    print("\nA 轴汇总：")
    for side, name in (("flash", "flash"), ("local", "local")):
        ax = report["a_axis"][side]
        if ax is None:
            continue
        print(f"  {name}: ok={ax['ok']}/{ax['samples']} 一次通过={ax['first_pass']}/{ax['ok']} "
              f"({ax['first_pass_rate']}) 熔断={ax['meltdown']} 参数非法={ax['param_illegal']} "
              f"脏文本={ax['dirty']} 截断={ax['truncated']} 平均迭代={ax['iterations_mean']} "
              f"均时={ax['elapsed_mean_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
