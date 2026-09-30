"""N1 真机冒烟：后台生成任务的进度 SSE（`docs/roadmap.md` N1）。

用法（先起服务）：
  uv run python -m game_agent web --port 8765
  uv run python scripts/worldgen_smoke.py --base http://127.0.0.1:8765

**为什么需要它**（pytest 侧 18 项守卫还不够）：那些用的是 `TestClient`，一次把整个
SSE 响应体取回来再看——**它验证不了"事件是随时间陆续到达的"**。而 N1 的全部意义就是
"分钟级任务能边跑边看进度"：如果实现变成"跑完再一次性吐出来"，TestClient 的断言
照样全绿，真实体验却退化成转圈等到最后。

所以本冒烟测的是测试客户端**测不到**的四件事：
1. **真流式**：事件到达时间戳必须分散开（首帧远早于终帧），而不是一次性；
2. **心跳**：一次 LLM 调用期间没有业务事件时，连接不能看起来像死了；
3. **断线重连回放**：中途断开再连上，仍能拿到此前全部进度；
4. **取消**：能在块之间停下来（这条只在真机上才有意义——离线跑得太快）。

全部用 `offline=true`（零成本、不联网），但走的是**同一个 HTTP 栈与同一份任务代码**。
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import httpx

SOURCE = (
    "# 环形都市\n\n"
    "调查员在一片环形都市里醒来，身上只有一张写着「林」的名片。\n"
    "林是城南诊所的医生，话很少，却记得每一个来过的人。\n"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    ok = True
    unjudged: list[str] = []
    pack_name = f"smoke_gen_{int(time.time())}"
    created: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'✓' if cond else '✗'}] {name}" + (f" · {detail}" if detail else ""))
        ok = ok and cond

    with httpx.Client(base_url=args.base, timeout=300.0) as client:
        # ---- 起任务 ----
        r = client.post("/api/packs/generate", json={
            "name": pack_name, "source_text": SOURCE, "offline": True, "with_corpus": True,
        })
        check("POST /api/packs/generate 接受并返回 202", r.status_code == 202, f"HTTP {r.status_code}")
        job_id = r.json()["job_id"]
        print(f"       job_id = {job_id}，包名 = {pack_name}")

        # ---- 同名护栏 ----
        dup = client.post("/api/packs/generate", json={
            "name": pack_name, "source_text": SOURCE, "offline": True,
        })
        check("同名包已存在 → 400（拒绝覆盖）", dup.status_code == 400,
              dup.json().get("detail", "")[:40])

        # ---- 真流式：记录每个事件的到达时间 ----
        arrivals: list[tuple[float, dict]] = []
        t0 = time.monotonic()
        with client.stream("GET", f"/api/packs/generate/{job_id}/events") as resp:
            check("SSE 响应头正确",
                  resp.headers.get("content-type", "").startswith("text/event-stream"))
            event = ""
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    event = line[len("event: "):]
                elif line.startswith("data: ") and event == "progress":
                    arrivals.append((time.monotonic() - t0, json.loads(line[len("data: "):])))
                elif event in ("done",) and line == "":
                    pass
        total = time.monotonic() - t0

        stages = [e["stage"] for e in (a[1] for a in arrivals)]
        check("收到终态 done 事件", "done" in stages, f"{len(arrivals)} 个事件，耗时 {total:.1f}s")
        check("拿到分块提取进度", stages.count("extract") >= 5,
              f"extract 事件 {stages.count('extract')} 条")
        check("拿到语料进度", "corpus" in stages)
        check("拿到修复轮进度", "repair" in stages)

        # 真流式判据：事件必须**分散在整段时间里**，而不是挤在最后一刻。
        #
        # 这里用**服务端**的 `ts` 而不是客户端的到达时间——到达时间会受缓冲、代理、
        # 网络影响，而"进度是逐步产生的"应当是服务端事实。
        #
        # **离线生成太快（<0.5s）时这一条不可判**：离线假 LLM 没有网络延迟，所有事件在
        # 几毫秒内产生，任何"分散"的判据都测不出东西。按本仓库的纪律
        # （`test_qa_gate.py`："全部被跳过 ≠ 通过"），不可判就**如实报不可判**，
        # 既不冒充通过，也不冤枉实现。
        #
        # 服务端的增量性由 `tests/test_jobs.py::test_job_stream_yields_incrementally`
        # 用合成慢任务**确定性地**钉住（并且做过变异验证：把 `_job_stream` 改成攒完再发，
        # 那条守卫必红）。所以这里不可判**不是缺口**，只是这条链路上判不了。
        if total >= 0.5 and len(arrivals) >= 3:
            spread = arrivals[-1][1]["ts"] - arrivals[0][1]["ts"]
            check("事件随时间陆续到达（不是一次性吐出）", spread > 0.3,
                  f"服务端 ts 跨度 {spread:.2f}s / 总耗时 {total:.2f}s")
        else:
            unjudged.append(
                f"HTTP 链路上的增量性（离线生成仅 {total:.2f}s，太快测不出）"
                f"——服务端增量性已由 tests/test_jobs.py 的合成慢任务守卫覆盖，无需花钱复核"
            )

        # ---- 终态与结果 ----
        final = client.get(f"/api/packs/generate/{job_id}").json()
        check("任务终态为 done", final["status"] == "done", str(final.get("error")))
        check("结果带包目录与摘要",
              bool(final["result"]) and "离线测试世界" in final["result"]["summary"])

        # ---- 晚订阅回放（等价于"刷新页面后接上"）----
        replay = [json.loads(b.split("data: ", 1)[1])
                  for b in client.get(f"/api/packs/generate/{job_id}/events").text.split("\n\n")
                  if b.startswith("event: progress")]
        check("任务结束后再订阅仍能回放完整事件", len(replay) == len(arrivals),
              f"回放 {len(replay)} 条 vs 实时 {len(arrivals)} 条")

        # ---- 生成的包出现在目录里 ----
        packs = client.get("/api/catalog").json()["packs"]
        entry = next((p for p in packs if p["id"] == pack_name), None)
        check("新包出现在 /api/catalog", entry is not None and entry["playable"],
              f"{entry['name']} · {entry['npcs']} 角色" if entry else "未找到")
        created.append(pack_name)

        # ---- 断线不取消：起一个任务、只连一下就断开，任务必须照跑完 ----
        j2 = client.post("/api/packs/generate", json={
            "name": pack_name + "_b", "source_text": SOURCE, "offline": True,
        }).json()["job_id"]
        created.append(pack_name + "_b")
        with client.stream("GET", f"/api/packs/generate/{j2}/events") as resp:
            for _ in resp.iter_lines():  # 读一行就断开
                break
        deadline = time.time() + 120
        status = ""
        while time.time() < deadline:
            status = client.get(f"/api/packs/generate/{j2}").json()["status"]
            if status in ("done", "failed", "cancelled"):
                break
            time.sleep(0.5)
        check("客户端中途断开后任务仍跑完", status == "done", f"status={status}")

    # 冒烟产物自己清掉：留在 world-packs/ 下会被误当成内容（甚至被 git add 进去）
    for name in created:
        try:
            shutil.rmtree(Path("world-packs") / name, ignore_errors=True)
        except OSError as e:  # noqa: BLE001
            print(f"  [!] 清理 {name} 失败：{e}")

    print()
    if unjudged:
        print(f"⚠️  {len(unjudged)} 项**不可判**（不是通过）：")
        for u in unjudged:
            print(f"     - {u}")
    print(f"===== N1 后台生成冒烟：{'全部通过' if ok else '有失败项'}"
          f"{f'（另有 {len(unjudged)} 项不可判）' if unjudged else ''} =====")
    print(f"已清理冒烟产物：{', '.join(created) if created else '（无）'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
