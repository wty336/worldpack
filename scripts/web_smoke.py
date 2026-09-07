"""F5 真机冒烟：驱动 Web API 完成「开局 → 抉择 → 发言 → 存档 → 读档」全流程（P3 验收）。

用法：uv run python scripts/web_smoke.py [--base http://127.0.0.1:8765]
"""

from __future__ import annotations

import argparse
import json

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'✓' if cond else '✗'}] {name}" + (f" · {detail}" if detail else ""))
        ok = ok and cond

    with httpx.Client(base_url=args.base, timeout=180.0) as client:
        # 1. 页面
        r = client.get("/")
        check("首页可访问", r.status_code == 200 and "江湖旧梦" in r.text)

        # 2. 开局（返回关键抉择）
        r = client.post("/api/new")
        d = r.json()
        sid = d["sid"]
        check("开局（新会话）", bool(sid) and d["view"]["choice_prompt"] is not None,
              f"sid={sid}")

        # 3. 关键抉择（SSE 流式，消费完整流）
        with client.stream("POST", f"/api/{sid}/turn", json={"kind": "pick", "index": 0}) as r:
            text = "".join(r.iter_text())
        check("关键抉择回合流式返回", "event: delta" in text and "event: done" in text,
              f"delta 事件 {text.count('event: delta')} 个")

        # 4. 自由发言（SSE：统计 delta 事件数 + done 视图）
        with client.stream("POST", f"/api/{sid}/turn",
                           json={"kind": "say", "text": "（微笑）沈姑娘，久仰才名。"}) as r:
            text = "".join(r.iter_text())
        delta_count = text.count("event: delta")
        done_idx = text.rfind("event: done")
        check("发言回合流式返回", delta_count > 5 and done_idx >= 0,
              f"delta 事件 {delta_count} 个")
        done_json = text[done_idx:].split("data: ", 1)[1].strip()
        view = json.loads(done_json)
        check("发言回合含叙事与选项", bool(view["narration"]) and len(view["choices"]) >= 3,
              f"选项 {len(view['choices'])} 个")

        # 5. 行动（日程）
        r = client.get(f"/api/{sid}/actions")
        acts = r.json()["actions"]
        check("行动列表可用", any(a["id"] == "cultivate" for a in acts),
              f"{len(acts)} 个行动")

        # 6. 存档 → 读档（A-1：仅接受 saves/ 内裸文件名）
        r = client.post(f"/api/{sid}/save", json={"path": "web-smoke.json"})
        check("存档", r.json().get("ok") is True)
        r = client.post(f"/api/{sid}/load", json={"path": "web-smoke.json"})
        check("读档", r.json().get("ok") is True and "status" in r.json())

    print("\n[✓] F5 冒烟通过" if ok else "\n[✗] F5 冒烟失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
