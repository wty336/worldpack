# 做成 dsh-tavern 那种效果：在自己的 harness 上的实现方案

> **目标**（照抄你的原话）：
> "选一张卡自由游玩，或绑定小说、剧本和大纲，让故事沿主线推进。也可以与 Agent 对话，从素材制作新卡，修改人物设定和世界书。"
>
> **约束**：不做 DSH 插件；保留自己的 agent harness；harness 可以改。
>
> 本文只回答"怎么做"。对标分析与"为什么不移植到 DSH"见 `docs/plan-dsh-tavern-parity.md`；
> 平台化的既有设计见 `docs/plan-creator-player.md`（本文与它同向，并把它与 dsh-tavern 的实际形态对齐）。

---

## 0. 结论：这三条能力 = 两个 Agent + 一个目录层

这是最重要的一句话，它决定了整个工作量分布：

| 你想要的 | 本质 | 你的引擎现状 |
| --- | --- | --- |
| 选一张卡自由游玩 / 沿主线推进 | **玩家 Agent**（你已有） + **目录层**（缺） | 引擎 90% 已有，缺产品外壳 |
| 绑定小说、剧本、大纲 | **生成管线**（你已有，但锁在 CLI 脚本里） | 逻辑 100% 已有，缺"服务化 + 进度" |
| 与 Agent 对话制卡 / 改设定 | **创作者 Agent**（**需要新写，但很小**） | 需要新写 |

**换句话说：你不需要新写一个游戏引擎，你需要一个产品层 + 一个"作者助手"Agent。**
难度最高、最贵、最容易被低估的是**前端**——所以第 5 节花最多篇幅。

---

## 1. 逐条对照：dsh-tavern 的说法 → 你的引擎对应物

| dsh-tavern 的说法 | 它在内部怎么实现 | 你已有的对应物 | 判定 |
| --- | --- | --- | --- |
| 选一张**卡** | 人物卡库（PNG/JSON），一个 Agent 一次一张卡 | `world-packs/*`（一个包 = 世界 + NPC + 主线 + 日程） | ✅ **你的包是卡的超集**，直接把包当卡即可 |
| **自由游玩** | 无 Script 绑定，模型自由推进 | `mainline.nodes: []` 的包 | ✅ **已经能跑**（`world-packs/baseline_probe/mainline.yaml` 就是 `nodes: []`；`storyline.pending_choice` 在 `node is None` 时返回 `None`，不会卡住） |
| 绑定**小说、剧本、大纲** | Script 资源 + 按 chunk 召回参考片段 | `scripts/import_story.py`（851 行）：素材 → 8 块原子化提取 → 校验-修复循环（≤4 轮）→ 生成 world/npcs/mainline/events/endings + 语料 | ✅ **管线已存在**，生成的就是 `mainline.yaml` 节点 |
| 让故事**沿主线推进** | 剧本进度 + 每轮召回参考片段 | `storyline.begin_turn` → 节点触发 → 关键抉择（`_try_enter_node` / `pending_choice` / `_advance_plan`） | ✅ **已经是最强的一块** |
| 从素材**制作新卡** | 卡片工作台会话 + 卡片工具 | `import_story.py` 的产物 + `scaffold.init_worldpack` 骨架 | ⚠️ 逻辑在，**锁在 CLI 脚本里**，且不是对话式 |
| 修改**人物设定** | `tavern_update_card` 等工具 + 工作版/原版分离 | `npcs/*.yaml` 字段（identity/personality/speech_style/secrets/…） | ❌ 无工具面 |
| 修改**世界书** | `tavern_update_worldbook` / `tavern_read_worldbook` | `world.yaml` 的 `lore: [{id, keys, text, …}]` | ❌ 无工具面 |
| **校验闸门** | 卡片校验 + 冲突报告 | `check_worldpack`：schema + 交叉引用 + 可达性 + 数值上限（97 个 raise） | ✅ **比它强**，直接当写入门禁 |

**读法**：右边 ✅ 的行说明你最大的资产是现成的；❌ 的行只有两处，而且都是"加一个工具面"，不是新机制。

---

## 2. 能力 A：选一张卡自由游玩

### 2.1 引擎侧（改动很小）

**问题**：当前是"一个进程 = 一个包"。`web.py:52` 的 `_pack_path()` 读环境变量
`GAME_WORLDPACK`，`_make_game(sid)` 无参数地取它 —— **换包要重启进程**。

```python
# game_agent/web.py:46-52 现状
def _pack_path() -> str:
    return os.environ.get("GAME_WORLDPACK", DEFAULT_PACK)
```

**改法**（`plan-creator-player.md` §1 已拍板同一方向）：

1. 新增 `game_agent/catalog.py`：扫描 `world-packs/*`，对每个包调 `load_worldpack()`，
   返回**卡片元数据**（`world.name` / `era` / `opening` / NPC 数与名单 / 节点数 / 结局数 / digest）。
   `load_worldpack` 本来就在开局时全量载入内存、之后不碰磁盘（`judge_corpus` 是唯一例外），
   所以目录扫描是干净的。
2. `_make_game(sid, pack_id)`：会话创建时按 `pack_id` 取包并绑定到 `Session`。
3. `POST /api/new` 接受 `{pack_id}`；新增 `GET /api/catalog` 返回卡片列表。
4. 环境变量降级为"默认包"（保持 CLI 与既有脚本不破）。

**存档必须带剧本身份**（否则改包后读旧档会静默穿帮）：`save.py:58-63` 的 `save_game()`
只写 `state.to_dict()` + `save_version`，**不含 `pack_id`**。
加 `pack_id` / `pack_digest`（复用 `evalmeta.file_digest`，已做换行归一化、跨机可对账），
读档不一致 → **拒绝并提示**，不静默降级。这是 `plan-creator-player.md` 的 G2。

### 2.2 "自由游玩 vs 剧本模式"

你的引擎已经有天然分界，不需要新机制：

- **剧本模式** = 包里有 `mainline.nodes` → 当前行为（节点触发 + 关键抉择 + `plan` 指针）
- **自由游玩** = 没有节点，或会话开关关掉节点进入

对已有节点的包想"自由游玩"，只加一个会话级布尔量，在 `Game` 里传给
`storyline.begin_turn` / `_try_enter_node`：为假时直接跳过节点进入与关键抉择。
**这是十几行的改动，不要动 `storyline.py` 的状态机本身。**

> 建议在前端把它做成每局开局的一个选择（"跟着主线走 / 自由探索"），
> 而不是包的一个属性——同一个包两种玩法是有价值的。

### 2.3 "卡"是什么：建议用包当卡

dsh-tavern 的"卡"= 一个角色；你的包 = 世界 + N 个角色 + 主线 + 日程。
**直接把包当卡**：零新格式、零转换、`load_worldpack` 已经产出富元数据。

如果以后想消费酒馆生态的人物卡作为**素材来源**，见 §9——但那是"导入内容"，不是"运行逻辑"。

---

## 3. 能力 B：绑定小说 / 剧本 / 大纲，沿主线推进

### 3.1 好消息：管线已经写完了

`scripts/import_story.py`（851 行）已经做了最难的部分：

- 素材读取（txt/md/目录，超长自动截断到 30 万字）
- **8 块原子化提取**：`EXTRACT_WORLD_SYSTEM` / `_EXTRA` / `NPC` / `SCHEDULE` / `ACTION` / **`NODE`（主线节点！）** / `EVENTS` / `ENDINGS`
- 校验-修复循环（`--rounds`，默认 4 轮，`_repair_sections` 按报错段重生成）
- 物化六个文件（`_materialize`）、调 `check_worldpack` 校验（`_validate`）、
  可选生成 30 条 Judge 语料（`--with-corpus`）、真机门禁（`--live`）
- `--draft-only` 输出草稿 JSON、`--offline` 内嵌假 LLM 做回归

它的 `EXTRACT_NODE_SYSTEM` 明确要求"3~5 个节点、关键抉择 1~3 个、每个选项效果写入 completion 旗标"
——**这正是"让故事沿主线推进"的实现**。

### 3.2 要做的事：从 CLI 脚本变成服务

三件事，都不是算法工作：

1. **提取成引擎侧模块** `game_agent/worldgen.py`（`plan-creator-player.md` 的 G3）。
   现状是 945 行单文件里混着提取逻辑、物化、修复循环、print 和 subprocess —— CLI 锁死了创作者模式。
   拆成纯函数 + 一个薄的 CLI 包装，两边复用。
2. **后台任务 + 进度流**。这个管线要跑几分钟（`--rounds 4` × 8 块提取）。
   不能在 HTTP 请求里同步跑。需要一个极简后台任务表：
   `POST /api/worldgen` → 返回 `job_id`；`GET /api/worldgen/{job_id}/events` → SSE 推进度。
   你已经有一模一样的 SSE 模式（`web.py` 的 `_turn_stream` + `queue.Queue` + 线程），**照抄即可**。
3. **草稿区 ↔ 已发布区**。生成的包先进 `world-packs/_drafts/<name>/`，
   过 `check_worldpack` 才允许"发布"到 `world-packs/<name>/`。
   这是 `plan-creator-player.md` 的"唯一写口"：**Web 界面不直写文件系统**。

### 3.3 与 dsh-tavern 的差异（这是你的优势）

dsh-tavern 的剧本模式是"从 Script 按需召回参考片段"——它**没有主线状态机**，
剧本进度只是个游标。你有 `mainline.yaml` 的节点 + `completion` 表达式 + 关键抉择 + `plan` 指针，
而且 `check_worldpack` 会在加载期就拒绝"**该节点将永远无法完成**"（flag 没人写）
和"**结局数值不可达**"（曾一次抓出 8 个包中 4 个的不可达结局）。

**所以"沿主线推进"这件事你本来就做得比它实。** 前端只要把"剧本进度 / 本轮节点目标"显示出来，
就是它那个"剧本模式"效果的加强版。

---

## 4. 能力 C：与 Agent 对话制作新卡、改人物设定和世界书

### 4.1 关键设计判断：**另起一个小 Agent，不要塞进 `game.py`**

你现有的 `llm.run_turn` + `ToolRegistry` 是为**叙事回合**优化的（流式、`submit_narration` 收尾、
截断整批作废、内轮自校正）。创作者任务是**结构化 CRUD**，目标完全不同。

**建议新增一条独立的轻量循环**，复用同一套 `ToolSpec` / `dispatch` 机制（`registry.py` 的
`ToolSpec` 已经是声明式的：schema / rejects / tag / validator），但：

- 不同的 system prompt（"你是世界包编辑器"）
- 不同的工具集（下面那张表）
- 不需要流式正文，不需要 Judge，不需要 factcheck
- 走已有的**按用途分层的模型路由**（`config.py` 已有 `judge_model` / `compress_model` / `extract_model` /
  `reflect_model` / `dedup_model`）——新增一个 `creator_model` 即可，空则回退主模型

这正好复用你 `docs/design.md` §14.2 的既有模式，不用发明新东西。

### 4.2 工具面（这是纯新增，不难）

工作者面向**工作版**（草稿副本），原始包只读：

| 工具 | 作用 | 关键约束 |
| --- | --- | --- |
| `read_world` | 读 `world.yaml`（世界名/时代/规则/文风/禁用/开场） | 只读 |
| `read_npc(npc_id)` | 读一张 NPC 卡全部字段 | 只读 |
| `list_npcs` | 列 NPC id + 名字 + 身份 | 只读 |
| `read_lore` | 列 `lore` 条目（id/keys/摘要） | 只读 |
| `update_world_field(field, value)` | 改世界级字段 | 白名单字段 |
| `update_npc_field(npc_id, field, value)` | 改人物设定 | 白名单字段 + 枚举校验 |
| `add_npc` / `remove_npc` | 增删人物 | 校验 id 唯一、引用完整 |
| `upsert_lore(id, keys, text, …)` | 增改世界书条目 | 复用 `lore.validate_key`（"能校验的必能匹配"） |
| `validate_pack` | 调 `check_worldpack` 并**把报错原文回给模型** | **这就是修复循环的燃料** |
| `diff_pack` | 显示工作版 vs 原版的差异 | 给玩家确认用 |

**`validate_pack` 是整个设计的支点**：你已经有 97 个 `raise WorldPackError` 的校验器，
包含"未声明引用""可达性""数值上限"。创作 Agent 的循环就是：

```
模型提议改动 → 写工作版 → validate_pack → 有错就把原文回灌 → 模型修 → 再校验 → 玩家确认 → 发布
```

**这正好是 `import_story.py` 里已经验证过的 `_validate` / `_repair_sections` 循环模式**，
只是从"一次性生成"变成"对话式增量修改"。

### 4.3 与 dsh-tavern 的差异

dsh-tavern 的改卡是"讨论确认后修改工作版"，并且明确要求
"**原世界书已有完整设定时，通过已读条目复用完成任务，不建立平行档案**"、
"正文被手动修改时**拒绝覆盖并报告冲突**"。

你的等价物更简单也更硬：**工作版 + 校验器 + 显式发布**。
不需要"讨论确认"这种软约束——`check_worldpack` 是硬门禁。

---

## 5. 前端：给不懂前端的人的选型与路径

这一节是本文的重点，因为它是你最不确定、也最容易卡住的地方。

### 5.1 先明确"dsh-tavern 的效果"到底是什么

剥掉插件生态，它的界面就是**三个区域 + 两个工作台**：

```
┌────────────┬──────────────────────────────┬──────────────┐
│ 左：会话/   │ 中：正文流（流式打字）         │ 右：状态栏     │
│  存档列表   │  ────────────────────────    │  属性/好感     │
│  选卡入口   │  候选项按钮（独立于正文）      │  进度/计划     │
│            │  ────────────────────────    │  在场角色      │
│            │  输入框（自由输入）            │  约定          │
└────────────┴──────────────────────────────┴──────────────┘
  工作台 1：卡片/包库（浏览、选、导入）
  工作台 2：编辑器（左边对话、右边字段与校验报告）
```

**这张图里的每一个数据你都已经在后端产出了**，这是你最该放心的一点：

| 界面元素 | 你已有的数据源 |
| --- | --- |
| 流式正文 | SSE `delta` 事件（`game.on_text` → `_make_stream_sink`） |
| 正文终稿 | SSE `done` 事件的 `narration` |
| 候选项 | `done.choices`（引擎已是独立字段，不是正文的一部分） |
| 关键抉择 | `done.choice_prompt` + `done.briefing` |
| 结局 | `done.ending` |
| 状态栏（模型可见的真值原文） | `GET /api/{sid}/status` → `{text, ending}`，`text` 就是 `game.status_text()` |
| 日程/行动点/可做行动 | `GET /api/{sid}/actions` → `{day, action_points_left, critical, actions[]}` |
| 本轮发生了什么 | `done.recovered` / `done.sub_turns` / `done.turn` |
| 卡片库 | 新增 `GET /api/catalog`（§2.1） |

> **产品差异点（建议主打）**：右侧状态栏直接渲染引擎注入给模型的**同一份** `status_text()`。
> 你 README 管这叫"代码掌握真值的可视化"。dsh-tavern 做不到这一点——
> 它的正文与状态来自**两个不同的 Agent**，玩家看到的状态是投影不是真值。

### 5.2 技术栈推荐：**Vue 3 + Vite**，分三阶段落地

**推荐理由（针对"不懂前端"这个前提）：**

1. **模板就是 HTML**。`v-if` / `v-for` / `{{ }}` 三个概念就能开工；React 要同时吞下 JSX、
   hooks、闭包捕获过时状态、依赖数组这些坑，对新手陡得多。
2. **中文文档是一等的**。Vue 官方文档中文版完整且是第一手（不是翻译腔），
   对你这个中文项目是实打实的优势。遇到问题搜到的资料量也够。
3. **Vite 的热更新**是学习期最重要的东西：改 CSS 即时生效、改组件即时生效且**保留当前状态**。
   没有它会非常痛苦——你会一直在"改一行 → 手动刷新 → 重新点回刚才那一步"。
4. **一条命令起步**：`npm create vue@latest` 会直接给你路由、状态管理、开发服务器的骨架。
5. **能长到你要的规模**：包库、编辑器、状态栏都只是组件。

**不推荐的理由（我认真考虑过这三个）：**

- **htmx / 服务端渲染**：做编辑器的表单很爽，但**流式正文日志**和**实时状态栏**会很别扭——
  htmx 的心智模型是"请求→替换片段"，而你这里是"一条长连接持续往列表里追加"。
- **继续手写原生 JS**：就是现在的状态。到 2000~3000 行会撞墙，而且每加一个界面都要重写一遍
  DOM 操作、事件绑定、状态同步。
- **React**：dsh-tavern 用的是它，但那是为了往 DSH 的槽位里塞组件——你不复用 DSH，
  这个理由对你**不成立**，于是只剩更陡的学习曲线。

### 5.3 三阶段路径（关键：第一阶段不需要 Node）

**Stage A（✅ 已完成，2026-10）** —— 把 `web.py` 的 `INDEX_HTML` 字符串拆成真实文件：

```
game_agent/webui/          ← 注意：不能叫 web/（与 web.py 同名会让包解析变脆）
  index.html               ← 从 INDEX_HTML 的外壳部分搬出来
  app.css                  ← CSS（26 行，原样搬运；三栏 grid 留给 Stage B）
  app.js                   ← JS（199 行，原样搬运）
```

**执行结果与验证见 §6.1**（含真机 HTTP 冒烟）。落地的关键点：

- `app.mount("/static", StaticFiles(directory=str(WEBUI_DIR)), name="static")`；
- `index_html()` 每请求读盘 + 注入自由输入文案 → 改前端不必重启；
- `frontend_bundle()` = HTML+CSS+JS 拼接，**保留给结构守卫**（拆分前那些
  "选项按 choice_prompt 分流/生成态/结束今天/恢复痕迹/回顾切换"的断言全部继续有效）；
- **行为零变化**：配色、布局、交互、SSE 契约都没动。这一步只是把"前端"从
  一个不可维护的字符串变成可迭代的目录。

> 原计划里的 `static/` 子目录**没有采用**：`game_agent/webui/` 已经语义明确，
> 再加一层 `static/` 只是让路径变长。真正要避的是与 `web.py` 同名。

**这一步不改变任何行为，但它是后面所有工作的前提**，而且当场就让你能用
CSS Grid 摆出三栏布局（`display:grid; grid-template-columns: 240px 1fr 320px`）。

**Stage B（3–6 天，引入 Vite + Vue 3，⬜ 未开始）**
```
game_agent/webui/
  index.html
  vite.config.js         ← 配 server.proxy 把 /api 转发到 127.0.0.1:8000
  src/
    main.js
    App.vue
    api/client.js        ← 7 个端点的薄封装
    composables/useTurnStream.js   ← ← 最关键的一个文件，见 5.4
    components/
      ProseStream.vue    ← 正文流 + 流式光标 + 生成计时
      ChoiceList.vue     ← 候选项 / 关键抉择（两种状态）
      StatusPanel.vue    ← 右侧：直接渲染 status_text() + actions
      InputDock.vue      ← 自由输入 + 发言/回顾/存档/读档
      EndingCard.vue     ← 结局
    views/
      PlayView.vue       ← 游戏主界面（上面那张三栏图）
      LibraryView.vue    ← 卡片/包库（GET /api/catalog）
      WorkbenchView.vue  ← 编辑器（对话 + 字段 + 校验报告）
```
开发时 `npm run dev`（Vite 跑 5173，proxy 转 `/api` 到你的 8000），
`uvicorn` 照常跑——**两边热更新，互不干扰，没有 CORS 问题**。

**Stage C（1 天，部署，⬜ 未开始）**
`npm run build` 产出 `dist/`，FastAPI 挂载 `dist/` 作为静态目录，
`GET /` 返回 `dist/index.html`。**一个进程部署**，沿用你现在的 `python -m game_agent web`。
Vite 只在开发时需要，运行期不需要 Node。

### 5.4 唯一一个你必须知道的坑：POST + SSE

**`EventSource` 在这里不能用，而且用了会出 bug**：

1. `EventSource` 只支持 **GET**，而你的 `/api/{sid}/turn` 是 **POST**（`TurnRequest` 有 body）。
2. 更严重的是：**`EventSource` 断线会自动重连**。对一个"跑一个回合"的端点，
   自动重连 = **把回合重跑一遍**（还会重复扣费、重复落盘）。

所以正确做法是 `fetch()` + `ReadableStream` 手动解析 SSE 帧——
你的原生 JS 现在就是这么做的（`web.py:519-541`），只是搬进 Vue 时要包成一个 composable。

**两个必须注意的细节**（很容易踩）：

- `delta` 事件的 `data` 是**一个裸 JSON 字符串**，不是对象：
  后端 `deltas.put(("delta", piece))` 然后 `json.dumps(payload)`（`web.py:272, 291`），
  所以 `JSON.parse(data)` 得到的是 `"那女子立于阶下…"`，**不是** `{text: "..."}`。
  （`done` 事件才是对象。）
- **永远以 `done.narration` 为最终正文**，不要相信累积的 deltas。理由是引擎会在回合内部
  作废重来（`_txn_rollback`：`empty_narration` / `meltdown` / `critique_regenerate`），
  已经推给前端的增量可能属于一份被丢弃的草稿。规则：
  **deltas 只负责"活着的感觉"，`done.narration` 负责"什么是真的"；收到 `error` 时把草稿标记为作废，不要当成正史展示。**

参考实现（`src/composables/useTurnStream.js`，约 45 行）：

```js
import { ref } from 'vue'

export function useTurnStream(sid) {
  const streaming = ref(false)
  const draft = ref('')      // 流式增量累积（仅用于展示）
  const error = ref('')

  async function send(body, { onDone } = {}) {
    streaming.value = true
    draft.value = ''
    error.value = ''
    try {
      const res = await fetch(`/api/${sid}/turn`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })

        let i
        while ((i = buf.indexOf('\n\n')) !== -1) {   // SSE 帧以空行分隔
          const frame = buf.slice(0, i)
          buf = buf.slice(i + 2)

          // 每个帧只有一行 data（后端 json.dumps 会把换行转义成 \n，不会真的断行），
          // 且 value 一定是合法 JSON，所以 trim 掉分隔空格是安全的。
          let ev = 'message'
          let data = ''
          for (const line of frame.split('\n')) {
            if (line.startsWith('event:')) ev = line.slice(6).trim()
            else if (line.startsWith('data:')) data += line.slice(5).trim()
          }
          if (!data) continue

          const payload = JSON.parse(data)
          if (ev === 'delta') draft.value += payload        // ← 裸字符串
          else if (ev === 'done') {
            draft.value = payload.narration                 // ← 以终稿为准
            onDone?.(payload)
          } else if (ev === 'error') {
            error.value = payload                           // ← 裸字符串
          }
        }
      }
    } catch (e) {
      error.value = String(e?.message || e)
    } finally {
      streaming.value = false
    }
  }

  return { streaming, draft, error, send }
}
```

`PlayView.vue` 里就是：

```js
const { streaming, draft, error, send } = useTurnStream(sid)
// 发言
send({ kind: 'say', text: '我上前一步。' }, { onDone: v => applyView(v) })
// 点关键抉择的第 2 项
send({ kind: 'pick', index: 1 }, { onDone: v => applyView(v) })
// 做一个日程行动
send({ kind: 'act', action_id: 'gift_visit' }, { onDone: v => applyView(v) })
// 结束今天（行动点耗尽时必须，否则会卡在同一天）
send({ kind: 'end_day' }, { onDone: v => applyView(v) })
```

> 顺带一个既有缺陷值得在重写时修掉：现在存档/读档是**硬编码 `path: "web.json"` 且用 `alert()`**
> （`web.py` 前端部分）。后端 `_safe_save_path` 已经做了路径白名单校验，
> 前端只要给个存档名输入框 + 一个非阻塞的提示条即可。

### 5.5 不要做的事

- **不要**在 Stage A 之前引入任何前端框架（会卡住）。
- **不要**一开始就上 TypeScript：先用 JS 把界面跑通，等结构稳定再迁移（Vue 对渐进式 TS 支持很好）。
- **不要**选组件库做游戏主界面（外观会被带偏）；但**编辑器工作台**的表单可以用
  Element Plus / Naive UI，那里正需要现成控件。
- **不要**碰 Electron / 移动端打包——那是 dsh-tavern 的分发层，与"三条能力"无关。

---

## 6. 引擎侧改造清单（合并去重）

> **状态口径**：✅ 已落地（含守卫）· ⬜ 未开始。改动代码后**必须**跑离线全量；
> 守卫文件写在"证据"列，以便后来人定位"这条为什么可信"。

| # | 改动 | 大小 | 阻塞谁 | 状态 · 证据 |
| --- | --- | --- | --- | --- |
| E-1 | **修记账串号**：`_WEB_TRACKER` 曾是全进程单例，多会话共写 `saves/usage-web.jsonl` | 小 | 一切多会话形态（Critical） | ✅ **已修**：每会话一个账本 `saves/usage-<sid>.jsonl` + 条目带 `session` 轴；新增 `GET /api/{sid}/cost`。`tests/test_web_accounting.py`（8 项，含"进程级单例不得回归"的钉名字守卫） |
| E-2 | **存档加剧本身份戳**：此前只有 `state.to_dict()` + `save_version` | 小 | 换包/改包后读旧档 | ✅ **已修**：`save_version` 2→3，落 `pack: {id, digest}`；读档不一致抛 `PackMismatchError`（**拒绝并提示**，且在构造 `GameState` 之前，不留半应用状态）。`tests/test_pack_identity.py`（14 项） |
| E-3 | 新增 `catalog.py` + `GET /api/catalog` + `_make_game(sid, pack_id)` + `POST /api/new{pack_id}` | 小 | 能力 A | ⬜ 未开始（会话级选包仍缺；`GAME_WORLDPACK` 仍是进程级） |
| E-4 | 会话级"自由/剧本"开关（跳过节点进入） | 小 | 能力 A | ⬜ 未开始 |
| E-5 | `import_story.py` → `game_agent/worldgen.py`（纯函数 + 薄 CLI） | 中 | 能力 B | ⬜ 未开始 |
| E-6 | 后台任务表 + 进度 SSE（照抄 `_turn_stream` 的 queue+线程模式） | 中 | 能力 B | ⬜ 未开始 |
| E-7 | 草稿区 `world-packs/_drafts/` + 发布闸门（过 `check_worldpack` 才能发布） | 小 | 能力 B/C | ⬜ 未开始 |
| E-8 | 创作者 Agent：新 system prompt + 10 个工具 + `creator_model` 路由 + 修复循环 | 中 | 能力 C | ⬜ 未开始 |
| E-9 | 会话列表 / 存档列表接口（现在 `SESSIONS` 是无淘汰的内存 dict） | 小 | 前端左栏 | 🟡 **部分**：`GET /api/{sid}/cost` 已加；会话/存档**列表**接口仍缺 |

> E-1 和 E-2 必须最先做：它们是 `plan-creator-player.md` 已认定的缺口（G1/G2），
> **在多会话/多剧本之前修是 3 个小改动，之后修是数据考古。** 本批已完成。

### 6.1 本批执行记录（2026-10）

**交付**：Stage A 前端拆分 + E-1 + E-2。

**Stage A（§5.3 第一阶段）**：`web.py` 的内嵌 `INDEX_HTML`（248 行 HTML+CSS+JS 混在一个
Python 字符串里）拆成 `game_agent/webui/{index.html,app.css,app.js}` 三个真实文件。

- 服务侧：`WebUI_DIR` + `app.mount("/static", StaticFiles(...))`；
  `index_html()` 每次请求读盘并注入自由输入文案（改前端不必重启，接近热更新）；
  `frontend_bundle()` = HTML+CSS+JS 拼接，**专供结构守卫**（等价于拆分前的 `INDEX_HTML` 语义）。
- **单一真源保持**：文案仍来自引擎常量 `storyline.FREE_INPUT_OPTION`，注入点从
  `INDEX_HTML.replace(...)` 移到 `index_html()`；占位符与 JS 全局改名
  （`window.__GAME_FREE_INPUT__`）以免与占位符字面量混淆。
- **一个目录命名坑**：不能用 `game_agent/web/static/`——`game_agent/web.py` 已占用该名字，
  目录与模块同名会让包解析变脆。故用 `game_agent/webui/`。
- 守卫：`tests/test_web_frontend.py` 新增 4 项（真实文件存在、注入语义不变、
  `/static/*` 真的可达、`/` 返回注入后的页面），并钉住 `INDEX_HTML` **不得回归**。

**E-1（G1 记账隔离）**：`UsageTracker(path, session=...)`；`web._usage_path(sid)`；
`Session` 增必填 `usage` 字段（漏传当场失败，而不是静默共享）；
`_shared_tracker`/`_WEB_TRACKER` 删除；新增 `GET /api/{sid}/cost`。

**E-2（G2 剧本身份戳）**：`worldpack.pack_digest()` / `pack_meta()`；
`evalmeta.normalized_bytes()` 抽出以让"换行归一"只有一个实现（`file_digest` 与
`pack_digest` 共用）；`save.PackMismatchError`；`check_pack_identity()`。

- **口径决策**：`pack_digest` 只吃**玩法内容**（world/schedule/mainline/events/endings + `npcs/*.yaml`），
  **不吃 `judge_corpus*.yaml` / `smoke_profile.yaml`**——把"质量门的尺子"当"游戏规则"
  会让每次扩语料都误拒全部旧档。
- **只补不漏**：旧存档（无身份戳）与裸调用（脚本/测试不传当前包身份）都放过。

**验证**：离线全量 **875 项全绿**（新增 4 个守卫文件/共 29 个 test 函数）；
8 个世界包 `check-worldpack` 全通过；`node --check app.js` 语法通过；
**真机 HTTP 冒烟**（uvicorn 起服务）：`/` `/static/app.css` `/static/app.js` `/static/index.html`
全部 200 且 content-type 正确，占位符已注入、页面正确引用外链脚本。

---

## 7. 交付顺序与工作量（人日，粗粒度）

| 阶段 | 内容 | 估算 | 状态 |
| --- | --- | --- | --- |
| 0 | E-1 / E-2（两个已知缺陷） | 2–3 | ✅ **已完成**（875 项全绿） |
| 1 | **Stage A 前端拆分**（半天，无新工具链）+ E-3 / E-4 / E-9 | 3–4 | 🟡 **Stage A 已完成**；E-3 / E-4 / E-9 未开始 |
| 2 | **能力 A 打通**：卡片库 + 会话选包 + 自由/剧本开关 + Stage B 游戏主界面 | 6–10 | ⬜ 未开始 |
| 3 | **能力 B**：worldgen 服务化 + 后台任务进度 + 草稿/发布闸门 + 导入界面 | 6–9 | ⬜ 未开始 |
| 4 | **能力 C**：创作者 Agent + 工具面 + 校验修复循环 + 编辑器工作台 | 6–9 | ⬜ 未开始 |
| 5 | Stage C 构建部署 + 存档名/提示条等打磨 | 2–3 | ⬜ 未开始 |

**合计约 25–38 人日**，其中前端约占一半。**已完成约 3–4 人日**（阶段 0 + Stage A）。
如果你想更快看到效果：**把阶段 2 做完就已经是"选一张卡自由游玩 + 沿主线推进"的完整体验了**
（因为能力 B 的生成管线本来就存在，先用 CLI 生成包喂给界面即可）。

> **下一步建议从这里接**：阶段 2 的 E-3（`catalog.py` + `GET /api/catalog` + 会话级选包）
> 是"选一张卡"的最小闭环，且它依赖的两件事（记账隔离、存档身份戳）本批已经落地。

---

## 8. 顺带值得改的 harness 改进点（来自对 dsh-tavern 的实证）

你说了 harness 也可以往好的方向改。以下四条是逐文件核过的、有对照物的改进点，
每条都标了**代价**，不是无脑照抄。

### 8.1 让"没调用收尾工具"不再等于熔断（最有价值的一条）

**现状**：你的协议强制模型每轮调用 `submit_narration` 收尾。
`MAX_TURN_ITERATIONS = 3`（`llm.py:32`）耗尽后 `raise LLMTurnError`，
`game.py` 接住后走 `_txn_rollback` + `_meltdown_fallback`——**整轮作废、换成保守文案、玩家看到的是兜底文本**。

**对照**：dsh-tavern 明确不要求工具调用
（`docs/architecture.md:142`："正常回合**不要求模型调用工具**来读取已知上下文或提交正文"），
而它的工具目录是**固定不变**的（`docs/design/agent-memory-retrieval.md:115`：
"工具定义始终存在，不随自由故事、剧本模式或当前轮次变化，**保持前台请求前缀稳定**"）。

**改法（保留你的原子性，去掉脆弱性）**：当模型这一轮**没有**发起任何工具调用、但返回了非空正文时，
把它当作"**零效果的叙事**"接受——即 `narration = 正文`、`effects = ∅`、`choices = []`，
照常落盘、照常建 checkpoint。

**为什么这不破坏"narration accepted ⟺ effects committed"**：该不变量说的是
"没有已提交的效果，就没有被接受的叙事"。零效果的回合里两边都是空集，**不变量依然成立**。
真正要拒绝的是"有工具调用但参数被截断"这类情况——那条你已经处理得很好
（`llm.py:157-177` 的截断即整批作废，是全文件最漂亮的一段工程判断）。

**代价**：这类回合拿不到候选项（`choices` 为空），前端要能优雅处理"本轮无候选项、只能自由输入"。
但这比"整轮熔断成兜底文案"好得多。

### 8.2 上下文省略要变成结构化数据

dsh-tavern 的 `context-planner.js` 每次 `plan()` 都返回一个 `audit` 块：
`included[]`（kind/chars/required）、**`omitted[]`**、`warnings`、`totalChars`，
而且省略是带原因的机器可读数据，例如
`omitted: [{kind:'stable-card-details', reason:'人物卡基本信息和常驻世界书已固定在游戏会话稳定前缀'}]`。

你的 `context.py` 有预算（`LORE_BUDGET_RATIO` / `CARD_BUDGET` / `MEMORY_BUDGET` / `STATUS_BUDGET`），
但**省略了什么、为什么省略**没有对外暴露。加一个同样的 audit 块会直接提升两件事：
① 前端可以在状态栏旁边显示"本轮注入了什么"（正是你 README 主打的"模型看到的与玩家看到的同源"）；
② 长局调参时不用再靠猜。**这是纯增量改动，不动现有预算逻辑。**

### 8.3 checkpoint 的两个具体细节（给阶段 2 用）

dsh-tavern 的 `story-timeline.js` 有两个值得直接采用的做法：

- **checkpoint 上限 40 条**（`:199`），而不是无限增长。
- **不存完整的前置状态，只存 `beforeRevision`**，回退时按 revision 回读历史；
  读不到就抛 `CHECKPOINT_HISTORY_REQUIRED`，**不猜**。
  这对你很重要——`runlog.py` 现在按回合存**全量** `state.to_dict()` + history（长局约 10MB/局），
  玩家面存档直接照搬会长得很快。**用 revision 回读 + 哈希校验，比复制全量状态更省。**

### 8.4 压缩失败必须可见（这条是前置条件，不是优化）

dsh-tavern 的压缩是**两侧独立**的（前台会话与后台会话各自压缩，各有
`{status, before, after}` 回执），因此一侧失败得到的是 `partial` 而不是静默无事发生。

你的 `_compress_history` 有 **5 处静默 `return`**（切点找不到 / 无新内容 / LLM 失败 / 摘要为空 / 配对失败），
而 `_overflow_retry` **只允许一次**。持续失败时的表现不是报错，而是**历史一路涨到熔断**。
阶段 3 把结算移入后台后，"安静地坏掉"的窗口会更宽——**所以这条要在阶段 3 之前做**。

---

## 9. 一条重要边界：内容可以导入，运行时兼容不要做

你第三条能力里的"**修改人物设定和世界书**"，以及"选一张**卡**"，
很容易被理解成"要兼容酒馆（SillyTavern）的人物卡和世界书"。
`docs/sillytavern-borrow.md` §E-1 已经把 `character_book` ↔ World Info ↔ 你的 `LoreSpec`
的字段映射表逐列写好了。**建议：只做单向导入，不做运行时兼容。**

理由是实证的——**dsh-tavern 自己的兼容度远低于它的宣传**（这是逐文件核过的）：

- **世界书**：`probability` / 作者写的 `sticky` / `cooldown` / `delay` **全都只存不生效**；
  `matchWholeWords` **对中文是坏的**（`\W` 把每个汉字都当边界，`峨眉和药王谷` 会误命中 `和药`），
  它自己的验证记录写的是"**容量改造已生效，但相关性仍明显不足**"。
- **预设**：采样参数、`sysprompt`、`reasoning`、`openai_max_tokens` 等**全部只存不生效**；
  项目自己的备忘写着"**dsh-tavern 不兼容酒馆预设的完整运行流程，它只从预设中抽取并兼容破限方案**"。
- **人物卡**：**没有 PNG 导出**（无编码器，官方功能清单明说"不宣称支持 PNG 导出"）；
  `spec_version` **从不校验**（写 `9.9` 也能过）；PNG 解析只认 `tEXt`（不认 `zTXt`/`iTXt`），
  而且同一段逻辑**被独立实现了三遍**；**没有卡片格式规范文档**。
- **卡片脚本运行时**：默认 `trustedCardMode: true` —— 沙箱 iframe **不带 `sandbox` 属性**，
  并把 `SillyTavern` / `TavernHelper` / `Mvu` 等全局**直接装到真实父窗口上**，
  CSP 允许 `unsafe-eval` 与任意网络。有审计记录显示卡片脚本在沙箱内用自己的 API Key
  直连模型端点，**这次调用不出现在宿主的请求日志、工具限制和缓存统计里**。

**结论**：把酒馆人物卡当**素材**读进来（角色设定、性格、说话风格、关系、世界书条目 → 你的
`npcs/*.yaml` 与 `lore`），收益大、成本低、且**这条路径与你的 D 系列决策（不借 PNG 卡、
不借 sticky/cooldown、不借加权随机、不借双宏引擎）完全不冲突**。
但要"运行"酒馆卡片的逻辑（脚本、MVU、正则美化、预设），是一条连参考实现都只走了一半的深坑，
且会把本项目最大的资产——**声明式、不执行作者代码**——换成它最大的风险。

---

## 附：与既有文档的关系

- `docs/plan-creator-player.md`——平台化的既有设计（目录层 / 会话选包 / 两个工作台 / 三个缺口 G1-G3）。
  本文与它同向，并把 dsh-tavern 的实际形态作为参照补进来。
- `docs/sillytavern-borrow.md`——ST 源码评审；A 系列（lore 注入语义）已落地，E 系列（卡片互操作）是本文 §9 的依据。
- `docs/plan-dsh-tavern-parity.md`——对标分析；解释为什么不做 DSH 插件（含宿主侧硬证据）。
- 本文**不修改**任何既有决策，也不改动引擎代码；§6 的清单是提案。
