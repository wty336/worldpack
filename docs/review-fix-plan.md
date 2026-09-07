# 审查修复计划：P0~P3 四批次代码审查发现（v0.1）

> 来源：2026-09-07 对改进路线图四批次实施（71628f2..HEAD，51 文件 +5036 行）的完整代码审查
> （子代理全量审查 + 全部 Critical/Major 发现逐条人工代码级复核，复核结论：**全部属实**）。
>
> 验证基线：`uv run pytest` 实测 205 passed；协议配对 / KV Cache 前缀 / 存档 version 兼容 /
> 审计回放不变量均保持且有测试锁定。
>
> 用途：按 §2 分批执行；每项含「位置 → 问题 → 修法 → 验收」，可独立拆出实施。
> 执行后按惯例回写本文档与相关报告（最小 diff + 可回滚）。

---

## 1. 发现总览

| # | 级别 | 问题 | 位置 | 批次 |
| --- | --- | --- | --- | --- |
| C1 | Critical | Web 存读档端点任意路径读写 | web.py:172-188 | A |
| M5 | Major | 检索/提取上下文被引擎元消息污染 | context.py:30-33 | A |
| M1 | Major | SSE error 帧未 JSON 编码，前端必崩 | web.py:145-163 | B |
| M2 | Major | /turn 未捕获 ScheduleError，流中途断裂 | web.py:150-163 | B |
| M3 | Major | SSE 伪流式：LLM 跑完才一次性送达 | web.py:139-169 | B |
| M4 | Major | 会话共享状态无并发保护 | web.py:38,53,56,137 | B |
| m5 | Minor | UsageTracker 多实例写同一 JSONL 无锁 | usage.py:75-81 | B |
| M6 | Major | judge_corpus.py 违反分层原则（在引擎层含内容文案） | judge_corpus.py:24-27 等 | C |
| M7 | Major | scaffold 的 name 无校验，路径穿越 | scaffold.py:151-160 | C |
| m1 | Minor | InsightEntry.sources JSON 回环后 tuple→list | state.py:174-177 | C |
| m2 | Minor | 事件路径 rng 不可复现（与 P2 报告声明不符） | events.py:72 | C |
| m3 | Minor | 反思门控计数不入存档，读档后重复反思 | game.py:103,293-300 | C |
| m4 | Minor | 越界语义三处口径不一致 | stats.py:5-8 / schedule.yaml / design.md | C |
| D1 | Docs | P1 报告内部 188/189 不一致 | p1-report.md §1 vs §6 | C |

**批次划分**：
- **批次 A（安全 + 机制纯度，立即）**：C1、M5
- **批次 B（Web 层一揽子）**：M1、M2、M3、M4、m5
- **批次 C（分层与卫生，顺手）**：M6、M7、m1、m2、m3、m4、D1

---

## 2. 批次 A：安全与机制纯度

### A-1（C1）Web 存读档端点路径穿越 ✅ 已修复（2026-09-07）

- **位置**：`game_agent/web.py:172-188`（`api_save` / `api_load`），`SaveRequest` 在 L94-95。
- **问题**：`req.path` 客户端完全可控，直传 `save_game` / `load_game`，无任何校验。
  `POST /api/{sid}/save {"path": "../../.env"}` 可将游戏状态 JSON 覆写至项目内任意文件
  （世界包 yaml、`.env`、`pyproject.toml`）；`/load` 可读取任意路径（错误信息经
  HTTPException 回显，附带路径探测能力）。当前硬编码 `host="127.0.0.1"`（cli.py:337）
  只是部署缓解——同机任意进程可调用，改绑 0.0.0.0 即完全暴露。
- **修法**（只取文件名 + 根目录约束 + 后缀白名单）：
  ```python
  SAVE_ROOT = Path("saves").resolve()

  def _safe_save_path(raw: str) -> Path:
      p = (SAVE_ROOT / Path(raw).name).resolve()  # .name 剥掉一切目录成分
      if not p.is_relative_to(SAVE_ROOT):          # 防御纵深
          raise HTTPException(400, "非法存档路径")
      if p.suffix != ".json":
          raise HTTPException(400, "仅允许 .json 存档文件名")
      return p
  ```
  两端点统一改用 `_safe_save_path(req.path)`。
- **验收**：tests/test_web.py 新增用例——`../../.env`、绝对路径、子目录路径、非 .json
  后缀均 400；正常文件名存读回环通过。
- **工作量**：小（半小时级）。
- **✅ 实施记录**：采用**严格拒绝**变体（raw 必须等于 `.name` 且匹配 `[\w\-.]{1,128}\.json`，
  否则 400）——计划代码的「剥除法」会把 `sub/dir/x.json` 静默映射为合法 `x.json`（200），
  与验收「子目录路径 400」冲突；严格拒绝更可预测。前端与 web_smoke 的默认路径同步改裸
  文件名（`web.json` / `web-smoke.json`）。新增 `test_save_path_traversal_rejected`
  （7 种坏路径 save/load 双端 400 + 函数级拒绝/放行断言）。209 离线全绿。

### A-2（M5）检索/提取上下文纯度

- **位置**：`game_agent/context.py:30-33`（`_recent_player_text`）；连带
  `game.py` `_recent_text`（事实提取素材，只跳过 `【` 前缀）。
- **问题**：`_recent_player_text` 取 history 最后 2 条 `role=="user"` 消息。但每轮
  `build_messages` 追加的状态栏快照（含场景卡/属性/记忆/上轮 lore 全文）以 user 角色
  进入 history（run_turn 返回后只滤 system），【反重复提示】【校验反馈】【主线节点】
  等元消息同理。后果链：
  1. A1 的 relevance 上下文被状态栏淹没——上轮注入的记忆/lore 文本恰在状态栏里，
     **自我强化命中**，检索退化为「越注入越命中」；
  2. B1 的 lore「按需注入」同理退化为「常驻注入」；
  3. 提取器素材混入状态栏，提炼质量下降。
  另注：P1 报告「召回@40 = 19/19」的成绩更多靠常驻区 + recency 支撑，relevance
  因子的真实有效性在此缺陷下未被真正测到。
- **修法**（推荐两步）：
  1. **打标**：引擎生成的 user 消息（状态栏、`【…】`元消息）统一加标准 `name` 字段
     （如 `"name": "engine"`）——OpenAI 消息合法字段，不破坏 API；
  2. **过滤**：`_recent_player_text` 与 `_recent_text` 只取无 `name` 标记的 user 消息
     （真实玩家输入：say 文本 / act 提示 / pick 选择）。
- **设计决策点（需在实施时拍板并回写 design.md）**：状态栏快照是否该留在 history？
  - 留：压缩摘要能看到状态变化轨迹（现状）；
  2. 不留：省 token、检索纯净，但压缩素材少一维信息。
  本计划默认维持「留 + 打标」，改动面最小。
- **✅ 实施记录**：拍板「留 + 打标」（design.md §4.5.1）。打标范围 = 状态栏快照/剧情摘要/
  【主线节点】/【事件】/【节点完成】/推进提示/命运事件/【反重复提示】/【校验反馈】/
  协议重试提示；玩家动作（say/act/pick/开场）不打标。`_recent_player_text` 与
  `_recent_text` 只取无标记消息（后者保留【前缀兜底）。新增离线用例 ×3
  （test_context ×2 / test_game ×1）。真机回归：**记录事实召回@40 = 14/14（100%）**
  维持；轨道事实 3/3（含测量协议「重读材料」加固——事实在材料中已实锤，噪声在答题侧）；
  完整对局 34 次 turn 无协议错误，`name` 字段被 DeepSeek 接受。
  **注意**：压缩摘要素材（history_text）仍含状态栏快照——这是「留」的意图
  （压缩看状态轨迹），不属于污染面。
- **验收**：
  - tests/test_context.py 新增：构造「状态栏 + 元消息 + 真实发言」混合历史，断言
    检索上下文只含真实发言；
  - tests/test_game.py 新增：提取器素材不含状态栏文本；
  - 真机回归：重跑 memory_regression（检查点 40 召回必须维持 100%）——
    这是本批唯一影响真机行为的改动，**保留集必须重跑**（见 §5）。
- **工作量**：中（半天级，含真机回归）。

---

## 3. 批次 B：Web 层一揽子

> 五项发现同属 web.py/usage.py 的 Web 层，一次改完一次测。批次 A 完成后进行。

### B-1（M3）SSE 真流式

- **位置**：`game_agent/web.py:139-169`（`api_turn.gen`）。
- **问题**：同步生成器中 `game.say()`（数十秒 LLM 调用）在第一个 yield **之前**跑完，
  on_text 只堆积队列，之后一次性 drain——客户端生成期间收不到任何字节。
  **P3 报告「617 个流式增量」只证明了最终字节序列，时序流式未达成**，报告 §4 需修正。
- **修法**（LLM 调用挪后台线程，生成器阻塞消费队列）：
  ```python
  def gen():
      def work():
          try:
              view_holder["view"] = dispatch()   # start/say/pick/act 分发
          except (GameError, StorylineError, ScheduleError, LLMTurnError) as e:
              deltas.put(("error", str(e)))
          finally:
              deltas.put(None)                    # 结束哨兵
      threading.Thread(target=work, daemon=True).start()
      while (item := deltas.get()) is not None:
          yield _sse(item[0], json.dumps(item[1], ensure_ascii=False))
      if "view" in view_holder:
          yield _sse("done", json.dumps(_view(game, view_holder["view"]), ensure_ascii=False))
  ```
- **验收**：tests/test_web.py 新增时序断言——首个 delta 到达时 LLM 调用尚未结束
  （用可控 fake：on_text 先发一段、阻塞、断言客户端已收到、再放行）；真机 web_smoke
  复跑，观察增量是否边生成边到达。
- **工作量**：中。

### B-2（M1）SSE error 帧 JSON 编码

- **位置**：`game_agent/web.py:145,153,156,159,162`。
- **问题**：delta/done 帧均 `json.dumps`，五条 error 分支裸文本；前端 L282 对 error 也
  `JSON.parse` → SyntaxError 中断整个读取循环（玩家看到故事区清空且无错误提示）；
  裸文本含 `\n\n` 还会撕裂 SSE 帧。现有测试断言的恰是裸文本格式——缺陷被测试固化。
- **修法**：error 帧统一 `_sse("error", json.dumps(str(e), ensure_ascii=False))`；
  test_web.py 改为断言 `json.loads(events[0][1])` 可解析且含错误信息。
- **验收**：前端在 error 帧后流读取循环不中断；新增含换行错误消息的帧完整性用例。
- **工作量**：小。注意与 B-1 合并实施（同改 gen 的组装处）。

### B-3（M2）/turn 补捕 ScheduleError

- **位置**：`game_agent/web.py:150-163`。
- **问题**：`game.act(req.action_id or "")` 在未知 action_id / 行动点不足 / requires
  不满足时抛 ScheduleError，不在 except 链内，StreamingResponse 中途断裂。
  UI 过滤不可用行动只能防点击，防不了竞态（行动点被并发请求扣掉）与直接 API 调用。
- **修法**：except 链加入 `ScheduleError`（与 GameError 同级，转 error 帧）。
  合并进 B-1 的 except 元组。
- **验收**：tests/test_web.py 新增——未知 action_id 的 act 请求收到 error 帧而非断流。
- **工作量**：小。

### B-4（M4）会话并发保护

- **位置**：`game_agent/web.py:38`（SESSIONS）、L53（每会话各自 UsageTracker 写同一
  JSONL）、L56（共享 autosave 路径）、L137（每请求覆写 game.on_text）。
- **问题**：FastAPI def 端点跑线程池，同 sid 并发回合互踩 on_text 回调，state/history
  无锁——tool_calls/tool 配对消息可能被交错撕开（协议不变量风险）；任一会话节点完成
  即覆盖其他会话的 `saves/autosave.json`。
- **修法**：
  1. 每会话一把 `threading.Lock`，/turn 全程持锁（同 sid 回合串行化）；
  2. autosave 路径带 sid：`f"saves/autosave-{sid}.json"`；
  3. tracker 改共享单实例（配合 B-5 的锁）。
- **验收**：tests/test_web.py 新增并发用例——同 sid 两个并发 turn 请求，断言串行执行、
  history 配对不变量保持（复用 ensure_pairing）；多会话 autosave 互不覆盖。
- **工作量**：中。

### B-5（m5）UsageTracker 写文件加锁

- **位置**：`game_agent/usage.py:75-81`。
- **修法**：模块级 `threading.Lock` 包住 `_append`（Windows 小写入通常安全，但无正式
  保证，行交错即 JSONL 损坏）。
- **验收**：tests/test_usage.py 新增多线程写一致性用例（N 线程 × M 条，逐行可解析）。
- **工作量**：小。

---

## 4. 批次 C：分层与卫生

### C-1（M6）judge_corpus.py 迁出引擎层

- **位置**：`game_agent/judge_corpus.py` 全文（412 行）。
- **问题**：P0 刚修完 F1「引擎层不得含内容文案」，同批却在引擎层新增通篇
  ancient_jianghu 专属内容的模块（L24-26：`DEFAULT_SCENE = "长安城·东市"`、
  `SHEN = "shen_qingqiu"` 等），且 `pyproject.toml` 的 `packages = ["game_agent"]`
  会把它打进 wheel 分发。
- **修法**：语料数据迁移（二选一，推荐前者）：
  1. `world-packs/ancient_jianghu/judge_corpus.yaml`（数据即内容，与世界包同生命周期），
     引擎层只留中立的 `JudgeCase` schema + loader；
  2. `tests/data/judge_corpus.py`（若定性为纯测试资产）。
  scripts/judge_sensitivity.py 的 import 同步改。
- **验收**：`uv run python -m game_agent check-worldpack` 与门禁脚本正常运行；
  `game_agent/` 包内 grep 不到「长安」「shen_qingqiu」。
- **工作量**：小。

### C-2（M7）scaffold 的 name 白名单

- **位置**：`game_agent/scaffold.py:151-160`（`target = Path(root) / name`）。
- **修法**：
  ```python
  import re
  if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
      raise ValueError(f"非法世界包名（仅允许字母/数字/_/-）: {name!r}")
  ```
- **验收**：tests/test_scaffold.py 新增——`../x`、`C:/x`、含空格名均拒绝。
- **工作量**：小。

### C-3（m1）InsightEntry.sources 反序列化归一化

- **位置**：`game_agent/state.py:174-177`。
- **修法**：`from_dict` 中 `sources=tuple(i.get("sources", ()))`；
  `test_a3_save_roundtrip_with_insights` 改为经真实 `json.dumps/loads` 回环。
- **验收**：JSON 文件级回环断言 sources 类型一致。

### C-4（m2）事件路径 rng 同源

- **位置**：`game_agent/events.py:72`。
- **问题**：`apply_effects(state, event.effects)` 未传 `self.rng`（schedule.py:114 传了），
  收益曲线效果（spread/decay）在事件路径回退系统熵，与 P2 报告「rng 同源可复现（R4）」
  的声明不符。当前包事件效果均为固定数字，无实害，属机制缺口。
- **修法**：`apply_effects(state, event.effects, rng=self.rng)`。
  **注意**：传入 rng 后行为变化仅限含 spread/decay 的事件效果，当前世界包无此类事件，
  保留集不受影响（无需重跑真机）；但需在报告中如实说明。
- **验收**：tests/test_events.py 新增——含 spread 效果的事件在同 seed 下两次触发结果一致。

### C-5（m3）反思门控状态入存档

- **位置**：`game_agent/game.py:103`（`_last_reflect_counts`）、L293-300（门控逻辑）。
- **修法**（二选一，推荐后者）：
  1. 计数放入 GameState（随存档）；
  2. 判定改为可从存档重建的条件（如「该 NPC 记忆中最大 round > 最近一次洞察 round」）。
- **验收**：存档→读档后不触发重复反思；离线测试覆盖。

### C-6（m4）越界语义口径统一

- **位置**：`game_agent/stats.py:5-8`（docstring 称「普通数字越界抛错」）、
  `world-packs/ancient_jianghu/schedule.yaml` 注释（「运行期报错」）、
  `docs/design.md` §5.3.3（统一饱和）。
- **修法**：三处统一为 design.md 口径——世界包效果路径一律饱和（先截断 delta 再算
  after），LLM 提议路径（change_stat）保持严格拒绝。
- **验收**：纯文档改动，无测试影响。

### C-7（D1）P1 报告笔误

- **位置**：`docs/p1-report.md` §1（"188 passed"）vs §6 / README（"189 passed"）。
- **修法**：§1 改 189；顺带复核 README「当前状态」区四条批次记录的数字一致性
  （146/159/189/205）。

---

## 5. 贯穿要求（沿用路线图 §9 与复盘预防规则）

| 批次 | 离线测试 | 保留集（真机） |
| --- | --- | --- |
| A | 全绿 + 新增用例 ✅（209 passed） | ✅ 已完成：轨道事实 3/3（重读协议加固后）+ memory_regression 召回@40（见 A-2 实施记录）；注入/通关可复用最近结果 |
| B | 全绿 + 新增用例 | web_smoke 复跑（7 项）；引擎主线无改动，保留集可复用 |
| C | 全绿 + 新增用例 | C-4 如上文注明无需重跑；其余纯卫生项 |

其他纪律：
- 每批一个 commit，信息含「审查修复 + 编号」（对应 git 历史风格）；
- 涉及 LLM 输出解析的改动先做标点/空白归一化（M2b #3）；
- 涉及 history 结构的改动（A-2 打标）必须补布局级不变量测试（M2b #1/#9）；
- 修完后回写本文档各项状态，并修正 P3 报告 §4 的流式验收表述（M3/B-1）。

---

## 6. 与既有文档的呼应

| 本文档条目 | 呼应 |
| --- | --- |
| A-1 / B 系列 | p3-report.md §4（F5 验收声明需随 B-1 修正） |
| A-2 | improvement-roadmap.md §3 A1/§4 B1（检索纯度的实施缺陷）；M2a 复盘「常驻可见才存活」 |
| B-1 | M2b 复盘 D3（流式 UX 的设计前提） |
| C-1 | improvement-roadmap.md §8 F1；design.md §2.1 分层原则 |
| C-4 | p2-report.md §2（rng 同源声明）；design.md R4 |
| 贯穿要求 | plan-m2.md §9；m2b-postmortem.md 预防规则 9-15 |

---

*审查修复计划 v0.1。实施中与本文档冲突时以实测为准，回写本文档。*
