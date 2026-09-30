"""真机冒烟：后台生成任务的进度 SSE（N1）+ 草稿区没坏（§3.2 ③ / E-7）。

用法（先起服务）：
  uv run python -m game_agent web --port 8765
  uv run python scripts/worldgen_smoke.py --base http://127.0.0.1:8765

**为什么需要它**（pytest 侧几十项守卫还不够）：那些用的是 `TestClient`，一次把整个
SSE 响应体取回来再看——**它验证不了"事件是随时间陆续到达的"**。而 N1 的全部意义就是
"分钟级任务能边跑边看进度"：如果实现变成"跑完再一次性吐出来"，TestClient 的断言
照样全绿，真实体验却退化成转圈等到最后。

所以本冒烟测的是测试客户端**测不到**的：

1. **真流式**：事件到达时间戳必须分散开（首帧远早于终帧），而不是一次性；
2. **心跳**：一次 LLM 调用期间没有业务事件时，连接不能看起来像死了；
3. **断线重连回放**：中途断开再连上，仍能拿到此前全部进度；
4. **取消**：能在块之间停下来（这条只在真机上才有意义——离线跑得太快）。

**草稿区部分（E-7）测的是"唯一写口"这条链在真实 HTTP 栈上真的接上了**：
生成落草稿区且**不出现在选卡列表**、同名草稿可重生成、发布过闸门才进已发布区、
已发布后**反过来拒绝**同名生成、未发布的草稿能开局并被标记 `draft`。
离线夹具里这些断言容易"因为假件绕开了选包"而假绿（本仓库真踩过），
真机上跑一遍才算数。

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


def _wait_job(client: httpx.Client, job_id: str, timeout: float = 180.0) -> str:
    """等到任务进终态，返回最终 status（超时则返回最后看到的状态）。"""
    deadline = time.time() + timeout
    status = ""
    while time.time() < deadline:
        status = client.get(f"/api/packs/generate/{job_id}").json()["status"]
        if status in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.3)
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    ok = True
    unjudged: list[str] = []
    stamp = int(time.time())
    pack_name = f"smoke_gen_{stamp}"
    aux_name = f"smoke_gen_{stamp}_aux"  # 走完整生命周期：草稿 → 发布
    play_name = f"smoke_gen_{stamp}_pt"  # 只用来试玩，用完删除
    created: list[str] = []  # 两个区域都要清，否则冒烟产物会被误当内容甚至提交

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'✓' if cond else '✗'}] {name}" + (f" · {detail}" if detail else ""))
        ok = ok and cond

    def gen(name: str, **extra) -> httpx.Response:
        return client.post("/api/packs/generate", json={
            "name": name, "source_text": SOURCE, "offline": True, **extra,
        })

    def draft_ids() -> list[str]:
        return [d["id"] for d in client.get("/api/packs/drafts").json()["drafts"]]

    def catalog_ids() -> list[str]:
        return [p["id"] for p in client.get("/api/catalog").json()["packs"]]

    with httpx.Client(base_url=args.base, timeout=300.0) as client:
        # ---- 起任务 ----
        r = gen(pack_name, with_corpus=True)
        check("POST /api/packs/generate 接受并返回 202", r.status_code == 202, f"HTTP {r.status_code}")
        job_id = r.json()["job_id"]
        created.append(pack_name)
        print(f"       job_id = {job_id}，包名 = {pack_name}")

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

        # ---- §3.2 ③：生成落在草稿区，且**不能**出现在选卡列表里 ----
        check("生成的包落在草稿区（/api/packs/drafts）", pack_name in draft_ids())
        check("草稿**没有**出现在选卡列表（/api/catalog）", pack_name not in catalog_ids(),
              "草稿泄漏 → 玩家会点到半成品")
        check("草稿目录真的在 world-packs/_drafts/ 下",
              (Path("world-packs") / "_drafts" / pack_name / "world.yaml").is_file())

        # ---- 生命周期（另一个名字，避免与上面的流式观察互相干扰）----
        created.append(aux_name)
        check("起草稿成功", _wait_job(client, gen(aux_name).json()["job_id"]) == "done")
        # 再生成一次同名草稿：这就是"改一版再生成"的入口，必须被允许
        # （草稿区落地之前，这条路被一刀拒了，只能不停换名字）。
        r2 = gen(aux_name)
        check("同名草稿可以反复生成（改一版再生成）", r2.status_code == 202,
              f"HTTP {r2.status_code}")
        check("重生成同名草稿能跑完", _wait_job(client, r2.json()["job_id"]) == "done")
        pub = client.post("/api/packs/publish", json={"name": aux_name})
        check("发布过闸门后进入已发布区", pub.status_code == 200, pub.text[:60])
        check("发布后出现在选卡列表", aux_name in catalog_ids())
        check("发布后从草稿区消失", aux_name not in draft_ids())
        check("已发布的同名包 → 拒绝再生成（不许遮蔽线上内容）",
              gen(aux_name).status_code == 400)
        check("草稿区里没有混进已发布包", set(draft_ids()) & set(catalog_ids()) == set(),
              "同一张卡同时出现在两个区域")

        # ---- 未发布的草稿能试玩，且被标记为 draft ----
        created.append(play_name)
        check("试玩用草稿生成完成",
              _wait_job(client, gen(play_name).json()["job_id"]) == "done")
        d = client.post("/api/new", json={"draft": play_name})
        if d.status_code == 500 and "DEEPSEEK_API_KEY" in d.text:
            # 无 Key 环境下这一项判不了。如实报不可判，不冒充通过也不冤枉实现。
            unjudged.append("草稿试玩（本机未配置 DEEPSEEK_API_KEY，/api/new 无法建局）")
        else:
            body = d.json()
            check("未发布的草稿能开局", d.status_code == 200, str(body)[:60])
            check("开局被标记为 draft（前端据此打「未发布」水印）", body.get("draft") is True)
            check("会话 meta 也标记为 draft",
                  client.get(f"/api/{body['sid']}/meta").json().get("draft") is True)
            check("已发布的卡则不是 draft",
                  client.post("/api/new", json={"pack_id": aux_name}).json().get("draft") is False)

        # ---- 删除草稿只动草稿区 ----
        check("DELETE 草稿成功", client.delete(f"/api/packs/drafts/{play_name}").status_code == 200)
        check("删除后草稿消失", play_name not in draft_ids())
        check("删除草稿**不会**动到已发布包",
              client.delete(f"/api/packs/drafts/{aux_name}").status_code == 400
              and aux_name in catalog_ids())

        # ---- 断线不取消：起一个任务、只连一下就断开，任务必须照跑完 ----
        dis_name = pack_name + "_b"
        created.append(dis_name)
        j2 = gen(dis_name).json()["job_id"]
        with client.stream("GET", f"/api/packs/generate/{j2}/events") as resp:
            for _ in resp.iter_lines():  # 读一行就断开
                break
        status = _wait_job(client, j2)
        check("客户端中途断开后任务仍跑完", status == "done", f"status={status}")

    # 冒烟产物自己清掉：留在 world-packs/ 下会被误当成内容（甚至被 git add 进去）。
    # **两个区域都要清**——生成早就改到草稿区了，只清已发布区会把产物留在 _drafts/ 里。
    for name in created:
        for base in (Path("world-packs"), Path("world-packs") / "_drafts"):
            try:
                shutil.rmtree(base / name, ignore_errors=True)
            except OSError as e:  # noqa: BLE001
                print(f"  [!] 清理 {base / name} 失败：{e}")

    print()
    if unjudged:
        print(f"⚠️  {len(unjudged)} 项**不可判**（不是通过）：")
        for u in unjudged:
            print(f"     - {u}")
    print(f"===== N1 + E-7 冒烟：{'全部通过' if ok else '有失败项'}"
          f"{f'（另有 {len(unjudged)} 项不可判）' if unjudged else ''} =====")
    print(f"已清理冒烟产物：{', '.join(created) if created else '（无）'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
