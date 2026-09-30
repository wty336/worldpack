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

### 2.1 引擎侧（改动很小）—— ✅ **已完成（E-3），见 §6.2**

**原来的问题**：曾是"一个进程 = 一个包"。`web.py` 的 `_pack_path()` 读环境变量
`GAME_WORLDPACK`，`_make_game(sid)` 无参数地取它 —— **换包要重启进程**。

```python
# 改前
def _pack_path() -> str:
    return os.environ.get("GAME_WORLDPACK", DEFAULT_PACK)
```

**实际落地**（`plan-creator-player.md` §1 已拍板同一方向）：

1. `game_agent/catalog.py`：扫描 `world-packs/*`，对每个包调 `load_worldpack()`，
   返回卡片元数据（`name` / `era` / `opening` / NPC 数 / 节点数 / 结局数 / digest）。
   `load_worldpack` 本来就在开局时全量载入内存、之后不碰磁盘，所以目录扫描是干净的。
2. `_make_game(sid, pack_id=None, *, mainline_enabled=True)`：会话创建时按 `pack_id` 取包。
3. `POST /api/new` 接受 `{pack_id, mode}`；新增 `GET /api/catalog` 与 `GET /api/{sid}/meta`。
4. 环境变量降级为**默认值**，且仍允许指向 `world-packs/` 之外的任意目录（CLI `--pack` 的既有用法不破）。

**存档的剧本身份**（否则改包后读旧档会静默穿帮）：原 `save_game()` 只写
`state.to_dict()` + `save_version`。现落 `pack: {id, digest}`，读档不一致 → **拒绝并提示**。
这是 `plan-creator-player.md` 的 G2，已在批次 1 落地（见 §6.1 E-2）。

### 2.2 "自由游玩 vs 剧本模式"

你的引擎已经有天然分界，不需要新机制：

- **剧本模式** = 包里有 `mainline.nodes` → 当前行为（节点触发 + 关键抉择 + `plan` 指针）
- **自由游玩** = 没有节点，或会话开关关掉节点进入

对已有节点的包想"自由游玩"，只加一个会话级布尔量，在 `Game` 里传给
`storyline.begin_turn` / `_try_enter_node`：为假时直接跳过节点进入与关键抉择。
**这是十几行的改动，不要动 `storyline.py` 的状态机本身。** ✅ 已按此落地（E-4）。

> 前端已把它做成**每局开局的一个选择**（"跟着主线走 / 自由探索"），
> 而不是包的一个属性——同一个包两种玩法是有价值的。

**实际实现的语义边界（比"十几行"更值得记住）**：`mainline_enabled=False` 只表示
**不再进入新节点**；已进入的节点状态、结局判定、禁用词过滤、日程与事件**全部照旧**。
理由：玩家想切换的是"要不要被主线牵着走"，不是"把已经发生的剧情擦掉"——
抹掉进度会让"先自由探索、之后再跟主线"变成不可能。
配套地 `choice_locked()` 在自由模式恒为 `False`：即使存档是从剧本模式带过来的、
`pending_choice` 还挂着，也不该在自由游玩里把玩家锁在固定选项上（那正是玩家切过来的原因）。

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

#### 工作台右栏是"呈现"，**不是**"表单"

上表里的 `update_*` / `add_npc` / `upsert_lore` 都是 **Agent 的工具**，不是给人点的按钮。
右栏（§5.1 的"右边字段与校验报告"）**只读呈现**：字段当前值、角色/节点/结局计数、
内容指纹、`check_worldpack` 结论、工作版 vs 原版的 diff。

**为什么不做手写字段编辑器**（一个自然的诱惑，也曾在 roadmap 里被排成 N2 的一部分）：

1. 本节 §4.1 的整个论证就是"结构化 CRUD 交给 Agent 比表单好"——同一份文档既说表单不合适、
   又排一个表单工作台，是自相矛盾的；
2. 需求原话是"**与 Agent 对话**…修改人物设定和世界书"，对话式才是目标；
3. **表单会和草稿抢同一份真值**。Agent 改完草稿，表单里显示的是它上次读到的值；
   要么表单跟着 agent 的工具调用实时刷新，要么两边不一致。这是纯粹的 UI 状态同步债，
   换不来任何能力。

结论：**编辑权归 Agent，人负责"确认与发布"**。人的两个动作是"说清要改什么"和
"看了 diff 之后决定发不发"——后者由 `check_worldpack` + 显式发布兜底（§4.3）。

#### 开工即做的前置：Agent 调用的**归因轴**

创作者 Agent 会为一句话跑好几个回合（读 → 改 → 校验 → 修）。这些调用必须能回答
"**这一版草稿改下来花了多少钱**"，所以 `UsageTracker` 的入参在 N6 第一次接线时就要
带上包/会话轴，**不能留到"成本可见"那一项再补**。

理由是实证的：G-6 就是记账串号——`_WEB_TRACKER` 曾是全进程单例，所有会话共写
`saves/usage-web.jsonl`，在多剧本下直接是**数据错误**而不是显示问题。事后补轴要重造
历史数据（`docs/plan-dsh-tavern-parity.md` G-6 / E-1）。

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

> ⚠️ **读这两行时别把它读成"两个先后项"**：工作台 2 的"左边对话"就是能力 C 的创作者
> Agent（roadmap **N6**），"右边字段与校验报告"是它的**显示面**——两者是同一个屏幕的两半，
> 不是两个阶段。按"N2 前端 → N6 Agent"的顺序做成"先手写表单、之后再加对话"会白做一块，
> 理由见 §4.2 的"右栏是呈现，不是表单"。当前排期已按此收窄为 **N2a（共享外壳）→ N6**。

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

**Stage B（✅ 已完成，2026-10）—— Vite + Vue 3 三栏重写**
```
game_agent/webui/
  index.html             ← Vite 入口（挂载点 #app）
  vite.config.js         ← base:'/static/' + build-manifest 插件 + dev proxy
  package.json           ← 依赖只有 vue / vite / @vitejs/plugin-vue
  src/
    main.js
    App.vue              ← 骨架：拿 config/catalog/saves → 选卡 → 进游戏
    styles.css           ← 三栏 grid + 窄屏塌成单栏
    api/client.js        ← 端点薄封装（唯一知道端点形状的地方）
    composables/useTurnStream.js   ← POST+SSE（见 5.4）
    components/
      NoticeBar.vue      ← 非阻塞提示条（替换 alert）
      ProseStream.vue    ← 正文流 + 流式光标 + 回顾切换 + 恢复痕迹
      ChoiceList.vue     ← 候选项 / 关键抉择 / 结局 / 生成计时
      StatusPanel.vue    ← 右栏：直接渲染 status_text() + 行动区
      InputDock.vue      ← 自由输入 + 发言/存档/换卡
    views/
      LibraryView.vue    ← 选卡屏（卡片 + 模式 + 读档）
      PlayView.vue       ← 三栏游戏屏
  dist/                  ← **入库的构建产物**（CI 不跑 npm，服务端直接托管）
    build-manifest.json  ← 源文件内容哈希（守卫据此发现"改了没重建"）
```
开发时 `npm run dev`（Vite 跑 5173，proxy 转 `/api` 到你的 8000），
`uvicorn` 照常跑——**两边热更新，互不干扰，没有 CORS 问题**。

**Stage C（✅ 已完成，2026-10）**
`npm run build` 产出 `dist/`，FastAPI 挂载 `dist/` 作为静态目录。
**一个进程部署**，运行期不需要 Node（`python -m game_agent web`）。

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
| E-3 | 新增 `catalog.py` + `GET /api/catalog` + `_make_game(sid, pack_id)` + `POST /api/new{pack_id}` | 小 | 能力 A | ✅ **已完成**：`game_agent/catalog.py`（坏包隔离 / 路径安全查表 / 签名缓存）；`GET /api/catalog`；`POST /api/new{pack_id}`（未知 id → 400）；`GET /api/{sid}/meta`。`tests/test_catalog.py`（15 项） |
| E-4 | 会话级"自由/剧本"开关（跳过节点进入） | 小 | 能力 A | ✅ **已完成**：`StorylineEngine(mainline_enabled=)` + `POST /api/new{mode}`；语义刻意收窄为"不再进入新节点"（§2.2）。`tests/test_catalog.py`（4 项） |
| E-5 | `import_story.py` → `game_agent/worldgen.py`（纯函数 + 薄 CLI） | 中 | 能力 B | ✅ **已完成**：`worldgen.py` 809 行（提示词/分块提取/物化/校验修复/语料/进度事件），CLI 851→**237 行**薄壳。等价性已证：重构前 vs 重构后离线跑，退出码相同、**8 个产出文件逐字节相同**、stdout 归一化后逐行相同。守卫 `tests/test_worldgen.py`（22 项） |
| E-6 | 后台任务表 + 进度 SSE（照抄 `_turn_stream` 的 queue+线程模式） | 中 | 能力 B | ✅ **已完成**（2026-10，roadmap **N1**）：`game_agent/jobs.py` 任务表 + 串行执行器；5 个端点（生成 / 查询 / SSE / 取消）；**不能照抄 `_turn_stream`**——那是秒级单连接，分钟级任务必须事件留档 + 回放 + 多订阅者广播。`tests/test_jobs.py`（22 项）+ 真机 `scripts/worldgen_smoke.py` |
| E-7 | 草稿区 `world-packs/_drafts/` + 发布闸门（过 `check_worldpack` 才能发布） | 小 | 能力 B/C | ✅ 已完成 · 见 **§6.6** |
| E-8 | 创作者 Agent：新 system prompt + 10 个工具 + `creator_model` 路由 + 修复循环 | 中 | 能力 C | ✅ **已完成**（2026-10，roadmap **N6**）：`game_agent/creator.py`（工作版 + **11 个工具** + 修复循环 + diff）+ `fork_to_draft` + 4 个端点 + 前端对话面板。证据：`tests/test_creator.py`（33 项）+ 真机对话冒烟 `scripts/creator_smoke.py`（真实模型确实按 read → 改 → validate 走） |
| E-9 | 会话列表 / 存档列表接口（`SESSIONS` 是无淘汰的内存 dict） | 小 | 前端左栏 | ✅ **已完成**：`GET /api/sessions`（pack/mode **由 game 推出**，不存第二份）+ `GET /api/saves`（只读顶层摘要 / 坏档隔离 / mtime 倒序）。`tests/test_catalog.py`（3 项） |

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

### 6.2 批次 2 执行记录（2026-10）：能力 A 打通

**交付**：E-3（目录层 + 会话选包）+ E-4（自由/剧本模式）+ E-9（会话/存档列表）+ 选卡屏。

**`game_agent/catalog.py`**——把 `world-packs/` 从"一个进程一个包"变成"可选的卡"。
三条纪律都是有来历的：
- **坏包隔离**：一个改到一半的包只以 `error` 出现在卡片上，不让整个选卡屏白屏。
- **`id` 不参与路径拼接**：`resolve_pack()` 的实现是"在已列出的目录项里查表"，
  所以 `../`、绝对路径、盘符天然无效（不在任何目录项里）；另加白名单正则兜底。
  与 `_safe_save_path` 同一条教训。
- **缓存必须会失效**：一次全量列举 = 8 次 `load_worldpack`（含 717 行 `_cross_check`），
  实测 **288 ms**；缓存后 **13.6 ms**（21×）。指纹是**逐文件 stat**（size + mtime_ns），
  不是目录 mtime——因为**修改已存在的文件不改父目录 mtime**，用目录 mtime 会让
  "作者改了 npcs/x.yaml"永远不失效。守卫 `test_cache_returns_fresh_content_after_edit`
  正面钉住这条。缓存只省"列举"，真正加载仍在 `_make_game`，所以不可能玩到旧内容。

**E-4 的语义刻意收窄**：自由模式 = **不再进入新节点**。已进入的节点状态、结局判定、
禁用词过滤、日程与事件全部照旧——玩家要切换的是"要不要被主线牵着走"，
不是"把已经发生的剧情擦掉"。抹掉进度会让"先自由探索、之后再跟主线"变成不可能。
`choice_locked` 在自由模式恒为 `False`（即使存档从剧本模式带过来还挂着待决抉择，
也不该把玩家锁在固定选项上）。

**一处设计收敛**：`Session` 起初加了 `pack_id` / `mode` 两个字段，随后**删掉**——
两者都能从 `game` 推出（`game.pack_meta["id"]` / `game.mainline_enabled`），
存第二份只会多出一个能进入非法组合的维度。现在由 `session_pack_id()` / `session_mode()`
派生，`test_session_derives_pack_and_mode_from_game` 用"只改 game、看列表与 meta 是否跟着变"
把这条钉住。`usage` 则相反：必须存且**不给默认值**（默认值 = 漏传即静默共享，正是 G1 的形状）。

**接口契约放在 HTTP 层**：`pack_id` 的校验刻意写在 `api_new`（`_require_pack`）而不是
只埋在 `_make_game` 里——离线夹具替换 `_make_game` 是常规做法，接口契约不该跟着消失。
`_resolve_pack` 内保留一次检查做纵深防御。

**前端（仍是原生 JS，Stage B 未开始）**：选卡屏（卡片列出世界名/时代/角色数/节点数/结局数）、
模式单选、存档下拉（按卡命名存档、`mtime` 倒序）；`alert()` 换成非阻塞提示条
（成功 4 秒消失、错误常驻）；开局失败能退回选卡屏而不是留一个空游戏屏。

**验证**：离线全量 **898 项全绿**（新增 `tests/test_catalog.py` 23 项）；
8 个世界包 `check-worldpack` 全通过；`node --check app.js` 通过；
**真机 HTTP 冒烟**：`/api/catalog` 返回 8 张卡、`default=ancient_jianghu`；
未知 `pack_id` 与未知 `mode` 均 **400**；`/api/saves` 列出 52 个档（旧档 `pack` 为空是
"只补不漏"的预期行为）；页面已挂选卡屏与 `startGame`。
**编码核对**：`/api/catalog` 的字节是合法 UTF-8 且 `ancient_jianghu → 江湖旧梦` 断言通过
（此前控制台看到的乱码是 PowerShell 的解码，不是接口问题）。

### 6.3 批次 3 执行记录（2026-10）：Stage B/C 前端三栏重写

**交付**：Vite + Vue 3 三栏前端（左本局 · 中正文+输入 · 右状态栏）+ 构建产物入库 + 真机冒烟脚本。

**为什么把 `dist/` 入库**：CI 只跑 pytest、不跑 npm，服务端直接托管 dist——这是参照实现
（dsh-tavern 提交 `lib/client.js`）的同一种取舍。代价是"源码改了但忘了重新构建"这个风险，
故配两条守卫：`vite.config.js` 的 `build-manifest` 插件把每个源文件的内容哈希写进
`dist/build-manifest.json`；`tests/test_webui_build.py` 重算源哈希比对。
**用内容哈希而不是 mtime**——全新 clone 里所有文件 mtime 都是检出时间，比不出先后。
（已实测：改一个 `.vue` 不重建，守卫必红并指名到文件；重建后恢复绿。）

**三处容易漏的东西**（都是"文件都在、只是不对"的类型）：
1. **`base: '/static/'`**：Vite 默认输出 `/assets/...`，而 FastAPI 在 `/static` 下托管 dist。
   配错时文件都存在、服务端与测试都发现不了，**只有浏览器白屏**。守卫直接断言
   `dist/index.html` 的每个引用都以 `/static/` 开头且文件存在。
2. **`/api/config` 取代了服务端占位符替换**：`dist/index.html` 是构建产物，服务端再去改写它
   会让"dist 是否与源码一致"的判定失去意义。自由输入文案改由运行时下发，单一真源不变。
3. **POST + SSE 的契约**（§5.4）：`EventSource` 只支持 GET 且**断线会自动重连 = 把回合重跑一遍**，
   故用 `fetch` + `ReadableStream`；`delta` 的 data 是**裸 JSON 字符串**（写成 `payload.text`
   会静默拿到 undefined，表现为"正文空白但状态栏正常"）；`done.narration` 才是真值。

**守卫结构整体重写了一次**（值得记下原因）：Stage A 期间前端是内嵌字符串/原生 JS，
测试只能对**产物文本做子串断言**（`"function startGen" in html`）。Vue 重写后这类断言
要么失效、要么与被测行为无关。现在分三层：
- **接口契约层**（`test_web_frontend.py` 上半）：视图载荷与引擎的分流/拒绝行为，不碰前端实现；
- **源码结构层**（同文件下半）：读 `src/**` 钉住"写错了不报错、只表现为怪现象"的契约
  （选项按 `choice_prompt` 分流、`new EventSource` 不得出现、`done.narration` 覆盖草稿…）；
- **跨层一致性**：从 `api/client.js` 解析出端点清单，与 `web.app` 的路由表逐个核对
  ——端点改名时前端会**静默 404**，这条把它变成测试失败。

**新增 `scripts/webui_smoke.mjs`：真机驱动界面的冒烟**（不依赖 API Key、不花钱）。
自带桩后端 + 走 CDP 驱动无头 Edge/Chrome（Node 22 自带全局 `WebSocket`，零 npm 依赖），
断言 18 项：选卡屏渲染、坏包以禁用态可见、三栏出现、开局叙事/关键抉择/候选项、
右栏渲染引擎状态栏原文、走通一个回合（delta 流式 → done 提交 → 草稿被终稿接管）、
行动区随阶段变化、恢复痕迹对玩家可见。

> **这个脚本当场抓出了两个我自己写的真 bug**（pytest 全绿也发现不了）：
> ① `App.vue` 把 `POST /api/new` 返回的 **opening view 丢掉了**——三栏都渲染出来，
> 但正文与候选全空、右栏一直"等待开局"；② `PlayView` **从不在挂载时取
> status/actions**，右栏永远空白。
> 它自己也先后犯了三个错并当场暴露：按 DOM 顺序选按钮（命中了「剧情回顾」而不是「发言」）、
> 一次性断言流式中间态（`done` 先写草稿、`streaming` 到 finally 才落回，存在假红窗口）、
> 以及桩后端的回合序号差一（第一个 turn 又返回了开局视图，导致断言"通过"得毫无意义）。
> **桩自己的状态机错了比没有桩更危险**——这三处都写进注释留档。

**验证**：离线 **905 项全绿**；前端真机冒烟 **18/18 通过**；
`node --check` 通过；8 个世界包 `check-worldpack` 全通过；
构建产物完整性守卫（入口/资源/哈希/陈旧度/依赖未入库）5 项全绿。

### 6.4 批次 3 补丁（2026-10）：异包旧档不再 500 —— G2 的洞补上了

**实测缺陷**（交接演示时被你自己撞到）：启动服务 → 选《江湖旧梦》开局 → 在存档下拉里
读一个《青槐高中·告白之前》留下的档 → 右栏报 **"读取状态失败：HTTP 500"**。

**根因**：G2 的 `check_pack_identity()` 是**只补不漏**的——旧档没有 `pack` 身份戳，
没有可比的东西，于是放行。放行之后 `state.stats` 里是 `grace`/`study`/`art`（青槐的养成轴），
而当前包（江湖旧梦）的 `schedule.stats` 里没有这些键 →
`context.status_text()` 的 `self.pack.schedule.stats[k]` 直接 **KeyError → 500**。

**所以"只补不漏"本身是对的，但只做身份戳不够**：身份戳能抓"有戳但对不上"，
抓不到"没戳"那一类——而那恰恰是最容易发生的（历史存档全都没戳）。

**修法**：加一道**内容级**校验 `save.state_pack_mismatch(state, pack)`，
不看存档自报的身份，只看内容能不能对上（属性 / 好感 / 计数器 / 物品 + NPC 引用：
记忆、洞察、在场）。口径与 `audit.audit_stats` 一致，避免"引用完整性"出现两套判定。
读档路径（Web 与 CLI）在**赋值之前**校验，不一致 → **400 + 结论先行的理由**，状态不被半应用。

玩家现在看到的是：

```
这个存档属于《青槐高中·告白之前》，而当前在玩《江湖旧梦》——不是同一份内容
（多半来自另一张卡，或是某张卡改版前的旧档）。对不上的内容：属性 ['art','grace',…]…
请改选该卡后再读档，或另开新局。
```

**顺带修好的可发现问题**：`save_summary()` 增 `pack_name`（状态里自报的世界名），
存档下拉于是能显示"旧档·青槐高中·告白之前"——此前 52 个旧档全显示"（旧档无身份戳）"，
玩家根本认不出哪个档属于哪张卡，只能靠"读了被拒"来试（这正是那次 500 的由来）。
实测 52/52 个旧档都能认出来了。

**守卫**：`tests/test_pack_identity.py` +5 项（内容级校验本身）、
`tests/test_catalog.py` +2 项（**端到端复现**：异包旧档经 HTTP 读档 → 400 而非 500，
且拒绝后状态栏仍可用）。
**已用变异验证**：把 `api_load` 里的内容级校验摘掉，这两条守卫立刻变红并复现
`KeyError: 'study'`（与线上同一个位置 `context.py:283`）——即守卫真的在测这个修复，
而不是恰好通过。（第一次做这个探针时 `String.Replace` 因 CRLF/LF 不匹配而静默没生效，
两条守卫"通过"了——**假绿**。探针必须断言"替换确实发生"，否则验证的是空气。）

### 6.5 批次 4 执行记录（2026-10）：B1 生成管线提取（E-5）

**交付**：`game_agent/worldgen.py`（809 行）+ `scripts/import_story.py` 变薄壳（851→237 行）
+ `tests/test_worldgen.py`（22 项）。

**为什么必须搬**：那条管线原本整条锁在一个 945 行的 CLI 脚本里——提取、物化、修复循环、
`print`、`subprocess` 混在一起。后果是**任何想复用的入口都得先 shell 出去**：
Web 创作工作台要进度流、要结构化结果、要在后台线程里跑；CLI 只想要一行行日志。
搬出来之后分工是：
- **`worldgen.py`**：读素材 → 分块提取 → 物化 → 校验-修复 → 语料 → smoke_profile。
  **不打印、不读环境、不起子进程**；进度与日志统一走 `on_progress` 回调。
- **CLI**：参数解析 + 把事件渲染成人读文本 + `--live` 的真机门禁编排
  （要 shell 出去跑 `judge_sensitivity.py` / `worldpack_smoke.py`，属运维编排不属"生成"）。

**新接缝（B2/B3 的接口，现在钉住）**：`on_progress` 事件（`start/extract/retry/repair/
validate/corpus/smoke/done/warn`，可 JSON 序列化 → 直接能过 SSE）、`GenerateOptions`、
`GenerateResult`（含 `stages` 时间线与 `summary`）、可注入的 `SectionExtractor`、
`build_llm(offline)`。

**等价性怎么证的**（不是"我读了一遍觉得没问题"）：把**重构前的 CLI 从 git HEAD 取出来**
与重构后的 CLI 各跑一遍离线管线，比对：
- 退出码相同；**8 个产出文件逐字节相同**（YAML + NPC 卡 + 语料 + 冒烟档案）；
- stdout 归一化后**逐行相同**。

**两处有意的输出变化**（都写进模块 docstring，不静默）：
1. **新增每块进度行**（`生成[world]` / `生成[npc1]` …）。重构前只在**重试**时打印，
   于是几分钟的提取阶段终端一片空白——作者分不清"在跑"还是"卡住"。这些事件同时就是
   B2 进度流的来源，所以它们必须是管线的一部分，而不是 CLI 的装饰。
2. **语料条数由写死的 "30 条" 改为实测值**：离线假 LLM 实际产 25 条（对抗 3×5 + 正常 10），
   真实路径才是 6×3 + 6×2 = 30。日志说 30 而实际 25 属于"日志撒谎"，排查时最费时间。

**顺带**：修掉一处重复输出——管线的 `done` 事件与 CLI 的结束语会各打一遍
"世界包已生成"；现在 CLI 抑制该事件（事件本身不带前导空行，那是给人读的排版）。

**验证**：离线 **934 项全绿**；CLI `--offline --with-corpus` 端到端退出码 0 且产出可校验；
`test_cli_is_a_thin_shell` 钉住提示词与管线函数不得回流到 CLI。

---

### 6.6 批次 5 执行记录（2026-10）：草稿区 ↔ 已发布区（E-7，§3.2 ③）

**交付**：`game_agent/catalog.py` 的草稿生命周期（+139 行）+ `web.py` 四个接口
（`GET /api/packs/drafts`、`POST /api/packs/publish`、`DELETE /api/packs/drafts/{name}`、
生成改道 + `/api/new{draft}`）+ `tests/test_drafts.py`（17 项）。

**这一步解锁了什么**：在草稿区落地之前，"同名生成"被一刀拒绝（`can_create` 怕
`materialize` 清空 `npcs/` 而静默毁掉线上包）。于是"改一版再生成"只能靠不停换名字——
`world-packs/` 里堆出 `foo`、`foo2`、`foo_new2`，作者自己都分不清哪个是最终版。
现在写口变成一条链：**生成 → 校验 → 发布**，Web 界面从不直写已发布区。

#### 三条设计判断

1. **草稿的不可见性靠结构，不靠"记得过滤"**。`_drafts/` 目录天然没有 `world.yaml`，
   而目录扫描要求 `world.yaml`，所以草稿**不可能**被当成一张可玩的卡——
   不需要每个读取点都记得排除它（"记得过滤"这种事迟早会漏一个点）。
   发布是一次 `rename`（同文件系统内原子），不是"改一个字段 + 祈祷没人漏读"。
2. **闸门是 `check_worldpack`，报错原文必须能拿到**。这条不是形式主义：`check_worldpack`
   会拒绝"该节点将永远无法完成""结局数值不可达"这类**作者看不出来、玩家一定会撞上**的问题。
   发布是把草稿变成"别人也能玩"的承诺，没过闸门的东西不该获得这个承诺。而拒绝时
   回的是**原文**而不是"校验失败"——那段原文就是工作台拿去喂模型修的燃料（§4.2）。
3. **`draft` 标记从包的真实位置推出**（`session_is_draft` = 父目录名是不是 `_drafts`），
   不另存布尔量。与 `session_pack_id`、`session_mode` 同一纪律：存第二份就多一个
   "改了这边忘了那边"的机会。

#### 写守卫时发现的一个真实缺陷（已修）

给"坏草稿"写守卫时发现：**草稿如果缺 `world.yaml`，会从草稿列表里整个消失**。
原因是 `list_drafts` 一开始复用了已发布区的 `_is_pack_dir` 判据。后果恰恰最糟——
作者改坏了草稿、正需要看报错原文，看到的却是"草稿不存在"；生成中途被杀留下的
半成品也一样无声蒸发。

修法是让 `list_drafts` **列出草稿区下的每一个子目录**，不要求它有 `world.yaml`。
这是与 `list_packs` 的一处**刻意分歧**，理由写进了代码注释：

- 已发布区是**内容**——缺 `world.yaml` 的东西不是内容，本就不该出现在卡列表里；
- 草稿区是**工作区**——那里的东西是作者明确放进去的。会消失的故障是最难查的故障，
  列出它并附上 `error` 原文，比让它凭空不见有用得多。

守卫用 `missing` / `bad_yaml` 两种坏法参数化，因为两者走**不同**的代码路径：
前者考验"目录还算不算草稿"，后者考验"加载失败有没有降级成 `error`"。

#### 一处必须记下的连带损伤（两个，同一根因）

**加一个兄弟目录 `_drafts/` 会打断所有"裸遍历 `world-packs/`"的地方。** 而 `_drafts`
在字母序上排在 `ancient_jianghu` **之前**（`_` = 0x5F < `a` = 0x61），所以那不是
"偶尔踩到"，是**每次都中**。两处：

1. **`scripts/worldgen_smoke.py` 会被改成"污染仓库"的脚本**。它原本断言生成的包出现在
   `/api/catalog`，并且清理时只删 `world-packs/<name>`。生成改道草稿区之后：
   那条断言会**假红**（其实行为是对的），而清理会**把产物留在 `world-packs/_drafts/` 里**
   ——下一次 `git add -A` 就会把冒烟垃圾提交进去。
   已重写为完整的生命周期冒烟（草稿不可见 → 同名可重生成 → 发布 → 反过来拒绝遮蔽 →
   草稿试玩带 `draft` 标记 → 删除只动草稿区），并**两个区域都清**。
2. **`scripts/replay.py` 会直接崩**：它用 `iterdir()` + `load_worldpack()` 找包，
   第一个候选就是 `_drafts/` → `WorldPackError: 缺少文件: world-packs\_drafts\world.yaml`。
   **pytest 盖不到它**（脚本不在测试范围内），是"改路径要连带查谁在断言它"这条纪律
   全仓搜出来的。已改用 `catalog.list_packs()`——即**生产代码回答"有哪些卡"的那个函数**。

全仓复核了 6 处 `world-packs/` 遍历点：4 处本来就按 `judge_corpus.yaml` 过滤（草稿没有语料，
天然安全），2 处（上面这两个）真的坏了。

**为这类坑补的守卫**：`tests/test_catalog.py::test_world_packs_enumerators_all_discriminate`
——登记每个遍历者"靠什么区分目录与卡"，并扫描 `scripts/` + `game_agent/` 里所有同时出现
`world-packs` 与 `iterdir(` 的文件，**新的遍历者不登记就失败**。要求登记不是官僚：
这个坑的成因正是"写遍历时没想过那个目录会有第二种"。

> 这个守卫自己先假绿过一次，值得记：第一版用**原始文本**检查判据，于是把
> `catalog.list_packs` 换成一个不存在的函数、只在上面留一句提到它的**注释**，守卫照样绿。
> 改成 `ast.unparse` 后注释被丢掉，变异立刻变红。**注释能满足的守卫等于没有守卫。**
> （顺带撞上 `card_hook_check.py` 带 UTF-8 BOM，`ast.parse` 需要 `utf-8-sig`。）

#### 另一处（测试基建）

`web._make_game` 加了 `draft` 关键字入参后，`test_web_frontend.py` 里那个按关键字
接收参数的假件没跟上 → `TypeError` → 500，症状是**五个与草稿毫无关系的前端测试**
报"读取状态失败"。补了一个 `test_make_game_signature_is_pinned` 把签名钉住：
下次漂移会得到一句指名道姓的失败，而不是五个莫名其妙的 500。

#### 验证

- 离线 **1048 项全绿**（`test_drafts.py` 17 项为新）。
- **变异验证**（三处，都在还原后复跑全绿）：
  1. 把 `list_drafts` 改回 `_is_pack_dir` 判据 → 恰好那 3 条覆盖该缺陷的守卫变红
     （`[bad_yaml]` 参数化分支照常绿，因为它们不依赖该判据）；
  2. 给 `web._make_game` 加一个入参 → 签名钉子变红；
  3. 遍历者登记守卫**两个方向都验**：把 `replay.py` 改名冒充新遍历者 → 报"没登记判据"；
     把它的 `catalog.list_packs` 换成别的函数（注释里仍提到该名字）→ 也变红
     （这一条第一次做时是**假绿**的，见上文"注释能满足的守卫等于没有守卫"）。
- **真机冒烟**：`scripts/worldgen_smoke.py` 29 项全过，1 项**不可判**（离线生成仅 0.11s，
  HTTP 链路上的流式增量性测不出——如实记不可判，不冒充通过）。
  冒烟产物已两区清理干净（`_drafts/` 复查为空）。

---

### 6.7 批次 6 执行记录（2026-10）：创作工作台的共享外壳（N2a）

**交付**：`webui/src/views/StudioView.vue`（工作台）+ `composables/useJobStream.js`
（进度流）+ `api/client.js` 8 个端点 + `LibraryView` 入口 + `App.vue` 三视图路由
+ `styles.css`；守卫 +4；**真机浏览器冒烟 18 → 36 项**。

**逐条推理见 `docs/roadmap.md` §2.3**（含"三次假绿"与"桩会同意我"两条教训），
这里只记与本文档其他章节的呼应：

1. **§4.2 的"右栏是呈现不是表单"落成了一条可执行断言**——冒烟直接断言
   "工作台右栏输入框数量 = 0"。设计决定写在文档里会被后人改掉，写成断言才会拦下来。
   这同时把 §5.1 那句"工作台 2：编辑器（左边对话、右边字段与校验报告）"的
   **右半**先立起来了：等 N6 把左半（对话）接上，就是那张图本来的样子，**不用返工**。
2. **§3.1 的成本口径第一次出现在 UI 里**：`--offline` 从 CLI 开关变成界面上的
   「离线试跑」勾选框，且**默认勾上**（真实生成一次约 ¥0.1–0.3）。取消勾选时显示
   醒目成本提示——§3.2 ② 那条"调用方负责在点之前把这件事说清楚"现在真的有人负责了。
3. **N1 的事件留档第二次兑现**：报告栏里的"修复轮报错原文"来自事件流的 `repair` 事件，
   **刷新页面后仍能回放**。第一次兑现是后台任务自身的断线重连。
4. **进度流的断线语义与回合流相反，两者结论不可互搬**：

   | | 回合流（POST） | 任务进度流（GET） |
   | --- | --- | --- |
   | 断线重连 | 会把回合重跑一遍（重复扣费）→ 禁止 | 只是重新订阅，不重跑任何东西 |
   | 重连后收到 | 新的一回合 | 服务端**先回放全部历史**再续播 |

   所以这里仍然不用 `EventSource`（无状态自动重连会重收历史），而是有上限的显式重连；
   且重连时从空列表重建日志——**"重复行"在结构上不存在，不需要去重代码**。

---

### 6.8 批次 7 执行记录（2026-10）：创作者 Agent（E-8，能力 C）

**交付**：`game_agent/creator.py`（工作版 + **11 个工具** + 修复循环 + diff）
+ `catalog.fork_to_draft` + `LLMClient.complete_with_tools` + `Settings.creator_model`
+ `UsageTracker(pack=…)` + 4 个端点 + 前端对话面板/两模式页签/diff
+ `tests/test_creator.py`（33 项）+ `scripts/creator_smoke.py`（真机对话冒烟）。

**逐条推理见 `docs/roadmap.md` §2.4**，这里只记与本文档其他章节的呼应：

1. **§4.1 的判断被真机验证**：另起一条轻量循环是对的。真实模型在这一轮里
   `read_npc → list_npcs → read_npc → update_npc_field → validate_pack`，
   9 帧 / 4.7s——它**先读再改、改完自己校验**。若当初把创作者任务塞进 `run_turn`，
   `submit_narration` 那套收尾协议会把它逼成"必须写一段叙事"，而它的产出根本不是叙事。
2. **§4.2 的"`validate_pack` 是整个设计的支点"成立**：工具面的第 10 个工具就是它，
   报错原文回灌给模型；而服务端在 `done` 里给**权威**校验结论（不采信模型自报）
   ——两边同源（都走 `load_worldpack`），所以不存在"Agent 说通过了、发布被拒"。
3. **§4.2 的"工作者面向工作版，原始包只读"落成了一条真机断言**：
   冒烟在对话前后各取一次 `pack_digest`，必须一模一样。
4. **§5.1 那张图现在字面成立**：工作台 2 = 左对话（中栏）+ 右字段与校验报告（右栏），
   且右栏**始终没有输入框**（浏览器冒烟直接断言，与 §6.7 同一条）。
5. **N6 的前置（按包归因的成本轴）在开工时就落地了**：`purpose="creator"` +
   `session` + `pack` 三个轴，账本按卡分开（`saves/usage-creator-<name>.jsonl`）。
   所以"改这一版花了多少钱"现在就能答——真机冒烟实测 6 次调用 13,822 tokens。
6. **一条关于冒烟指令的教训（已升为 roadmap §4 纪律 8）**：
   第一版冒烟用"把**主角**的说话风格改简短点"（歧义：该包里"主角"是玩家、没有角色卡），
   模型**拒绝猜**并反问——**那是设计意图，不是缺陷**，坏的是指令。
   冒烟指令的歧义会被记成产品缺陷。
7. **§4.2 那张表最终上线 12 个工具**（原表 10 行 / 11 个工具，补了 `start_generation`）：
   "与 Agent 对话，**从素材制作新卡**"这句需求原先只有表单路径满足，现在对话里也能。
   Agent 不代跑那条分钟级管线，而是**转交**（起任务 + 推 `job` 事件让工作台自动切到
   进度页签 + 让模型别等）。工具未接入时以 `disabled_msg` 明确拒绝，挡的是最坏的失败：
   模型凭空答应"我这就做"、然后什么也没发生。
8. **真机通关暴露了两个引擎 bug（已修）与一个生成质量缺口（立为 N12）**：
   见 `docs/roadmap.md` §2.5。前者是**关键抉择期的卡死**（视图与引擎状态自相矛盾：
   `say`/`act`/`end_day` 全拒，而界面上没有选项可点）；后者是"生成的卡结局阈值
   没有任何确定性增益路径能达到"（15 天好感只到 13、结局要 50），
   而数值可达性门禁因结局不带 `day` 门槛被**整体跳过**。

---

## 7. 交付顺序与工作量（人日，粗粒度）

> ⚠️ **进度已并入 `docs/roadmap.md`**（2026-10）。那张表是"下一步做什么"的唯一答案；
> 本表保留作**执行记录与验收口径**，不再单独维护状态。

| 阶段 | 内容 | 估算 | 状态 |
| --- | --- | --- | --- |
| 0 | E-1 / E-2（两个已知缺陷） | 2–3 | ✅ **已完成** |
| 1 | **Stage A 前端拆分** + E-3 / E-4 / E-9 | 3–4 | ✅ **已完成** |
| 2 | **能力 A 打通**：卡片库 + 会话选包 + 自由/剧本开关 + **Stage B/C 三栏界面** | 6–10 | ✅ **已完成**（真机冒烟 18/18） |
| 3 | **能力 B**：worldgen 服务化 + 后台任务进度 + 草稿/发布闸门 + 导入界面 | 6–9 | ✅ **已完成**（`worldgen.py` + 后台任务 SSE + 草稿区/发布闸门 + **N2a 创作工作台**，见 §6.5/§6.6/§6.7） |
| 4 | **能力 C**：创作者 Agent + 工具面 + 校验修复循环 + 编辑器工作台 | 6–9 | ✅ **已完成**（`creator.py` + 11 个工具 + 修复循环 + 对话面板 + diff，见 §6.8 / roadmap N6） |
| 5 | 分发（entry point / `asset://` / MCP 加固） | 2–3 | ⬜ 见 roadmap N11 |

**合计约 25–38 人日**，其中前端约占一半。**已完成约 21–27 人日**（阶段 0/1/2 + 能力 A/B/C）。
**三条能力全部完整可用**：① 选卡 → 三栏游玩 → 存档读档；② 贴素材 → 边跑边看进度 →
看校验报告 → 草稿试玩 → 过闸门发布；③ **和 Agent 对话改人物设定与世界书**。

> **三条能力已全部收口；剩下的都是"周边与纵深"**（roadmap N4/N5/N7–N11）：
> 存档点与回退、前后台分离、ST 内容导入、harness 四条改进、分发。
> **其中 N7（存档点/回退/分支）是玩家侧最大的体验缺口**，若更在意"玩起来爽"可优先。

> **能力 C 也已收口**——至此 §4.2 那张工具表上线的有：
> `read_world` / `read_npc` / `list_npcs` / `read_lore` / `update_world_field` /
> `update_npc_field` / `add_npc` / `remove_npc` / `upsert_lore` / `validate_pack` / `diff_pack`。
> 当初 §4.2 设想的"工作台 2：编辑器（左边对话、右边字段与校验报告）"现在是**字面成立**的：
> 中栏是对话（N6）、右栏是字段与校验报告（N2a + N6 的 diff），并且右栏**始终没有输入框**
> （编辑权归 Agent，这条由浏览器冒烟直接断言）。
> **具体排期见 `docs/roadmap.md`，边界论证见其 §1.1。**

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
