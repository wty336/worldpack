# 对标 dsh-tavern：差距分析与改造方案

> **分析基准**
> - 参照物：`flizzywine/dsh-tavern` @ `main`（package `dsh-profile-tavern` v2.4.0），本地只读 checkout 于 `%TEMP%\dsh-tavern-ref`。
>   实测规模（非空行 / 物理行两套口径都记，下文表格统一用非空行）：
>   服务端 `tavern-plugin/lib/**`（不含 `vendor/` 与生成的 `client.js`）**310 文件 / 47,888 行（物理）**，
>   其中 `lib/domain/` **283 文件 / 36,196（非空）/ 38,111（物理）行**；
>   客户端源码 `tavern-plugin/src/client/` **73 文件 / 15,379（非空）/ 15,704（物理）行**；
>   两者合计即**第一方产品面 ≈ 63,700 行**。测试 **622 文件 / 51,650 行（物理）**，文档 **270 文件 / 28,158 行**。
>   **仓库 61.97% 是 vendored 代码**（`packages/dsh-better-sidebar` 一项就 302,825 行），
>   加上生成的 lockfile 与 `client.js` 后达 **66.4%**——第一方原创只剩 **33.6%**。
>   `lib/vendor/` **327 文件 / 56,171 行**（pinned 上游 ST 模板引擎 + MVU）；全仓 2,065 文本文件 / 633,259 物理行。
> - 本项目：引擎 `game_agent/` **31 模块（33 个 .py）/ 8,358 行**（非空行；物理行 9,548），测试 **79 文件 / 12,231 行（物理 15,731）/ 828 个 test 函数**，世界包 8 个（每个 0.9–2.9K 行 YAML）。
>   *口径说明：本表两侧行数统一取 `Measure-Object -Line`（**非空行**），两侧可比；引用物理行数时另行标注。*
> - 宿主：本地 `@deepseek-ai/dsh` **0.1.5-rc.3**；dsh-tavern 锁定 **0.1.5-rc.2** 并自带 patch 层。
>
> **本文的定位**：回答"要不要、以及怎么把本项目做成 dsh-tavern 那种形态"。
> 它不改写 `docs/design.md` 的决策，与 `docs/sillytavern-borrow.md`（ST 源码评审）、
> `docs/plan-creator-player.md`（剧本平台化）、`docs/plan-runtime-platform.md`（平台化三期）是同一条线的继续。

---

## 0. 结论速览

**一句话**：dsh-tavern 和本项目不在同一层——它 ≈ **80% 宿主平台 + 生态兼容**、20% 游戏运行时；本项目 ≈ **90% 游戏运行时**、10% 外壳。
所以"做成 dsh-tavern 这种"**不能照抄**，照抄等于把本项目唯一的护城河（代码掌握真值 + 828 项测试）扔掉去换一个 RC 阶段的宿主。

**推荐**：**保持引擎为权威运行时不动**，按期 0–5 把"平台层 / 生态层 / 分发层"这三块短板补齐（§5）。
其中**收益最高、成本最低的三件事**是：

1. **重写前端**（原为 `web.py` 里一个 250 行的 `INDEX_HTML` 字符串常量，对照 dsh-tavern 的 15.4K 行客户端）
   —— ✅ **已完成**：Vite + Vue 3 三栏界面 + **创作工作台**（含创作者 Agent 对话面板），
   产物入库，真机浏览器冒烟 **50/50**；
2. **把 `runlog` 的回合 checkpoint 提升为玩家可见的存档/回退/分支**（数据结构已经在，只差产品化）
   —— ⬜ **仍未做**，是**目前最大的玩家侧缺口**（roadmap **N7** / 期 2 / 差距 G-2 ⭐ P0）；
3. **实现 `sillytavern-borrow.md` §E-1 的人物卡/世界书导入映射表**（表已写好，只差代码）
   —— ⬜ **仍未做**（roadmap **N9** / 期 4 / 差距 G-4 ⭐ P1）。

**当前达标情况（2026-10，回填）**：用户那句需求原文拆出的四件事——
① 选一张卡自由游玩 ② 绑定小说/剧本/大纲并沿主线推进 ③ 与 Agent 对话从素材做新卡
④ 对话改人物设定与世界书——**四件都已打通并有真机证据**（见 §4 的 G-1/G-5 与
`plan-tavern-shaped-product.md` §6.2/§6.6/§6.7/§6.8）。
**但"dsh-tavern 那种成熟度"还没到**：G-2（回退/分支，P0）、G-3（一主多子体感，P1）、
G-4（生态内容导入，P1）、G-8/G-9（分发与插件缝，P2）四块仍在。
也就是说：**功能面能演示、能自己玩；产品面还差一层。** 这一层里最该先做的是 G-2。

**明确不推荐**：把引擎移植成 DSH 插件（TS/cordis 重写）。理由见 §3.1——**架构上敌对 + 宿主明确禁止你拥有回合循环**，且它仍在 RC。
**一个需要你拍板的例外**：把 DSH **只当 UI 壳**（客户端插件 + 引擎留在 HTTP 另一侧）是**真实可行**的，
不威胁护城河；但它只适合"聊天页 + 侧栏"形状，全屏游戏布局会落到未文档化的 DOM 逃生口上（§3.3）。

| 维度 | dsh-tavern | 本项目 | 判定 |
| --- | --- | --- | --- |
| 状态真值与审计 | 台账（ledger）+ 结算事务 | stats/flags/counters/items/appointments + `after == before + delta` 审计 | **本项目领先** |
| 质量门与测试 | 51K 行测试 | 828 个离线测试 + L1/真机四级门禁 | **本项目领先**（就"可判定对错"而言） |
| 上下文工程 | 稳定前缀 + 每轮注入 + 后带 | 静态前缀字节级冻结 + 比例制 lore + 压缩 | 各有胜负 |
| 宿主平台 | DSH 白拿（会话/模型/UI/插件/压缩/token 表） | 自建（FastAPI + SSE，单进程单包） | **本项目大幅落后** |
| 生态兼容 | ST 卡/世界书/预设/正则/MVU/Helper 脚本 | 无（D 系列论证过大部分不该抄） | **本项目落后（但取舍正确）** |
| 产品与分发 | 安装器 / Android APK / 文档站 / Discord | `uv sync` + 源码 | **本项目大幅落后** |

---

## 1. dsh-tavern 的真实结构：它是"宿主平台 + 一个领域插件"

这一节是全部结论的地基。**dsh-tavern 的绝大部分能力不是它自己写的，是 DSH 给的。**

### 1.1 它是一个 DSH Profile，而不是一个独立应用

`package.json` 顶层：

```json
"dsh": {
  "profile": { "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app",
                            "dsh-web-mobile", "dsh-better-sidebar", "dsh-dream-skin",
                            "dsh-tavern-plugin", "dsh-tavern-remote"] },
  "bundle": { "patch": "./plugin.patch.yml" }
}
```

`plugin.patch.yml`（578 行）是 profile 的**编排补丁**：它按 `id` 覆盖/插入/禁用 DSH 的"行"（row）。
其中真实的领域插入只有最后几行：

```yaml
- insert:
    - id: dsh-tavern
      name: ./tavern-plugin/lib/index.js
      inject: [fs, llm, webServer, tools, skills, agentDefaultModel,
               sandboxPolicy, shell, agentPresets, settings, tokenMeter]
```

**其余 570 行在复用 DSH 的原生能力**，例如：`session-persistence-jsonl`（会话持久化）、`storage-json`、
`dsh-compaction-basic` + `dsh-command-compact` + `dsh-compaction-tool-result-pruner`（上下文压缩）、
`dsh-token-meter`（token 计量）、`dsh-web-app`（Web 服务与前端构建）、以及 `ui-*` 共 **60+ 个浏览器插件**（聊天、侧栏、设置、工具树、审批、goal、jobs……）。

### 1.2 Agent 本体也是"配置"出来的，不是写出来的

`presets/tavern/agent.cordis.yml` 声明这个 Agent 由哪些插件组成：
`@deepseek-ai/dsh-persona`（人格）、`dsh-fs-local` + `dsh-tool-str-replace-editor`（文件）、
`dsh-tool-skill`（技能）、`dsh-compaction-basic`（压缩）。

> 也就是说：**dsh-tavern 没有自己实现 agent loop**。循环、会话、上下文装配、压缩、模型路由、重试、UI 全部来自 DSH。
> 它实现的是**酒馆领域**：人物卡、世界书、剧本、候选、MVU、剧情时间线、脚本运行模块、场景配图。

### 1.3 它与宿主的关系是"贴上去的"，而且是脆的

`plugin.patch.yml` 里有一段自述值得逐字读：

> "Tavern's card workspace is an explicitly writable Agent surface, so restore the cross-platform primitives here instead of replacing them with task-specific RPCs."

以及 `CONTEXT.md` 的 **Host Projection Replay**：

> "该层依赖宿主私有接口，升级需跑原生差分测试，不直接修改已安装 DSH。"

`docs/adr/0007-bundle-and-pin-dsh-runtime.md` 的决策是：**DSH 是内置的固定组件，打包时不跟随上游升级**。
`lib/vendor/` 里 29.4 MB 是 vendored 的上游产物。**这些是"宿主还在动"的成本证据，不是它的优点。**

### 1.4 领域代码的重心在"兼容"，不在"游戏"

`lib/domain/` 282 个文件按前缀归类（实测文件数 / 行数）：

| 前缀族 | 文件 | 行数 | 性质 |
| --- | --- | --- | --- |
| `mvu-*` | 21 | 3,291 | MVU 协议：解析、草稿、结算、协调、诊断、大状态 |
| `scene-image-*` + `scene-*` | 23 | 1,735 | 场景配图：多 provider、队列、参考图、风格 |
| `worldbook-*` | 16 | 1,707 | 世界书：激活、BM25、筛选、合并、放置、随机、召回 |
| `tavern-helper-* / -script-* / -macro-*` | 8 | 1,977 | 第三方脚本运行与宿主适配 |
| `card-*` | 12 | 1,180 | 人物卡：读取、校验、组织、开局、工作台 |
| `template-*` / `*prompt-template*` | 10 | 489 | 上游 EJS 模板引擎 |
| `sillytavern-*` | 3 | 321 | 兼容模式 / CSS 兼容 / 严格工具 |
| **游戏运行时核心族**<br>`turn-orchestration` / `story-timeline` / `story-ledger` / `context-planner` / `session-stable-prefix` / `background-*` / `foreground-*` / `settlement-*` / `candidate-*` / `conversation-*` / `session-*` | **49** | **6,437** | 回合编排、剧情时间线、台账、上下文规划、稳定前缀、前后台、候选 |
| （其余未归类） | 137 | ~19,000 | 压缩、存档迁移、投影、诊断、资源、状态栏等 |

**结论：真正可与本项目对标的是那 49 个文件 / 6.4K 行；`mvu-*`、`scene-*`、`worldbook-*`、`tavern-*script*`、`template-*` 合计 78 文件 / 9.2K 行全是生态兼容与配图。**
其中 `mvu-*` 一族（3,291 行）体量最大——而它存在的理由恰恰是"酒馆没有权威状态"（§6 因此明确不借）。

---

## 2. 两边独立收敛到同一个结论（这是最重要的发现）

dsh-tavern 的 `docs/design/sillytavern-architecture-ceiling.md` 写道：

> "SillyTavern 的核心运行模型是组织上下文、发送文本、接收文本和展示文本。它没有原生的 Agent 控制面、
> 结构化工具事务和权威领域状态。……社区只能把本应属于运行时的能力编码进人物卡、世界书、预设、正则、MVU、EJS……
> 这不是社区缺少设计能力，而是底层能力决定的架构上限。"

`docs/design/tavern-capabilities-in-llm-harness.md`：

> "这是一种 **LLM 混合程序**。它同时依靠两类计算：LLM 语义计算 …… Harness 确定性计算 ……"

而这就是本项目 README 的第一句话：**"模型只管生成，代码掌握真值"**。

更关键的是 `docs/design/rethinking-tavern-ecosystem-as-llm-games.md` 给出的映射表——
它的每一行，本项目**都已经有了对应物**：

| 酒馆生态概念 | dsh-tavern 提出的"原生 LLM 游戏概念" | 本项目的对应物 |
| --- | --- | --- |
| Swipe、回退、重生成 | 存档点、世界线与时间线 | ⚠️ **只有 `runlog`（离线实验用），没有玩家面** |
| 人物卡 | 人物集与角色社会 | ✅ `npcs/*.yaml` |
| 世界书 | 知识与上下文披露系统 | ✅ `lore` + `select_lore` + `query_world` |
| 状态栏、小手机、HTML | 游戏 View 与交互界面 | ⚠️ 只有状态文本（`_view`） |
| 数据库、摘要、检索 | 世界连续性与记忆系统 | ✅ `memory.py` + `compression.py` + `factgraph.py` |
| MVU、变量宏 | 状态、事件与效果事务 | ✅ `state.py` + `ToolRegistry` + 审计不变量 |
| 预设 | 叙事策略与 Agent 编排 | ✅ `context.py` 静态前缀 + `ENGINE_RULES` |
| 正则、EJS、Helper 脚本 | 受控程序能力与兼容适配器 | ✅ `conditions.py` 声明式 DSL（且**不执行作者代码**） |
| 人物卡及其配套资源 | 可版本化的原生游戏包 | ✅ `world-packs/` + `check-worldpack` |

**这张表说明：本项目在领域模型上并不落后，反而在"权威状态 / 事务 / 校验"这一列上更超前。
落后的是表以外的两列——宿主平台与产品分发。**

---

## 3. 路线与取舍

> **先说一条贯穿全部路线的事实**：DSH 从设计上**不允许插件拥有回合循环**
> （`dsh-agent-loop` 是宿主单例，注册第二个直接抛错；`tools`/`systemPrompt`/`agents`/`sessions`
> 注册表都在 host plane，不可按会话替换）。
> 于是任何"接进 DSH"的方案都自动分成两类：**交出循环**（A / B）或**只借它的表现层**（C′）。
> 本项目的护城河全部押在"自己掌控请求装配与回合循环"上，所以 A / B 不应作为主干。

### 3.1 路线 A：把引擎移植成 DSH 插件（TS / cordis 重写）——**不推荐**

- 成本：需重写 8.4K 行引擎 + 让 828 个测试的**语义**在新栈上重建（测试不能直接搬）。
  dsh-tavern 用 57K 行原创新代码 + 51K 行测试才走到今天，其中 20K+ 行是兼容层。
- **架构敌对（决定性理由）**：本项目最贵的三样东西——**静态前缀字节级冻结（自述 KV 命中 99.3%，
  但该数字目前只存在于 gitignored 的 `docs/resume-entry.md:16`，见 §7 风险 5）、
  每轮校验后落盘的审计不变量 `after == before + delta`、内轮自校正与 Judge 门禁——都要求自己掌控
  请求装配与回合循环**。一旦把循环交给 DSH，这些保证就变成"取决于宿主行为"，护城河当场消失。
- **宿主明确禁止你拥有回合循环（硬证据，不是推测）**：`dsh-agent-loop` **"registers the one agent factory and
  throws on a second"**；`tools` / `systemPrompt` / `agents` / `agent-loop` / `sessions` 这些注册表
  以及跨会话的持久化、存储、设置、凭据、沙箱/审批**都属 host plane，按设计不可按会话替换**
  （`dsh-agent-presets/.../editing-cordis-compositions/SKILL.md:20-24, 163-166`）。
  插件只能**向**这些注册表贡献，**不能拥有**回合循环。
  **接入深度还远超"挂个插件"**：dsh-tavern **前台根本不调用模型**——
  DSH 自己的 Agent loop 发起请求，Tavern 只是在三个 hook 上把它**中途改写**：
  `agent/pre-step`（装入帧、裁剪消息）、`system-prompt/assemble`（**整体替换** `assembly.sections`）、
  `llm/stream`（重写 provider 请求）；它的 adapter 把游戏帧伪装成一条 `role:'user'` 的合成消息塞进宿主会话。
  **也就是说：采用这条路，你的回合循环不是"被 DSH 托管"，而是"被 DSH 在飞行中拦截并改写"，
  而且宿主从设计上就不允许你把它拿回来。**
  本项目的静态前缀字节级冻结、每轮 `after == before + delta` 落盘、`llm.run_turn` 的
  "截断即整批作废"判定，都得重新表达成一组宿主 hook 投影——**而这些语义恰恰是宿主不认识的**。
- 平台风险（**已取证，比"RC"这个说法严重得多**）：dsh-tavern 是**精确版本锁死 + 打补丁**，不是"插件兼容"：
  - `config/dsh-compatibility.json` → `"adaptedDshVersion": "0.1.5-rc.2"`；
  - `bin/dsh-compatibility.mjs:26-32` `assertCompatibleDshVersion` **要求宿主版本字符串精确相等**，否则抛错；
    `tavern-plugin/lib/domain/host-compatibility.js` 也只在精确相等时返回 `'verified'`。
    **本机是 `0.1.5-rc.3`，因此 Tavern 的守卫在这里会拒绝挂载**——同一个小版本内的 rc 差异即不可用。
  - 它必须在内存里**猴补宿主内部实现并对 8 个宿主文件做 SHA-256 校验**：
    `host-session-patch.js`（放宽 `@deepseek-ai/dsh-session` 的消息替换/影子校验，好让编辑/回退/重生的 tombstone 被接受）、
    `host-subprocess-patch.js`（往 `dsh-subprocess-local` 注入 `windowsHide:true`）；
  - 还有 `patches/dsh-better-sidebar@0.19.1.patch` / `dsh-pocket@2.10.6.patch`、vendored 19KB 官方 web-app patch 层、
    以及 29.4 MB vendored 上游产物。
  **结论：在这样一条"精确版本 + 猴补 + vendor"的路径上重建全部资产，风险与收益完全不成比例。**

### 3.2 路线 B：引擎不动，DSH 当外壳（MCP / 薄插件桥接）——**只能当出口，不能当产品**

- 做法：一行 profile patch 挂 `@deepseek-ai/dsh-mcp-client`，把 Python 引擎的工具桥成
  `mcp__<server>__<tool>`（stdio 或 streamable-http，带重连与 tools/list 变更重同步）；
  或写一个 ~100 行宿主插件把引擎当子进程包装。DSH 白送聊天壳、会话、持久化、压缩、模型路由。
- **MCP 只桥工具，这是硬边界**：`dsh-mcp-client` 明确 "Resources and Prompts have no harness consumer
  mechanism and are deferred"；**没有"引擎→浏览器"的流式通道**，调用是请求/响应且默认 60s 超时；
  富 UI 拿不到（只有 DSH 的通用工具卡片）。所以**用 MCP 做不出游戏前端**。
- 那剩下什么：把引擎降级成"模型可以调的工具"，**节奏归 DSH 的模型**。
  代价是丢掉回合编排、Judge、压缩、静态前缀——即 §3.1 列的全部资产。
- 保留价值：作为**外接出口**很有意义（见 §5 期 5）——让别人（包括 DSH）能把本项目当工具面调用，
  而不必交出方向盘。

### 3.3 路线 C′：把 DSH 只当 UI 壳（客户端插件 + 自己的后端）——**真实可行，但对"游戏 UI"这个形状合不上**

这条我原先判为不可行，**是错的**——已由活动 profile 里的第三方源码证实：

- 客户端插件就是浏览器里的 cordis 插件（`inject` + `apply(ctx)`），往命名槽位注册 **React 组件**：
  实测存在的槽位有 `settings.section`、`conversation.chat.turnTail`、`conversation.input.dock`、
  `sidebar.right.pane.tab`、`sidebar.footer.action` 等。
- **浏览器侧没有沙箱**：是普通页面 JS，有完整 `window`/`document`，能 `appendChild` 到 `document.body`、
  挂自己的 React root、甚至猴补 `window.fetch`/`WebSocket`/`EventSource`
  （`@linxin666/dsh-remote-web-ui` 就是这么干的）。
- **访问你的 Python 后端完全不需要宿主介入**：直接 `fetch('http://127.0.0.1:8000/...')` 或自开 WebSocket
  （页面 origin 是 `http://127.0.0.1:3080`，CORS 自理）。**引擎留在 HTTP 边界另一侧，护城河不受威胁。**
- 打包是一个 npm 包（`dsh.bundle.patch` 一个空操作行 + `dsh.client` + `exports["./client"]`），
  `dsh plugin add` 安装，`tsdown --watch` 可 HMR。

**但我仍然不把它列为主干，理由是具体的、针对"游戏 UI"这个形状的：**

1. **没有"整页路由"贡献点**。官方槽位 API 里拿下一整块区域的唯一姿势是注册到高层槽位，
   而指南明确警告：替换根级占用者会**连带移除它声明的所有后代槽位**。
   **你要的不是"聊天页里加个面板"，是全屏游戏界面（正文 + 状态栏 + 候选项）——正好落在最不合的形状上。**
2. **第三方实际用的逃生口是直接挂 DOM**（`document.body.appendChild` + React root）——
   而报告的原话是：**"这不是文档化 API，因此也是最可能在宿主升级时坏掉的东西。"**
3. **宿主有删除"面"的前科**：本机 `~/.dsh/profiles/node_modules/@deepseek-ai/` 里存在**指向已不存在包的悬空 junction**
   ——`dsh-client-runtime`、`dsh-client-ui-slots`、`dsh-client-ui-primitives`、`dsh-client-web`、`dsh-host-apiproxy`。
   第三方 `@mlgbnb/dsh-archive-manager` 因为 import 了被删的 face，**会直接让 `dsh web` 启动失败**。
4. **槽位目录无法预先枚举**：`dsh-client-ui-slots` 在本机根本没装，权威来源是运行时 provider
   `Slots.listSubTree`（需要一个活的宿主）。**你没法在动手前把 UI 方案规划完。**
5. 还有两条工程约束：DSH 的 `--host 0.0.0.0` **不支持**（仅回环），
   且官方 GUI 里的插件面板是**只读**的，安装必须走 CLI。

**判断（把取舍说清楚，这是你需要拍的板）**：
如果你愿意**持续跟宿主升级**、且能接受界面被约束在"聊天页 + 侧栏"的槽位形状里，
C′ 能省下大量前端工作量，且不碰引擎——**这对"快速拿到一个像酒馆的界面"是真实的捷径**。
但如果你要的是**全屏游戏布局**（本项目的 `_view()` 数据形状本来就是为此设计的），
你会被推到那条未文档化的 DOM 逃生口上——**那就等于把产品最显眼的一层押在一个已被证明会删面的 RC 宿主上**，
与本项目"字节级冻结 + 828 项测试全绿才合并"的纪律直接冲突。
**我的建议：C′ 作为"要不要先做个壳试试"的可选实验可以，不作为主干排期。**

### 3.4 路线 C：引擎不动 + 自建产品层 —— **推荐主干**

保留 Python 引擎的全部资产，把 dsh-tavern 的**产品形态**（不是它的技术栈）作为目标：
真正的前端、玩家面的存档/回退/分支、前后台分离、内容导入、可分发。

> 本项目的 `docs/plan-creator-player.md` 已经拍板同一方向：
> **"这不是两个产品，是同一个引擎的两个准入面……引擎零改动是硬约束。"**
> 本文与它一致，只是把"对标 dsh-tavern"补进来的四件事排进同一个序列。

### 3.5 路线 D：C 为主 + 保留 MCP 出口 —— **最终推荐**

`game_agent/mcp_server.py` 已经存在，且 `ToolRegistry.to_openai(include_internal=False)` 已经
把"协议工具"和"玩家可见工具"分开——**外接宿主的路已经铺好一半**。
不需要为它现在投入，但要保证它不被破坏。

---

## 4. 差距清单（按可执行粒度）

标 ⭐ 的是"数据/设计已在、只差产品化"的高性价比项。

| # | 差距 | 现状证据 | dsh-tavern 的对应实现 | 优先级 |
| --- | --- | --- | --- | --- |
| G-1 ⭐ | **前端是原型级** —— ✅ **已重写（2026-10）** | 原为 `web.py:344` 的 248 行内联字符串 → Stage A 拆成真实文件 → **Stage B/C 用 Vite + Vue 3 重写为三栏**（左本局 · 中正文+输入 · 右状态栏），`dist/` 入库并由 FastAPI 托管。守卫：`tests/test_web_frontend.py`（接口契约 + 源码结构 + 前端端点与后端路由一致性）+ `tests/test_webui_build.py`（产物完整性/资源路径/陈旧度）+ **`scripts/webui_smoke.mjs` 真机驱动界面 50 项**。**未做**：跨设备响应式打磨、多语言 | `tavern-plugin/src/client/` 72 文件 15,379 行；`play-controls.js` 95KB、`sidebar.js` 81KB、`card-library.js` 38KB；宿主另有 60+ `ui-*` 插件 | ✅ **已完成** |
| G-2 ⭐ | **玩家面没有存档/回退/重生成/分支** | `game.py:642 _txn_rollback` 只是**轮内事务**；`runlog.py` 有回合级全量 checkpoint，但写在 `runs/`（`.gitignore` 内）、定位是离线 replay | Story Timeline：revision / branch / checkpoint；ADR 0004→**0006**（前台先提交，后台派生的结算失败不阻塞、不撤销正文，过期结果按 branch+revision 丢弃） | **P0** |
| G-3 | **一轮里什么都干**（正文 + 状态 + 候选 + Judge 串行） | `game.py` 单回合内串行完成 | "一主多子"：前台主 Agent 只写正文；**共享一个后台 Agent** 做候选生成与状态结算（ADR 0002），前缀各自稳定 | **P1** |
| G-4 ⭐ | **无法消费酒馆生态内容** | 无 ST 导入路径（`import_story.py` 无 `--format`）；A 系列已实现，**E 系列未实现**；MVU 全仓 0 命中。`.st-inspect/` 已按 2026-10 决定加入 `.gitignore`（上游 SillyTavern 1.19.0，仅作只读调研） | PNG/JSON 人物卡、世界书、预设、正则、MVU、Helper 脚本全兼容 | **P1** |
| G-5 | **单进程单包**，无目录层 —— ✅ **已修（2026-10）** | 原为进程级 `GAME_WORLDPACK`（换包要重启）；现 `game_agent/catalog.py` 提供目录层，`POST /api/new{pack_id, mode}` 做**会话级绑定**，`GET /api/catalog` 与 `GET /api/{sid}/meta` 支撑选卡屏；环境变量降级为默认值。守卫 `tests/test_catalog.py`（15 项） | 人物卡库 + 剧本库 + 会话级绑定 | ✅ **已完成** |
| G-6 | **记账串号**（数据错误）—— ✅ **已修（2026-10）** | 原为 `_WEB_TRACKER` 全进程单例共写 `usage-web.jsonl`；现**每会话一个账本** `saves/usage-<sid>.jsonl` + 条目带 `session` 轴 + `GET /api/{sid}/cost`。守卫 `tests/test_web_accounting.py`（8 项，含"进程级单例不得回归"钉名字守卫） | 按会话隔离 | ✅ **已完成** |
| G-7 | **存档无剧本身份戳** —— ✅ **已修（2026-10）** | `save_game()` 原只写 `state.to_dict()`；现 `save_version` 2→3 并落 `pack: {id, digest}`，读档不一致抛 `PackMismatchError`（拒绝并提示，且在构造 `GameState` 之前）。守卫 `tests/test_pack_identity.py`（14 项） | 存档带版本 | ✅ **已完成** |
| G-8 | **无法分发** | `pyproject.toml` 无 `console_scripts`；无发布；无安装器 | EXE / APK / `install.ps1` / `dsh-tavern update` / 文档站 | **P2** |
| G-9 | **无插件/扩展缝** | `conditions.py` DSL 只能被引擎消费 | cordis 插件系统 + `dsh.client` 浏览器插件 + 用户 skill 目录 | **P2** |
| G-10 | 无场景配图 / 无小手机 / 无 MVU | 无 | `scene-image-*` ~25 文件 + 多 provider | **P3（建议不做）** |

---

## 5. 推荐路线图

> 原则：**引擎零改动是硬约束**（沿用 `plan-creator-player.md` 的拍板）。
> 全部改动落在新增的平台层、Web 层与工具层。

### 期 0 · 先修三个已知缺陷（阻塞项，先于一切新功能）—— ✅ **已完成（2026-10）**

- **G-6 记账串号**（Critical）→ ✅ 已完成：`UsageTracker` 按会话隔离
  （`saves/usage-<sid>.jsonl` + 条目带 `session` 轴），进程级单例已删除，
  新增 `GET /api/{sid}/cost`。守卫 `tests/test_web_accounting.py`。
- **G-7 存档版本戳**（Major）→ ✅ 已完成：`save_version` 2→3，落 `pack: {id, digest}`；
  读档不一致抛 `PackMismatchError`（**拒绝并提示**，且在构造 `GameState` 之前）。
  守卫 `tests/test_pack_identity.py`。摘要口径：只吃玩法内容（**不吃 judge_corpus**）、
  换行归一（CRLF/LF 同摘要，跨机可对账）。
- **G-3 前置**：把 `scripts/import_story.py` 的生成管线从 CLI 提到引擎侧模块 → ⬜ 未开始（属期 3）。

> 理由：这三个是 `plan-creator-player.md` 已认定的缺口，且**任何多会话/多剧本形态都会被它们直接击穿**。
> 在多玩家之前修是 3 个小改动；在多玩家之后修是数据考古。**前两项已验证，第三项随期 3 走。**
> 详见 `docs/plan-tavern-shaped-product.md` §6.1 的执行记录与验证证据。

### 期 1 · 前端重写（最大可见差距）—— ✅ **已完成（2026-10）**

- 目标形态（对齐 dsh-tavern 的界面分区）：**左侧会话/存档列表 · 中间正文流 · 右侧状态栏**。
- 必须有的：流式正文、候选选项（引擎已有的关键节点固定选项 → 独立渲染，不塞进正文）、
  **状态栏分栏**（场景 / 身份锚点 / 属性好感 / 计划块 / 在场角色卡——`_view()` 已经在产出这些数据）、
  历史回溯、存/读档、成本可见。
- 技术选择：引擎侧已经有 `FastAPI + SSE`（`web.py` 的 `/api/{sid}/turn` 流式），
  **保留 SSE 契约，换掉表示层**即可，不必重写后端。
  ✅ **已全部完成**：Stage A 拆真实文件 → Stage B/C 用 Vite + Vue 3 重写为三栏 →
  **N2a 创作工作台**（中栏两模式：从素材生成 / 和 Agent 改这一版；右栏只读呈现 + diff）→
  **N6 创作者 Agent 对话面板**。真机浏览器冒烟 **50/50**（`scripts/webui_smoke.mjs`）。
  §5.1 那张"工作台 2：编辑器（左边对话、右边字段与校验报告）"现在是**字面成立**的。
  **未做**：跨设备响应式打磨、多语言、主题系统（后两者属 G-10 一类，建议不做）。
- 关键的架构收益：**玩家看到的与模型看到的同源**（README 已经把这称为"代码掌握真值的可视化"）——
  这一点 dsh-tavern 做不到（它的正文与状态来自两个 Agent），是本项目前端该主打的产品差异点。

### 期 2 · 存档点 / 回退 / 分支（把 `runlog` 产品化）—— ⬜ **仍未做（最高优先）**

这是"dsh-tavern 形态"里**性价比最高**的一项，因为数据已经在：

- `runlog.py` 已有回合级 `checkpoint`（全量 `state.to_dict()` + `history`）+ `load_checkpoint` + 重建 `Game`。
  缺的只是：**把它从事后实验设施变成玩家面存档模型**（写入位置、保留策略、UI 入口）。
- 需要新增的语义（直接采用 dsh-tavern 已经踩过坑的结论）：
  - **revision 只增不减**，回退也产生新版本（`CONTEXT.md` 定义）；
  - **前台正文提交即建 checkpoint**；派生状态失败**不撤销正文、不阻塞下一轮**（ADR 0006）；
  - **结算结果必须匹配 branch + revision**，否则丢弃（防迟到写入污染新剧情）；
  - 回退后旧分支保留但不得继续影响当前剧情。
- 已验证的坑（`docs/sillytavern-borrow.md` D-1/D-2）：**不要**引入"按消息计数"的定时状态机，
  **不要**引入随机数决定注入——两者都与 `after == before + delta` 可回放冲突。
  本项目有 `state.py` 的日程真值，应走 `conditions.py` 声明式条件。

### 期 3 · 前后台分离（"一主多子"）—— ⬜ **仍未做**

- 现状：一轮内串行完成正文 + 状态 + 候选 + Judge。
- 目标：**前台只出正文并立即提交**；候选生成、状态结算、关系洞察作为**绑定 branch/revision 的后台操作**。
- 收益：直接改善首字延迟与体感（dsh-tavern 的公开主张是 ~10 秒/轮），
  且与期 2 的 revision 契约天然契合。
- 注意：本项目已有 `judge` 内轮自校正（`_critique_and_regenerate`）——它**必须留在前台**
  （在正文提交前决定是否重生成），只有"派生的、可后补的"工作才能后移。

### 期 4 · 内容生态互操作（导入优先）—— ⬜ **仍未做**

- **只做导入，先不做导出**。价值在于一次性接入酒馆生态的海量现成人物卡。
- 实现依据已经在仓库里：`docs/sillytavern-borrow.md` §E-1 已经把
  `character_book` ↔ World Info ↔ `LoreSpec` 的字段映射表逐列写好了；
  §E-3 给了正确的姿态：**解析宽松 → 归一为标准模型 → 再严格校验**（`load_worldpack` 读宽 / `check_worldpack` 写严）。
- 落地：新增 `game_agent/st_convert.py`（`character_card_to_worldpack()`），
  `scripts/import_story.py --format sillytavern`，往返一致性测试放 `samples/`。
- **不做**：PNG 卡写出、`sticky/cooldown` 定时、加权随机、7 种插入位置、双宏引擎
  （D-1～D-10 已逐条论证）。
- ⚠️ **前置纪律（有先例）**：A 系列（lore 注入语义）落地后，`LoreSpec` 的
  `max_recursion` / `secondary_keys` / 四值 `logic` / `case_sensitive` / `ignore_budget` / `constant`
  等**十个字段在 8 个真实世界包里零用量**。E-1 必须**由一份真实酒馆人物卡驱动**（真卡语料进 `samples/`），
  **不要**先按映射表把字段铺满再等用例——否则会复现同一类"校验通过但从未运行"的死代码。

### 期 5 · 分发与出口—— ⬜ **仍未做**

- `pyproject.toml` 加 `[project.scripts]`，做到 `uvx game-agent play <pack>` / `pipx install` 一条命令开玩。
- 存档自带剧本身份（期 0 已做）→ 支持"整包分发 + 存档可迁移"。
- 世界包加 `asset://` 间接层（`sillytavern-borrow.md` §E-2 已给方案）——**这是"导出/分发"功能的核心设计**，
  否则换环境就失效。
- 保留并加固 `mcp_server.py` 作为**外接宿主出口**：让别人（包括 DSH）能把本项目当工具面调用，
  而不必把运行时的方向盘交出去。

---

## 6. 明确不做（防范围蔓延）

| 不做 | 理由 |
| --- | --- |
| 移植成 DSH 插件 / TS 重写 | §3.1：架构敌对 + 宿主 RC 阶段 + 需重建 828 项测试的语义 |
| Tavern Helper 脚本执行沙箱 | 本项目 `conditions.py` 是**声明式、不执行作者代码**；这是安全优势，不是缺口 |
| MVU 协议实现 | 它存在的理由是"酒馆没有权威状态"；本项目有 `state.py` + 事务提交，属于**架构倒退** |
| 正则美化 / HTML 状态栏 | 它是"文本模拟结构化数据"的补偿手段；本项目走工具调用契约 |
| 场景配图（本期） | ~25 文件的体量，与核心价值无关；等前端与存档稳定后再评估 |
| 多平台安装器（EXE/APK） | 期 5 的 `uvx`/容器方案覆盖同一需求，成本低一个数量级 |
| 账号体系 / 云端 | `plan-creator-player.md` 已拍板"本机/局域网形态，服务端出 key，不做账号" |

---

## 7. 风险与待拍板

**风险**

1. **前端重写的范围失控**。dsh-tavern 的客户端是 15.4K 行 + 宿主 60+ 个 UI 插件。
   必须**以 `_view()` 已有数据为边界**：引擎不产出的东西（小手机、MVU 面板、配图）本期一律不做。
2. **后台化会稀释审计不变量**。期 3 引入异步后，"每轮零偏差"的证明面会变宽。
   必须先落 ADR-0006 式的 **branch + revision 校验**，再开后门；否则迟到写入会污染历史。
3. **`runlog` 复用需谨慎**。它现在写 `runs/`（gitignored）、按回合存全量（长局 ~10MB/局）。
   产品化要重新定保留策略与目录，**不要**直接把 `runs/` 当存档目录。
4. **`.st-inspect/` 是一个未跟踪、且未被忽略的「嵌入式 git 仓库」**（自带 `.git`，实测 **95.5 MB**；
   `git check-ignore -v .st-inspect` 返回空，`.gitignore` 里没有对应规则）。
   需要澄清一个容易误判的点：它**不会**把 95.5 MB 提交进仓库——
   `git add -A` 只打印 `warning: adding embedded git repository: .st-inspect`，然后添加一个 **gitlink**。
   真正的代价是：仓库里从此多出一个**没有 `.gitmodules` 的悬空子模块指针**，
   克隆者拿到的是空目录 + 指向外部 commit 的失效引用，且 `git status` 长期出现噪声条目。
   修法一行：`.gitignore` 加 `.st-inspect/`（或把它移出工作区）。
   附带一个**可复现性**问题：`docs/sillytavern-borrow.md` 明确以该 checkout 为分析基准
   （`06bde93` / v1.19.0），但**全新 clone 拿不到它**——外部读者无法复核这份评审的 `file:line`。
   建议把基准 commit 写进文档正文（已有）并在 `.gitignore` 里注明获取方式。

5. **"证据面"里有两处不可复现的数字**，而本项目以证据面为卖点，这类数字会被当成证据读：
   - `README.md` 声称"**648 项离线测试**"（:49、:127）与"**47 个工具**"（:129），
     实测为 **828 个 `def test_`**（78 个模块）与 **38 个 `scripts/*.py`**；
     `.github/workflows/ci.yml:22-23` 自己记录了同类漂移（"843 → 633"）并把计数从步骤名里移除——
     同样的处置应施加到 README（或改为 CI 自动回写）。
   - **更值得注意的是 99.3% 缓存命中率**：它记在 `docs/resume-entry.md:16`，而该文件被
     `.gitignore` 明确排除（"简历/面试预案：只在本地维护，绝不入库"）。
     于是 **README 引用的核心性能指标，在 clone 里根本不存在、也无法复算**。
     引擎**确实**逐次记录 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`（`usage.py:96-101`）并计入 `cost_report`，
     所以修法不是补测量，而是**把这条指标从一份 gitignored 文档里挪进可复现的产物**（例如 `reports/` 的报告卡）。

6. **两处结构性脆弱点，期 2/期 3 会直接踩上去**（先修再动，别在它们上面叠异步）：
   - `worldpack._cross_check` 是 **717 行单函数 = `worldpack.py` 的 55%**，内含 **97 个 `raise WorldPackError`**，
     并且**手工重复实现了五遍**"条件/效果引用了什么"的采集（node `when`、node `completion`、event `when`、
     ending `when`、action `requires`、tool `requires`）。这个重复**已经产生过两个线上缺陷**（`ActionEffects.flags`
     缺失导致行动写 flag 静默 no-op；`options[].effects.stats` 未采集）——两处都在代码里留了复盘注释。
     期 4 要往校验器上加 ST 导入路径，**必须先把"什么可被引用"收敛成一份声明式 schema**，否则第 6 遍手写采集会诞生第 3 个同类 bug。
   - **压缩会静默退化**：`_compress_history` 有 5 处静默 `return`（切点找不到 / 无新内容 / LLM 失败 / 摘要为空 / 配对失败），
     而 `_overflow_retry` **只允许一次**。持续失败时的表现不是报错，而是**历史一路涨到熔断**。
     期 3 把结算移入后台后，"安静地坏掉"的窗口会变宽，因此这项要先加可观测性。

7. **若最终仍决定采用 DSH（哪怕只做 §3.3 的 UI 壳），先接受它的升级成本。**
   DSH 是 **RC**，且**已经删过第三方依赖的包**（本机悬空 junction：`dsh-client-runtime`、
   `dsh-client-ui-slots`、`dsh-client-ui-primitives`、`dsh-client-web`、`dsh-host-apiproxy`；
   第三方 `@mlgbnb/dsh-archive-manager` 因 import 被删 face 会**直接让 `dsh web` 启动失败**）。
   它**没有**任何插件签名/白名单/完整性校验，也**没有**版本协商机制
   （`dsh.engines.dsh` 这个字段 **DSH 根本不读**，只是第三方插件管理器自己的约定）。
   唯一的旗舰消费者 dsh-tavern 的应对就是**锁死一个精确版本 + vendor 29.4 MB + 对 8 个宿主文件做 SHA-256
   校验的猴补**。采用 DSH 就要按这个量级预留维护预算，并**优先选把引擎留在进程/HTTP 边界另一侧的方案**。

**待拍板（需要你定）**

- **A. 目标是什么？** "像 dsh-tavern" 有三个互不相同的目标，优先级决定路线：
  ①**产品形态**（界面/安装/分发）；②**生态兼容**（能玩酒馆卡）；③**架构形态**（DSH 插件、前后台 Agent）。
  本文按 ① ② 排期，③ 只取"一主多子"这一条可移植的思想。
- **B. 前端技术栈（三个选项，证据见 §3.3）**：
  ① 继续原生 JS（轻，与现有 SSE 契约最近）；② 引入构建步骤（React/Svelte，上限高但引入 toolchain）；
  ③ 把界面写成 **DSH 客户端插件**（引擎仍在 HTTP 另一侧，不碰护城河）。
  **取舍已收敛为一个具体问题：你要的界面是"聊天页 + 侧栏"，还是"全屏游戏布局"？**
  前者 ③ 很划算；后者会被推到未文档化的 DOM 逃生口上——那就建议自己做（①②，我推荐 ①）。
  如果不确定，花 1–2 天用 ③ 做个只读状态栏面板当**探针**（验证槽位是否够用、宿主升级会不会坏），
  比直接排期更省钱。
- **C. 是否要在期 4 之后评估"导出到酒馆试玩"**（用别人的成熟前端验证自己的世界观，
  `sillytavern-borrow.md` §E-1 的第二个方向）——这是低成本借力，但会引入格式维护成本。

---

## 8. 工作量估算（粗粒度，人日）

> ⚠️ **进度已并入 `docs/roadmap.md`**（2026-10）。那张表是"下一步做什么"的唯一答案。
> **本表的期 2/3（存档点/回退/分支、前后台分离）此前不在另两份路线图里**——
> 合并时补进 roadmap 的 N7/N8。

| 期 | 内容 | 估算 | 依赖 | 状态（2026-10） |
| --- | --- | --- | --- | --- |
| 0 | 记账隔离 / 存档版本戳 / 生成管线提取 | 3–5 | 无 | ✅ **已完成**（记账隔离 + 存档版本戳 + 生成管线提取 B1） |
| 1 | 前端重写（流式 + 状态栏 + 候选 + 历史 + 存读档） | 10–18 | 期 0 的 G-6/G-7 | ✅ **已完成**：Stage A → Stage B/C（Vite + Vue 3 三栏，产物入库）；真机冒烟 18 项 |
| 2 | 存档点 / 回退 / 分支（`runlog` 产品化 + revision 契约） | 8–14 | 期 0 | ⬜ 见 roadmap **N7** |
| 3 | 前后台分离（后台候选 + 后台结算 + revision 绑定） | 8–14 | 期 2 的契约 | ⬜ 见 roadmap **N8** |
| 4 | ST 导入（`st_convert.py` + 宽容解析层 + 往返测试） | 5–8 | 无（可与期 1 并行） | ⬜ 见 roadmap **N9** |
| 5 | 分发（entry point / 存档迁移 / `asset://` / MCP 加固） | 4–7 | 期 0、期 4 | ⬜ 见 roadmap **N11** |
| — | harness 改进 4 条（本文 §8 的"顺带值得改"） | 小 | — | ⬜ 见 roadmap **N10** |

**总计约 38–66 人日**，**已完成约 18–27 人日**（期 0 + 期 1 + 平台化 2/3 + B1）。
对照路线 A（TS 重写）：dsh-tavern 是 57K 行原创代码 + 51K 行测试的体量，
即使只做引擎等价部分，也远超此表一个数量级，且会失去现有测试的资产价值。
（离线测试**现已 934 项**全绿。）

---

## 附：本次分析的方法与限制

- 对 dsh-tavern 的结论均来自本地只读 checkout 的实际文件（`plugin.patch.yml`、`presets/tavern/agent.cordis.yml`、
  `docs/architecture.md`、`docs/adr/*`、`docs/design/*`、`lib/domain/` 文件清单与行数统计），已尽量给出路径。
- 对 DSH 宿主能力的判断有四处实证：①已安装的 `@deepseek-ai/dsh@0.1.5-rc.3` 包
  （**其打包产物未混淆，注释与 JSDoc 保留**，且每个同级包都带 `README.md`，共 **251 个** `@deepseek-ai/*` 同级包）；
  ②dsh-tavern 的 patch 与兼容文件（`plugin.patch.yml` / `config/dsh-compatibility.json` /
  `bin/dsh-compatibility.mjs` / `tavern-plugin/lib/domain/host-*-patch.js`）；
  ③活动 profile 里**带完整 TypeScript 源码**的第三方插件（`dsh-better-sidebar@0.19.1` 的槽位/路由/配置契约，
  `@linxin666/dsh-remote-web-ui` 的浏览器侧能力边界）；④`dsh-mcp-client`、`dsh-session`、`dsh-llm`、
  `dsh-typert-protocol` 等宿主包自带的 README。
  profile 覆盖顺序（bundle patches → profile `cordis.patch.yml` → `$DSH_HOME/cordis.patch.yml` → `--patch`，
  且 patch **整体替换**某行的 `config`、不做深合并）来自 `lib/profile-boot-*.js` 与 `dsh-app-boot`。
  **仍未验证（明确列出，不当作既定 API）**：①完整槽位目录——`dsh-client-ui-slots` 本机未安装，
  权威来源是运行时 provider `Slots.listSubTree`，需要一个活宿主才能查询；
  ②DSH 官方是否有扩展点稳定性承诺（当前未发现任何此类机制）；
  ③`dsh-market.com` 等第三方市场存活情况（本机 fetch 被策略拒绝）。
  本文所有"插件能做什么"的描述都限于**已在上机源码中读到证据**的部分。
- **未核实**：dsh-tavern 的 star / 用户量；其"缓存命中率 95%+""一轮约 10 秒"等性能主张（属其自述，本文仅作对照，未复现）。
- 本文**不修改**任何既有文档的决策，也不改动引擎代码。
