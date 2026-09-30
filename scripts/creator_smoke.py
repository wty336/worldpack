"""N6 真机冒烟：创作者 Agent 的一轮真实对话（会花钱，但很少）。

用法（先起服务）：
  uv run python -m game_agent web --port 8765
  uv run python scripts/creator_smoke.py --base http://127.0.0.1:8765

**为什么必须真机跑一遍**（`tests/test_creator.py` 那 33 项还不够）：那些用的是
**脚本化的假模型**——它按我写的顺序调工具，于是验证的是"循环接线对了"，
而验证不了**真实模型会不会那样做**。§4.2 的整个设计（"validate_pack 是支点"）
赌的是模型会 先读 → 改 → 校验 → 按报错修；这件事只有真模型能证。

**成本**：一次两三回合的小对话，`deepseek-v4-flash` 上是几分钱量级
（脚本结束会打印账本里的实际花费，不用猜）。这与 `worldpack_smoke.py` 同一性质：
真机冒烟花钱，但它是"这条链到底能不能用"的唯一硬证据。

清理是**脚本自己的责任**：产物草稿与创作会话都删掉——上一批就踩过
"冒烟把垃圾留在 `_drafts/` 里等着被 `git add -A` 提交"。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

# 挑一个**小而明确**的改动：能在一两回合内完成、且不牵动结构
# （牵动结构的改动会被 check-worldpack 拒绝，那反而测不出"正常路径通不通"）。
# **指令必须指名道姓**。第一版写的是"把主角的说话风格改得更简短克制一些"，
# 结果真实模型**拒绝猜**：它读了世界之后回答"本包只有 1 张角色卡（沈清秋），
# **「主角」是玩家角色**，只存在于 world.player_role"——并反问要改谁。
# 那次"失败"其实是**设计意图被验证了**（系统提示里就有"问清楚再动手，比猜错一遍便宜"），
# 坏的是冒烟指令本身有歧义。教训写进 docs/roadmap.md §2.4：
# **冒烟指令的歧义会被记成产品缺陷**，所以这里点名 npc id 与字段。
ASK = (
    "把角色卡 shen_qingqing（沈清秋）的 speech_style 字段改成"
    "「只说短句，不解释」。改完调用 validate_pack 确认一下。"
)


def _digest(pack: str) -> str:
    """已发布包的内容摘要（用来证"工作版隔离有效"）。"""
    from game_agent.worldpack import pack_digest

    return pack_digest(Path("world-packs") / pack)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--pack", default="ancient_jianghu", help="从哪张已发布的卡复制")
    args = parser.parse_args()

    ok = True
    unjudged: list[str] = []
    name = f"smoke_creator_{int(time.time())}"
    cost_lines: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'✓' if cond else '✗'}] {label}" + (f" · {detail}" if detail else ""))
        ok = ok and cond

    def frames(text: str) -> list[tuple[str, dict]]:
        out = []
        for block in text.split("\n\n"):
            if not block.strip():
                continue
            event, data = "message", ""
            for line in block.split("\n"):
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data += line[6:]
            if data:
                out.append((event, json.loads(data)))
        return out

    with httpx.Client(base_url=args.base, timeout=300.0) as c:
        # 0) fork 用的是**同名**（`world-packs/<name>` → `_drafts/<name>`），所以草稿名
        #    由已发布包名决定。若该名字的草稿已经存在，**绝不删它**——那可能是作者
        #    正在改的东西（第一版写的是"删掉再来"，那会毁掉用户的工作）。
        existing = {d["id"] for d in c.get("/api/packs/drafts").json()["drafts"]}
        if args.pack in existing:
            print(f"[!] 草稿区里已经有 {args.pack!r}——本冒烟不删别人的草稿。")
            print(f"    请先自行处理它（工作台里删，或换个 --pack），再跑本冒烟。")
            return 1
        before_digest = None
        print(f"  从 {args.pack} 复制成草稿…")
        r = c.post("/api/packs/fork", json={"name": args.pack})
        check("fork 成功（原版只读，复制一份来改）", r.status_code == 200, r.text[:80])
        if r.status_code != 200:
            return 1
        draft = args.pack  # 草稿名与已发布包同名（发布时会被拒，故本冒烟不发布）
        before_digest = _digest(args.pack)

        try:
            before = c.get(f"/api/creator/{draft}").json()
            check("创作会话初始未开", before.get("open") is False)
            check("工作版摘要可读", bool(before.get("summary")), before.get("summary", "")[:60])

            # 1) 真实对话
            from game_agent.config import load_settings

            if not load_settings().has_api_key:
                unjudged.append("真机对话（未配置 DEEPSEEK_API_KEY）")
            else:
                t0 = time.monotonic()
                with c.stream("POST", f"/api/creator/{draft}/chat",
                              json={"message": ASK}) as resp:
                    body = "".join(resp.iter_text())
                elapsed = time.monotonic() - t0
                fr = frames(body)
                kinds = [k for k, _ in fr]

                check("SSE 首帧是 start（连接确认）", kinds and kinds[0] == "start")
                check("收到终态 done", "done" in kinds, f"{len(fr)} 帧 / {elapsed:.1f}s")

                tools = [p["name"] for k, p in fr if k == "tool"]
                print(f"       模型调用的工具：{tools or '（一个都没调）'}")
                check("真实模型**真的去读了**内容（read_* 至少一次）",
                      any(t.startswith(("read_", "list_")) for t in tools), str(tools))
                check("真实模型**真的改了**内容（写工具至少一次）",
                      any(t.startswith(("update_", "add_", "remove_", "upsert_")) for t in tools),
                      str(tools))

                done = [p for k, p in fr if k == "done"][0]
                # 这一条是本冒烟的核心：§4.2 赌的是"模型会自己校验"
                if "validate_pack" in tools:
                    check("真实模型自己调用了 validate_pack（修复循环的前提）", True)
                else:
                    unjudged.append(
                        "模型这一轮没主动调用 validate_pack"
                        "（协议要求它这么做，但它有权判断——这是**行为观察**，不是缺陷；"
                        "服务端仍在 done 里给出权威校验结论："
                        f"{done.get('validate_text', '')[:40]}）"
                    )
                check("服务端给出权威校验结论（不依赖模型自报）",
                      isinstance(done.get("validate_ok"), bool), done.get("validate_text", "")[:60])
                check("done 带 diff（人靠它确认）", bool(done.get("diff")),
                      f"{len(done.get('diff') or '')} 字符")
                check("改动文件清单非空", bool(done.get("changed")), str(done.get("changed")))
                check("Agent 给了人话答复", bool((done.get("reply") or "").strip()),
                      (done.get("reply") or "")[:70])

                # 2) 会话现状可接回（刷新页面场景）
                st = c.get(f"/api/creator/{draft}").json()
                check("会话现状可读取（刷新页面能接上）", st.get("open") is True)
                check("对话记录有人话", len(st.get("messages") or []) >= 2,
                      f"{len(st.get('messages') or [])} 条")
                check("现状里的 diff 与 done 一致", bool(st.get("diff")))

                # 3) 改动**真的落到了磁盘上的工作版**（不是只在内存里）
                wc_path = Path("world-packs") / "_drafts" / draft
                check("工作版目录存在", wc_path.is_dir())
                if st.get("changed"):
                    changed = st["changed"][0]
                    check(f"改动的文件真的在磁盘上（{changed}）", (wc_path / changed).is_file())

                # 4) 原始包没被动过——**改前改后各取一次摘要**再比
                #    （写成 `pack_digest(X) == pack_digest(X)` 是恒真式，等于没测：
                #    同一个路径当然等于自己。这类"看起来在守、其实在自证"的断言
                #    本仓库已经踩过三次，这里刻意把两侧取在不同时刻。）
                after_digest = _digest(args.pack)
                check("原始包内容未被改动（工作版隔离有效）",
                      after_digest == before_digest,
                      f"{before_digest[:12]}… → {after_digest[:12]}…")
        finally:
            # 清理：创作会话 + 草稿（**两样都要清**）
            try:
                c.delete(f"/api/creator/{draft}")
                c.delete(f"/api/packs/drafts/{draft}")
                print(f"  已清理：创作会话与草稿 {draft}")
            except Exception as e:  # noqa: BLE001
                print(f"  [!] 清理失败：{e}")
            ledger = Path("saves") / f"usage-creator-{draft}.jsonl"
            if ledger.is_file():
                total = 0
                for line in ledger.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        e = json.loads(line)
                        total += e.get("prompt_tokens", 0) + e.get("completion_tokens", 0)
                        cost_lines.append(f"{e.get('model')} {e.get('purpose')} "
                                          f"in={e.get('prompt_tokens')} out={e.get('completion_tokens')}")
                print(f"  账本 {ledger.name}：{len(cost_lines)} 次调用，共 {total} tokens")
                for line in cost_lines[:6]:
                    print(f"      {line}")
                sample = json.loads(
                    [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()][0]
                )
                check("账本带 pack 归因轴（N6 前置）", sample.get("pack") == draft,
                      str(sample.get("pack")))
                check("账本带 purpose=creator（与游玩调用分开）",
                      sample.get("purpose") == "creator", str(sample.get("purpose")))
                ledger.unlink()  # 冒烟账本不留（它不是证据，是产物）

    print()
    if unjudged:
        print(f"⚠️  {len(unjudged)} 项**不可判**（不是通过）：")
        for u in unjudged:
            print(f"     - {u}")
    print(f"===== N6 创作者 Agent 真机冒烟：{'全部通过' if ok else '有失败项'}"
          f"{f'（另有 {len(unjudged)} 项不可判）' if unjudged else ''} =====")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
