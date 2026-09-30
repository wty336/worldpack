# SillyTavern 借鉴清单 v0.1

> 本文档汇总 2026-09-30 对 SillyTavern 的一次全量源码评审结论，输出**「值得借什么、不值得借什么、为什么」**。
>
> **分析基准**：`SillyTavern/SillyTavern` release 分支 @ `06bde93`，`package.json` version **1.19.0**，AGPL-3.0。
> 服务端 `src/` 96 文件 / 29,171 行；前端 `public/scripts/` 201 文件 / 118,330 行；`tests/` 38 文件 / 10,944 行。
> 评审方式：四路并行只读深挖（World Info / 提示词-宏-预设 / 服务端安全 / 扩展-工具调用），
> 逐条核对至 `file:line`；凡未在源码中找到的能力，均显式标注「未找到」而非推测（见 §6）。
>
> **与既有文档的关系**：本文档不改写 design.md 的决策。它与 `improvement-roadmap.md` §1 是**同一条外部对标线的细化**——
> roadmap 的 B1 已按「参照 SillyTavern World Info」实现了 lore 按需注入，本文档给出 B1 之后的**下一步清单**。
> 每项均含「现状 → 问题 → 方案 → 验收」，可单独拆出执行。
>
> **重要前提（决定了本文档的取舍口径）**：
> SillyTavern 不是 Agent，**它是一个上下文组装器**。全项目随包发布的函数工具只有 `GenerateImage` 一个；
> web search 是 provider 侧标志位而非本地工具；它的工具循环只有「执行 → 塞回 transcript → 再生成」，
> **没有任何代码在判断模型该不该知道某个数值**。
> 因此正确的读法是：**把它当成「上下文注入的参考实现」读，不要当成「Agent 架构」读。**
> 本文档中所有 A 类条目都来自它的注入机制，而非它的业务逻辑。

---

## 0. 结论速览

| 批次 | 主题 | 条目数 | 优先级 | 状态 |
| --- | --- | --- | --- | --- |
| A 系列 | World Info（世界书）注入语义 | 5 | **P0**（改动小、语义更正） | ✅ **全部落地（2026-09-30）** |
| B 系列 | harness 契约的五处显式化 | 5 | **P1**（架构级，值得排期） | 待办 |
| C 系列 | 内容层与工程配件 | 12 | P2（遇到了再拿） | 待办 |
| D 系列 | 明确不借（含 3 条"看似缺失实为优势"） | 10 | — | — |
| E 系列 | 内容格式的生态互操作 | 3 | P2（素材进出成本） | 待办 |

**一句话总结**：SillyTavern 值得借的全部集中在 **World Info / 角色卡两个系统暴露出的机制**（A / C-1 / E）
+ **四个与其 Agent 能力无关的工程契约**（B）；
它的内容格式（PNG 卡、手写校验器、巨型单文件、双宏引擎）应作为**反面参照**，D 系列逐条说明原因。

**A 系列的落地结果（详见 §A 各项的「验收证据」）**：新增共享判定模块 `game_agent/lore.py`；
`LoreSpec` 增 7 个字段；`lore` 预算改 token 制并按窗口推导；
**离线测试 648 → 846 项**（新增 46 项 lore 守卫），8 个世界包校验全绿，零回归。

---

## A 系列：World Info（世界书）注入语义（P0）

> 背景：`docs/design.md` §4.3.1 的 B1 已实现 `world.yaml` 的 `lore: [{id, keys, text}]` 按需注入
> （`game_agent/context.py::select_lore`）。A 系列是**在 B1 之上做语义修正**，不推翻 B1 的数据结构。

### A-1 lore 预算从「字符数」改为「占上下文的比例」

- **现状**：`game_agent/context.py:17` `LORE_BUDGET = 1500`，单位是**字符**；
  逐条 `len(text) + len(id) + 8` 近似累加（`context.py:90`）。
- **问题**：1500 是个**魔法数**。它真正的语义应该是「动态区允许占多少窗口」。
  静态前缀已按 `docs/design.md` 定为 ≤8K 预算（`p3-report.md` §2 验收），
  **动态区的 lore 预算却没跟上下文窗口挂钩**——世界包一换（如 30+ 条 lore 的大包），这个数就失去意义。
  另外"字符"与"token"在中文世界的比例不固定，跨包不可比。
- **ST 参照**：`public/scripts/world-info.js:4736`
  `budget = Math.round(world_info_budget * maxContext / 100) || 1`，
  `:4738-4741` 再受 `world_info_budget_cap` 截断（配置为 0 表示不设上限）。
- **方案**：
  1. `LORE_BUDGET` 改为比例（如 `LORE_BUDGET_RATIO = 0.05`）+ 可选绝对上限；
  2. 预算计算式：`budget = ratio × window_tokens − static_prefix_tokens`，复用既有 token 校准设施；
  3. 保留逐条累加判溢出的结构（已是此形状），**不要改成"选完再截断"**；
  4. 借 ST 的 `ignoreBudget` 豁免口子（`:5017-5026`）——给「无论如何必须注入」的条目留一个白名单，
     对应本项目的**引擎强制接管节点**。
- **验收**：
  - 离线：新增/改造 `tests/test_lorebook.py` —— 同一包在不同 `ratio` 下注入条数单调；
    大包（30+ lore）静态前缀恒定量不变；`ignore_budget` 条目在预算耗尽后仍注入；
  - 真机：`scripts/lore_smoke.py` 复跑，命中集合与验收记录一致；
  - 文档：回写 `docs/design.md` §4.3.1 的预算口径（字符 → 比例 + token）。

- **落地状态**：✅ **已完成**
- **落地证据**：
  - `context.py`：`LORE_BUDGET_RATIO = 0.05` / `LORE_BUDGET_CAP = 2500` /
    `LORE_BUDGET_FALLBACK = 1500` + `resolve_lore_budget(window_tokens)`；
    `select_lore` 的 cost 改用 `est_tokens`（复用 `compression.py` 的**既有单一真源**，
    与压缩阈值/溢出预检同口径）；
  - `ContextBuilder` 增 `lore_budget` 字段，`from_pack(pack, window_tokens=...)` 推导；
    `game.py` **先赋 `self.context_window` 再建 builder**（顺序错了会静默取到默认值）；
  - `LoreSpec` 增 `ignore_budget`（与 A-4 的 `constant` 明确为两个独立维度）；
  - 测试 5 项：token 而非字符计数、预算随窗口缩放并被上限截断、
    **小窗口下注入更少 lore 但静态前缀逐字节不变**、`ignore_budget` 条目在预算耗尽后仍注入、
    注入总量不超过推导预算；
  - 全量 833 passed（A-1 完成时）、8 包 `check-worldpack` 全绿。

### A-2 lore 增加次级关键词与四值逻辑

- **现状**：`LoreSpec`（`game_agent/worldpack.py:61-63`）只有 `id / keys / text`；
  `select_lore` 用 `k in context` 子串匹配（`context.py:83`）。
- **问题**：当前在库世界包的 lore 只有 **3~8 条**（实测：`star_ring` 8、`ancient_jianghu` 6、
  `campus_otome` 8、`xianxia_wendao` 7、`urban_neon` 7），主键子串匹配完全够用。
  **但世界包长到 30+ 条后，"东市"「诗会」这类词在叙事文本里到处出现，误命中与预算挤占会成为第一个痛点。**
  这是可预见的功能性缺陷，不是假想问题。
- **ST 参照**：`keysecondary[]` + `selectiveLogic` 四值枚举
  （`world-info.js:33-38` 定义，`:4943-4978` 实现）：
  | 值 | 语义 | 典型用途 |
  | --- | --- | --- |
  | `AND_ANY(0)` | 次键任一命中 | 主键命中后再要求一个上下文词 |
  | `NOT_ALL(1)` | 次键任一**未**命中 | 排除干扰 |
  | `NOT_ANY(2)` | 次键全部未命中 | **最常见**：命中 A 但没提到 B 才算 |
  | `AND_ALL(3)` | 次键全部命中 | 严格共现 |
- **方案**：
  1. `LoreSpec` 增 `secondary_keys: list[str] = []` 与 `logic: Literal[...] = "AND_ANY"`；
  2. `select_lore` 的**签名不变**（`lore, context, budget` —— context 里什么都有），
     只在命中判定内部加一层次键求值；
  3. 加载期校验：`secondary_keys` 非空时 `logic` 必须显式给出（避免默认值歧义）；
  4. 顺带升级排序：借 ST 的组内评分 = 主键命中数 + 次键命中数（`world-info.js:428-473` `getScore`），
     现有"命中关键词数降序"（`context.py:86`）已是该思路雏形，直接扩展即可。
- **验收**：
  - 离线：`tests/test_lorebook.py` 新增四值逻辑各自的真值表用例（4 值 × 边界=命中0/1/全部）；
    反例守卫：同一 `keys` 配不同 `logic` 必须产生不同命中集；
    `check-worldpack` 对「有次键无 logic」报错；
  - 变异验证：按项目惯例做一次变异（例如把 `NOT_ANY` 实现成 `NOT_ALL`），守卫必须失败。

- **落地状态**：✅ **已完成**
- **落地证据**：
  - 新建 `game_agent/lore.py`：`match_score()` 承载四值逻辑，
    `is_regex_key` / `compile_regex_key` / `validate_key` 承载键匹配原语。
    **独立成模块的原因**：`worldpack`（加载期校验）与 `context`（运行时匹配）都要用它，
    而 `context` 已 import `worldpack`——放任一侧都会循环导入。共用同一套判定
    保证"**能通过校验的必能匹配**"；
  - `LoreSpec` 增 `secondary_keys` / `logic`；加载期校验
    「有 `secondary_keys` 就必须显式声明 `logic`」（用 `model_fields_set` 区分
    "作者写了"与"默认值"，避免默认值静默决定语义）；
  - 测试 12 项：四值各自的正反用例、**同一 keys/次键仅 logic 不同必须产生相反结果**
    （防四值被实现成同一分支）、次键为空时退化为纯主键命中、评分参与排序，
    以及 3 项加载期校验用例；
  - **变异验证（文档要求的守卫强度证明）**：把 `NOT_ANY` 临时改成按 `NOT_ALL` 求值
    → `test_secondary_not_any_passes_when_no_secondary_hits` **如期失败** → 已恢复实现。

### A-3 键支持正则与大小写敏感

- **现状**：`keys: list[str]`，匹配是 `k in context` —— 纯子串、大小写敏感、不支持模式。
- **问题**：世界包一旦要匹配**模式化**的词（日期格式、编号、数值区间、带变体写法的称谓），
  纯字面量就表达不了，作者只能穷举近义词。
- **ST 参照**：键元素若写成 `/pattern/flags` 走正则，且**正则优先于其他所有匹配选项**
  （`world-info.js:337-342`，`parseRegexFromString` `:2901`）；
  另有 `caseSensitive` / `matchWholeWords` 两个逐条可覆盖的布尔（`:337-366`，`matchWholeWords`
  对单词用 `(?:^|\W)(key)(?:$|\W)` 以覆盖标点边界 `:356`）；
  条目级不设时继承全局设置（`entry.caseSensitive ?? world_info_case_sensitive`，`:268-271`）。
- **方案**：
  1. 键元素沿用 ST 的「`/re/` 即正则」**隐式约定**（作者友好，无需多一个字段）；
     实现上可先支持无 flags 形式，非法正则**加载期报错**而非运行时静默跳过；
  2. `LoreSpec` 增 `case_sensitive: bool = True`（中文场景下默认敏感更安全）；
     仅在需要匹配英文/拼音变体时关闭；
  3. 明确规则「正则键命中即忽略其他匹配选项」并写入 `docs/worldpack-manual.md`。
- **验收**：
  - 离线：正则键与字面键在同一 context 下行为可区分；非法正则被 `check-worldpack` 拒绝并给出行号；
    `case_sensitive: false` 时大小写变体命中、`true` 时不命中；
  - 文档：`docs/worldpack-manual.md` 的 lore 章节补正则与大小写说明及反例。

- **落地状态**：✅ **已完成**
- **落地证据**：
  - 正则判定在 `lore.py`：`/…/` 成对即正则，**单个斜杠仍是字面量**（避免误伤含斜杠的词）；
    非法正则 `LoreKeyError` → 加载期转 `WorldPackError`（含条目 id），
    **不再运行时静默不命中**；
  - `LoreSpec` 增 `case_sensitive: bool = True`，同时作用于主键与次键；
  - **有意不实现 `matchWholeWords`**：SillyTavern 用 `(?:^|\W)(key)(?:$|\W)` 兜标点边界，
    但 `\W` 对中文按 Unicode 判定——每个汉字都是 `\w`，该规则在中文语境下不成立。
    中文字面量本无边界分歧，故不引入这个只对英文有效、却会让中文作者困惑的开关
    （已在 `lore.py` 模块文档中记录理由）；
  - 测试 8 项：正则命中模式而非字面量、多选分支、大小写默认拒绝变体 /
    关闭后命中、大小写对次键同样生效、正则键与次键正则的非法加载期拒绝、单斜杠字面量。

### A-4 常驻注入与递归激活

- **现状**：只有「关键词命中 → 注入」一条路径。`constant` / `excludeRecursion` /
  `preventRecursion` / `delayUntilRecursion` 均无对应字段。
- **问题一（常驻）**：世界通则类设定（货币、历法、交通、忌讳）**每轮都必须知道，但每轮都不一定被提到**，
  靠关键词匹配必然漏。现在只能塞进静态前缀，而静态前缀有 ≤8K 硬预算与 KV Cache 冻结纪律。
- **问题二（递归）**：世界包存在「提到 A 才需要知道 B」的**二级知识**——
  命中「青云仙宗」应顺带让人知道「洗剑池」「苏长老」，但正文里不会同时出现这些词。现在这类知识要么漏，要么靠堆 `keys`。
- **ST 参照**：
  - `constant: true` 跳过关键词判定直接激活（`world-info.js:4899-4903`）；
  - 递归：本轮新增且未设 `preventRecursion` 的条目 content **加入递归缓冲**，作为下一轮扫描输入
    （`:5097-5100`、`:5138-5144`），`excludeRecursion` 使其不被递归激活，
    `delayUntilRecursion: N` 延迟到第 N 级才可激活；
  - 与预算交互：`ignoreBudget` 是**另一套**豁免（`:5017-5026`），两者不重叠。
- **方案**：
  1. `LoreSpec` 增 `constant: bool = False` —— 跳过关键词判定、总是候选，**但仍受预算约束**
     （常驻 ≠ 无限，这是常驻条目最容易失控的地方）；
  2. 递归默认**关闭**，由世界包开关启用 `max_recursion: int = 0`（借用 ST 的深度闸，但默认 0 而非无限）；
     配 `exclude_from_recursion` / `no_recursion_trigger` 两个逐条标志；
  3. **明确 `constant`（位置规则：是否需关键词）与 `ignore_budget`（预算规则：是否占额度）是两个独立维度**，
     不要合字段——A-1 已引入 `ignore_budget`，此处是最容易实现成歧义的地方。
- **验收**：
  - 离线：`constant` 条目在 context 无任何关键词时仍注入；`constant` 不改变其他条目的预算账（非负测试）；
    `max_recursion: 0` 时递归不发生；`max_recursion: 2` 时二级命中可达、三级不可达；
    `exclude_from_recursion` 条目不被递归激活但可被直接命中；
  - **真机**：新增探针（仿 `scripts/lore_smoke.py`）——给包加一条「A 命中 → B 递归命中」的二级知识，
    断言 B 的单轮注入**在 A 之后**发生（顺序而非仅存在）。

- **落地状态**：✅ **已完成**
- **落地证据**：
  - `LoreSpec` 增 `constant` / `exclude_from_recursion` / `no_recursion_trigger`；
    `WorldSpec` 增 `max_recursion: int = 0`（**默认关闭**，与旧行为一致），
    加载期拒绝负数；
  - `select_lore` 改为**逐层扫描**：每层先对本层候选打分，
    层内按「评分降序、同分文件序」定序，层间按激活顺序（直接命中在前、递归命中在后）——
    返回顺序即激活顺序，可解释、可断言；
  - 常驻与预算**两个维度独立**：`constant` 免关键词判定但仍占预算，
    `ignore_budget` 免预算但仍需关键词，两者各自单独生效（各有测试）；
  - 测试 10 项：无关键词也注入、常驻仍受预算约束、两维度独立、
    递归默认关闭、A→B 二级知识可达、深度限制（1 层 vs 2 层）、
    `exclude_from_recursion` 仍可被直接命中、`no_recursion_trigger` 不传染、
    递归受预算约束、**多轮递归后每条只出现一次**；
  - 中途发现并修正一处回归：把循环从"先排序再选取"改成逐层扫描时
    **丢掉了 A-2 的评分排序**，被既有的两项排序测试当场抓住（这正是先写测试的价值）。

### A-5 动态区内部的放置与排序语义

- **现状**：lore 以单一 `<lore>` 区块注入，内部按「命中关键词数降序、文件序」；
  **B1 把它追加在 `<agent_status>` 之后**——即状态栏不再是动态区的最后一块。
- **问题**：注入内容**离生成点越近、影响力越大**——这是 ST 用默认 order 表达的核心经验
  （`main → worldInfoBefore → charDescription → dialogueExamples → jailbreak`，
  `default/content/presets/openai/Default.json`「越靠近对话末尾锚定越强」）。
  本项目动态区本身在对话之后（**方向已经对了**），但区块先后缺依据：B1 让 lore 排到了
  状态栏后面，于是**离生成点最近的变成了补充知识，而不是引擎真值**。
  这与 `design.md` §4.4「状态栏（每轮末尾追加）」直接冲突——文档与实现各说各话。
- **方案**：
  1. 明确动态区末尾的**固定序列**并写入 `design.md`：`场景/在场 → lore → 状态栏`，
     即**最不可替代的内容离生成点最近**（状态栏是引擎真值的唯一权威表述，lore 是补充知识）；
  2. A-2 的评分（主键命中数 + 次键命中数）作为 lore 内部排序键，现有"命中数降序"保留为其退化形式；
  3. ST 的 `outlet` 位置（不自动注入、由宏按需取用，`world-info.js`）
     **不需要新字段**——本项目的 `query_world` 只读查询工具已覆盖该需求。
- **落地状态**：✅ **已完成**（**修正了一处既有的文档/实现不一致**）
- **落地证据**：
  - **发现**：`design.md` §4.4 写「状态栏（每轮末尾追加）」，但实现里 `<lore>` 是
    **追加在状态栏之后**的——即"末尾"其实被 lore 占了。这是 B1 引入 lore 时与 §4.4 的冲突，
    文档与实现各说各话。本次按文档原意修正实现（而非改文档），
    理由是注入位置的依据应来自机制（离生成点近 = 影响力大），而不是"谁后来加的谁排最后"；
  - `status_text` 重构：状态栏各行收集进独立的 `status_lines`，
    在 `<lore>` 之后 `lines.extend(status_lines)` 统一追加 → 固定序列
    **场景/身份 → 在场角色卡与记忆 → `<lore>` → 状态栏**；
  - 测试 2 项：状态栏是动态区最后区块（`rstrip().endswith("</agent_status>")`）、
    `<lore>` 起始与结束都在 `<agent_status>` 之前（**先断言确有 lore 命中，否则测试空转**）；
  - 全量 846 passed（迁移状态栏位置**零回归**——状态栏历史快照的字节级不变量未受影响）；
  - 回写 `docs/design.md` §4.3.1 与 §4.4。

---

## B 系列：harness 契约的五处显式化（P1）

> 这五条不是功能，是**契约**。它们的共同特征是：ST 用一套显式机制把一个隐式约定变成了可校验的东西，
> 而本项目目前靠约定/注释维持。每条都标注了"本项目的现有落点"。

### B-1 agent 循环的预算必须对外可见

- **现状**：`game_agent/budgets.py` 存在；对话循环有工具调用收尾（`ENGINE_RULES` 第 1 条：
  「每一轮必须以工具调用结束」）。
- **问题**：工具调用**轮次**是否有硬上限、该上限是否对外可见可配，需核对；
  ST 的 `RECURSE_LIMIT = 5`（`public/scripts/tool-calling.js:255`）证明这是必须显式的量——
  它一路透传 `depth` 参数（`script.js:4494-4496`、`:5408-5437`）。
- **ST 参照**：固定常量 + 可配置覆盖（`oai_settings.tool_call_recurse_limit`，`openai.js:4393`），
  简单、显式、可复现，**比"检测循环"可靠**。
- **方案**：把工具调用轮次纳入 `budgets.py`，写进 trace/usage 记账，并暴露为世界包或运行时可配项。
- **验收**：离线用例——构造一个反复调用工具不收敛的假模型，断言在 N 轮后被引擎截断且
  trace 中记录截断原因（而不是无限循环或静默失败）。

### B-2 「转录即真相，API 消息是派生物」——核对 runlog 的持久化哲学 ⭐

> **本条是四份报告中价值最高的一条，建议优先核对。**

- **现状**：`game_agent/runlog.py` 支持回放；审计不变量 `after == before + delta` 全程可回放（README）。
- **问题**：**"回放"有两种截然不同的实现，代价差一个数量级**——
  (a) **重放执行**：按日志重新调用工具，要求**所有工具幂等**；
  (b) **重建消息**：不重新执行，只把历史工具调用**重建**成 `assistant(tool_calls)` + N 条 `tool(result)` 消息。
  ST 明确选择 (b)：工具调用存进 `message.extra.tool_invocations`，
  下一轮由 `openai.js:996-1063` 从存档重建（token 预算不足就 `break`），**而不是重放**。
- **为什么对本项目特别重要**：本项目有随机检定（`noise`）、有概率事件（`chance`）、有 LLM 提取——
  这些**天然不幂等**。若 runlog 走 (a)，回放必然漂移；走 (b) 则只要求记录完整。**这是一个能省很多事的架构确认。**
- **ST 参照的附带细节（必须一起借）**：`signature` / `reasoning` 只在
  `originApi` + `originModel` 与当前一致时才回传，否则剥离（`openai.js:626-642`）。
  这是**跨模型续玩的必要条件**——本项目按用途分层路由（turn/judge/compress/extract、`llm.py`），
  换用途/换模型时若把上一模型的推理签名原样带上，会触发后端报错或污染。
- **方案**：
  1. **先核对**（不改代码）：读 `runlog.py` 与 `game.py` 的回放路径，确认是 (a) 还是 (b)；
  2. 若为 (a)：评估改为 (b) 的成本，重点是历史工具调用是否已完整落盘；
  3. 无论 (a)/(b)：检查跨模型续玩时是否残留上一模型的 reasoning/signature 类字段。
- **验收**：一份核对结论写回本文档或 `docs/design.md`；若改动，新增守卫测试
  「同一存档在两个不同用途模型下重建出的消息序列不含对方模型的私有字段」。

### B-3 工具的「动态可见性」与「stealth 通道」

- **现状**：`game_agent/registry.py` 是声明式工具层（ToolRegistry，批次 C）；
  `query_world` 是只读查询工具，引擎规则第 8 条要求模型「查到的才写，查不到的不得编造」
  且「查询结果只作叙事依据，不得原样复述给玩家」。
- **问题**：这两条要求目前是**提示词纪律**，不是**机制保证**。模型不遵守就会把查询结果复述进叙事，
  或者在不需要某工具的场合误调用。另外全量工具常驻会稀释注意力、浪费 token。
- **ST 参照**：
  1. **`shouldRegister()` 每次请求时求值**（`public/scripts/tool-calling.js:400-418`）——
     引擎按当前场景只暴露相关工具（房间里没门就不给 `open_door`）；
  2. **`stealth` 工具执行后不入 transcript、不触发后续生成**（`script.js:5421/5546`）——
     **引擎侧的状态查询 / 骰子 / 校验必须走这条路径**，否则污染叙事文本。
- **方案**：
  1. `ToolSpec` 增 `visible_when: <condition>`（复用 `conditions.py` 的判定 DSL，加载期校验）；
  2. 增 `stealth: bool`，stealth 工具的结果不进对话历史、不触发下一跳生成，只进 trace；
  3. `query_world` 改为 stealth —— 把"不得复述"从提示词要求升级为**结构上做不到**。
- **验收**：离线断言——stealth 工具的调用不出现在下一轮请求的消息序列中；
  `visible_when` 为假时工具不在下发列表中；两者均有守卫测试。

### B-4 按用途分层的**采样参数**与 reasoning 开关

- **现状**：`game_agent/llm.py` 已按用途分层路由模型（turn/judge/compress/extract/reflect/dedup）。
- **问题**：**分层路由了模型，未必分层路由了参数**。ST 的 `reasoning` 是一套可配模板
  （`reasoning.js:21-27`：`{name, prefix, suffix, separator}`，**前后缀为空即关闭**），
  默认模板为 `Think XML`，DeepSeek 预设为 `<think>\n` / `\n</think>` / `\n\n`；
  请求体带 `include_reasoning` / `reasoning_effort`（`openai.js:2820-2821`），
  且**不支持的源会被自动 delete**（`:2836/:2966/:3115`）而非报错。
- **为什么值得借**：judge / dedup / compress 这类**结构化子任务开 reasoning 通常有害**
  （拖慢、且模型容易在思考里"说服自己"给出宽松判定）；turn 需要文采但通常也不需要长思考。
  按用途关闭 reasoning 是**低成本的质量改进**。而它"空模板即关闭 + 不支持的源自动剥离"的失败姿态，
  正好是你要的：**配置错了就降级为关闭，而不是发一个被后端拒绝的请求**。
- **方案**：`llm.py` 的用途分层表增加 per-purpose 的 `temperature` / `reasoning`（开或关）；
  对不支持 reasoning 的后端**静默剥离该字段**（照抄 ST 的失败姿态）。
- **验收**：离线断言每个用途生成的请求体字段正确；不可用字段被剥离而非透传；
  真机抽查 judge 用途关闭 reasoning 前后的判定一致性（`scripts/judge_sensitivity.py`）。

### B-5 世界包的「名字型钩子」+ per-hook 超时

- **现状**：世界包是纯 YAML（`worldpack.py`），已能带自定义效果型工具
  （`CustomToolSpec`，`schedule.yaml` 的 `tools`，如校园包的 `write_letter`）。
- **问题**：世界包能声明**数据**（工具/事件/结局），但**不能声明行为**。
  任何新的行为耦合都需要改引擎——这削弱了 README 的核心主张
  「引擎零内容耦合是被世界包**验证过的**」。
- **ST 参照**：扩展的生命周期钩子是 **manifest 里的函数名字符串**，
  不是硬编码约定：`hooks: {activate: "init"}` → 宿主 `import` 后 `module["init"]()`
  （`public/scripts/extensions.js:406-466`）。
  且每个钩子有 **5 秒超时**（`HOOK_TIMEOUT = 5000`，`:447-459`）。
- **方案**：
  1. YAML 增 `hooks: {on_load: <函数名>, on_turn_end: <函数名>}`；
     引擎在**受控的注册表**中解析调用（不是任意 `import`，避免世界包变成代码执行入口）；
  2. **必须带 per-hook 超时与异常隔离**——一个写坏的世界包不能挂死游戏循环；
  3. 钩子失败按 ST 的姿态处理：**记录错误并继续**，不中断整局。
- **为什么排 P1 而不是 P2**：它是唯一能让内容作者在不碰引擎的前提下扩展行为的机制，
  直接服务于「换包即玩」的验证路线。但成本也是 B 系列最高的，**不建议与其他 P1 项并行做**。
- **验收**：离线——钩子超时被触发时回合仍正常结束；钩子抛异常时错误进 trace 且游戏继续；
  恶意钩子名（指向引擎内部函数）被注册表拒绝；世界包校验能识别声明的钩子是否存在。

---

## C 系列：内容层与工程配件（P2，遇到了再拿）

### C-1 内容层：NPC 卡缺「示例对话」⭐

> **本条是本项目内容层唯一真正的缺口，优先级高于所有其他 C 系列条目。**

- **现状**：`NpcSpec`（`worldpack.py:252-262`）字段为
  `identity / personality / speech_style / secrets / boundaries / forbidden / affection_stages / memory_limit`。
  实测 `su_wanying.yaml` 的 `speech_style: 言简意赅，剑修做派，偶引剑诀，从不说废话` ——
  **这是抽象形容词，靠模型自己翻译成语言。**
- **问题**：对「养成游戏 NPC 语气一致性」这个核心体感，两三个**具体示例**的边际收益
  **远高于**再加三行形容词。而 `affection_stages` 已经做了「好感阶段 → 语气」的分阶段描述，
  同样的思路套到示例对话上是自然的。
- **ST 参照**：`mes_example`。三个实现细节值得抄：
  1. **以 `role='system'` 投递，内容里带 `{{char}}:` / `{{user}}:` 前缀**（`openai.js:1115-1121`）
     ——不是伪造多轮 user/assistant，避开部分后端对角色交替的严格校验；
  2. **每个示例前插一条可配置的分隔提示词**（`new_example_chat_prompt`，`:1108`）把示例切成独立片段；
  3. **位置靠近生成点**：默认 order `main → worldInfoBefore → charDescription → dialogueExamples → jailbreak`
     （`default/content/presets/openai/Default.json`），越靠近对话末尾风格锚定越强；
  4. 预算不够就 `break`（`:1124`）——示例是低优先级内容。
- **方案（字段形状建议）**：按好感档位分组，与 `affection_stages` 的档位对齐：
  ```yaml
  example_dialogue:
    low:      # 对应 affection_stages 第一档
      - user: 苏师姐，这招我怎么都使不对。
        npc:  "手腕太松。"她看了一眼，没有伸手，"再练三天。"
      - user: 苏师姐要去哪儿？
        npc:  "剑峰。"脚步没停。
    high:
      - user: 师姐，你那天为什么替我挡那一剑？
        npc:  她沉默很久，才把剑收回鞘里："别问了。"
  ```
- **三条硬约束**：
  1. **绝不放进静态前缀**——会破坏 KV Cache 字节级冻结纪律（实测命中 99.3%），也让所有 NPC 示例常驻挤窗口；
     正确落点是 **NPC 在场时随角色卡进动态区**（动态区在对话之后，正好满足 ST 的"靠近生成点"经验）；
  2. **必须包一个 `<example>` 区块并显式标注是范例而非剧情**——
     few-shot 注入最常见的翻车方式就是模型把示例里的对话当成已发生的事；
  3. 加载期校验照旧：示例成对、非空、`npc` 行须符合该 NPC 的语气档位；
  4. **示例必须用引擎的占位符指代玩家与 NPC，不写死名字。** ST 用 `{{char}}` / `{{user}}` 在投递时替换
     （`openai.js:1115-1121`）。好处是同一份示例可跨世界包复用、玩家改名不破坏示例。
     若本项目暂无玩家名占位符（状态栏用的是 `player_role`），需一并定义。
- **验收**：离线——示例成对性/非空校验；示例不进静态前缀（复用既有前缀隔离测试）；
  仅在场 NPC 的示例被注入。真机——同一 NPC 在加入示例前后各跑一段，judge 复核语气一致性。

### C-2 内容层：多开局 = 多初始状态

- **现状**：`WorldSpec` 有单一 `start_scene`（`worldpack.py:43`）与单一 `opening`（`:49`）。
- **问题**：8 个在库世界包每个只有一条开局线，**重开一局的意愿与开局差异度成正比**。
- **ST 参照**：`alternate_greetings: []`（多开场白选择器，`characters.js:612`、`:573-577`）。
- **对本项目的额外价值**：ST 的多开场白只换文本；**本项目有真值状态，所以它能换状态**——
  每个档位带自己的 `start_scene` + 初始属性/好感/flag。**引擎本来就只认 `state`，不认开局从哪来，
  所以这是零引擎改动的内容层能力。**
- **验收**：`check-worldpack` 校验每个开局档位的 state 落点合法；离线断言不同档位开局产生不同初始 state。

### C-3 工程：名字型钩子的四件套门禁

- **ST 参照**：`requires`（依赖的能力模块）/ `dependencies`（依赖的其他扩展）/
  `minimum_client_version`（引擎版本门禁）/ `loading_order`（加载顺序），
  四项检查全部在激活前完成（`extensions.js:568-666`）。
- **本项目落点**：世界包可声明 `requires_engine: ">=1.2"` / `requires_packs: [...]` / `load_order`。
  现有 `check-worldpack` 做的是 schema + 交叉引用 + 可达性 + 数值上限，**版本与依赖维度是空白**。
- **验收**：老版本世界包在新引擎上给出明确提示而非崩溃。

### C-4 工程：失败只记录、不中断

- **ST 参照**：单个扩展激活失败写入 `extensionLoadErrors` 并在 UI 标红，**永不阻塞启动**（`extensions.js:665`）。
- **本项目落点**：世界包加载期同构——坏包报错可见，但其余包照常可玩。

### C-5 工程：merge-patch 的 UNSET 哨兵

- **ST 参照**：`'__@@UNSET@@__'` 区分「置 null」与「删除键」（`extensions.js:2061`）。
- **为什么需要**：世界状态打补丁时，`{"key": null}` 与「key 不存在」语义不同，
  现有 `effects` 结构在表达"移除某个 flag"时容易歧义。

### C-6 工程：内容哈希做变更检测

- **ST 参照**：`hash = getStringHash(JSON.stringify(entry))`（`world-info.js:4632`），
  用于增量更新向量索引。
- **本项目两个用途**：① 记忆检索/BM25 索引的增量更新；② 存档记 `pack_digest`
  （`docs/plan-creator-player.md:70` 已在做，ST 做法可作交叉验证）。

### C-7 工程：热路径缓存与预热

- **ST 参照**：`worldInfoCache = StructuredCloneMap({cloneOnGet: true})`（`world-info.js:882`），
  命中即返回深拷贝（`:2041-2043`），`CHAT_CHANGED` 时预热（`:1013-1018`）。
- **本项目落点**：世界包加载结果、token 计数都是天然可缓存项。

### C-8 工程：降级而非崩溃

- **ST 参照**：向量索引损坏时自愈重建（`src/endpoints/vectors.js:447-468`）。
- **本项目落点**：记忆检索 / BM25 索引同理——索引坏了应重建，而不是让整局失败。

### C-9 工程：可中断的提示词中间件链

- **ST 参照**：`generate_interceptor` 是 manifest 指向的函数名，按 `loading_order` 串行执行，
  签名 `fn(chat, contextSize, abort, type)` —— **可原地改消息数组，也可 abort 中断生成**
  （`extensions.js:2024-2049`）。
- **本项目落点**：这是「引擎强制接管关键节点」在 ST 里的对应位置——
  **状态注入、判定结果、可用工具都在这一层改写，而不是散落在业务代码里**；
  LLM-as-Judge 也应做成链上**可 abort 的一环**，而非特例分支。
- **排 P2 的原因**：本项目已有等价的强接管（关键节点固定选项 + flag 复核），
  但形状是"特例分支"而非"链上中间件"。值得在未来重构时收敛，**不是当下缺口**。

### C-10 工程：结构化报错带行列提示

- **ST 参照**：`SlashCommandParserError` 带 `line` / `column` / `hint`，
  渲染源码 3 行 + `^^^^^` 指示位（`public/scripts/slash-commands/`）。
- **本项目落点**：`check-worldpack` 的报错若带 YAML 行号 + 上下文，作者体验明显好于现在。

### C-11 工程：输入侧文本变换的「位置 + 深度范围」语义

- **ST 参照**：regex 扩展的 `placement` 7 值枚举（`MD_DISPLAY / USER_INPUT / AI_OUTPUT /
  SLASH_COMMAND / WORLD_INFO / REASONING`，`extensions/regex/engine.js:281-292`）
  + `minDepth` / `maxDepth`（`:362-372`）+ `promptOnly` / `markdownOnly` 双通道（`:348-355`）。
  管线执行顺序有显式文档（`script.js:1769-1787`）。
- **本项目价值**：文本变换很少，**但"只作用玩家输入""只作用最近 N 轮"这种声明式范围值得借**——
  比在代码里散落 `if` 判断清晰。

### C-12 工程：配置的缺键迁移

- **ST 参照**：`config-init.js` 的 `keyMigrationMap`（`:9-137`，连同**环境变量**一起迁移）
  + `_.defaultsDeep` 补齐缺失键并打印新增项（`:164-243`）。
- **本项目落点**：世界包 schema 演进时，给老包一条迁移路径，而不是直接判不合格。
- **参考（输入侧宽容）**：ST 的校验器逐级尝试 V1 → V2 → V3 判定来源版本
  （`validator/TavernCardValidator.js:32-48`），并在转换时把字符串与数组两种写法统一
  （`characters.js:573-577`：`alternate_greetings` 是字符串就包成单元素数组）、给缺失的可选字段补默认值
  （`:620-626`）。**读宽、写严**是值得照抄的输入姿态（详见 E-3）。

---

---

## D 系列：明确不借

### D-1 ~ D-3 「看似缺失、实为架构优势」的三条

> 这三条最容易误判为缺口，**写在这里是为了防止将来有人"补"上它们**。

| # | ST 的机制 | 为什么**不**借 |
| --- | --- | --- |
| D-1 | **World Info 的 `sticky` / `cooldown` / `delay` 定时效果**（`world-info.js:479-793`，含"消息删除/重 roll 后怎么办"的清理规则 `:619-660`） | 它之所以需要一套**按聊天消息条数计数的状态机 + 存 `chat_metadata`**，是因为 **ST 没有游戏状态**——一个纯聊天前端不知道"今天是第几天"。本项目有 `state.py` 的日程真值、`schedule.py`、`appointments`。lore 要"第 3 天后才出现"或"只在夜晚注入"，**正确做法是写进 `conditions.py` 的声明式条件 + 加载期校验**——更准、可回放、可审计。移植消息计数器是**架构倒退** |
| D-2 | **`group` 加权随机 + `probability` 掷骰**（`world-info.js:5041-5045`、`:5452-5465`） | 同一轮里"lore 注入与否"由随机数决定 = **不可复现的上下文**，直接与审计不变量 `after == before + delta` 全程可回放冲突 |
| D-3 | **7 种插入位置**（before/after char def、AN 上下、`atDepth`+role、示例区上下、`outlet`，`world-info.js:855-864`） | 这是为"角色卡 + 作者注 + 示例对话"的 Web 前端概念服务的。本项目上下文是「冻结静态前缀 + 动态追加」，**多开一个位置维度就多一份破坏 KV Cache 冻结的风险**。其中 `outlet`（不自动注入、按需取用）已由本项目的 `query_world` 只读查询覆盖，见 A-5 |

### D-4 ~ D-10 历史包袱

| # | 不借的东西 | 原因 |
| --- | --- | --- |
| D-4 | **PNG tEXt + base64 卡片信封**（`character-card-parser.js:28-29`，双写 `chara` + `ccv3`，读时 `ccv3` 优先 `:64-74`） | 纯粹是"角色卡要有头像"的产物。本项目世界包是 YAML 目录，**可 diff、可 review、可 CI 校验**；换成二进制图床是纯倒退 |
| D-5 | **手写校验器 + 宽松版本维度**（`validator/TavernCardValidator.js`，169 行：V1 查 6 字段存在、V2 查 14 个、**V3 只查 `spec` 与 `3.0 <= spec_version < 4.0`，`data` 是对象即放过** `:159-168`） | 本项目 `check-worldpack` 做的是 schema + 交叉引用 + 可达性 + 数值上限，领先不止一个身位。**只借它的"规格版本"维度**（见 C-3、C-12、E-3），不借它的校验强度 |
| D-6 | **角色卡可覆盖引擎提示词**（`data.system_prompt` / `data.post_history_instructions`，优先级链：全局预设 → 角色卡 → 聊天级） | 本项目 `ENGINE_RULES` 冻结在静态前缀里、世界包改不动——**这是对的，保持** |
| D-7 | **双宏引擎并存**（旧 `macros.js` 正则链 vs 新 `macros/` 的 lexer→CST-walker，靠 `experimental_macro_engine` 开关切换，旧引擎标 `@deprecated` 但仍在跑） | 纯技术债，迁移未完成 |
| D-8 | **巨型单文件**（`public/script.js` 12,600 行 / `openai.js` 7,300 行 / `slash-commands.js` 295 KB；一个函数承担 8 类插入策略） | 可维护性反面教材 |
| D-9 | **150 字段的 `getContext()` god-object**（`public/scripts/st-context.js:115-309`） | 它存在是为了打破前端循环 import。Python 侧应使用**显式依赖注入** |
| D-10 | **无沙箱的启动期 `git pull` 插件**（`enableServerPluginsAutoUpdate: true`，`plugin-loader.js:237-293`）在**启动路径**上拉取并执行任意代码；`docker-entrypoint.sh:18` 硬编码注入 `--listen`，叠加 root 运行与 compose 暴露 `0.0.0.0:8000` | 安全姿态反面教材 |

## E 系列：内容格式的生态互操作（P2）

> 这一系列不是"补功能"，而是**降低素材进出成本**。
> 本项目的定位是「引擎 + 世界包」，世界包的素材来源天然包括各类现成角色卡；
> 而 ST 恰好是这类素材的事实标准持有者。

### E-1 `character_book` ↔ World Info 的字段映射表（双向可用）

- **ST 的行为**：导出角色卡时，若它绑定了外部世界书，就把整本世界书**转进卡片的 `character_book` 字段**
  （`src/endpoints/characters.js:628-644` → `convertWorldInfoToCharacterBook` `:663-722`）。
  它解决的问题是"分发一张卡，世界书会丢"——本项目的世界包**天然没有这个问题**（`world.yaml` 自带 `lore`）。
- **对本项目的真正价值**：`character_book` 的条目 schema **就是 ST 世界书条目的换名映射**：

  | ST 世界书 | `character_book` 条目 | 本项目 `LoreSpec` |
  | --- | --- | --- |
  | `key` | `keys` | `keys` |
  | `keysecondary` | `secondary_keys` | `secondary_keys`（A-2 引入） |
  | `content` | `content` | `text` |
  | `comment` | `comment` | —（可用 `id` 承载） |
  | `order` | `insertion_order` | —（A-2 评分 + A-5 排序） |
  | `disable` | `enabled`（取反） | — |
  | `constant` / `selective` | `constant` / `selective` | `constant`（A-4 引入） |
  | `position: 0/1` | `position: 'before_char'/'after_char'` | —（D-3 不借） |
  | `/re/` 键 | `use_regex: true` | A-3 引入 |
  | `extensions.*` | 同名搬进 `extensions`（`characters.js:682-715`） | 逐项对应 |

  **一张表同时支持两个方向**：
  ① **导入**——把现成角色卡（含其内嵌 `character_book`）转成世界包的 `lore`，作为素材起点；
  ② **导出**——让作者把自己写的世界包导入 SillyTavern 试玩，**用别人的成熟前端验证自己的世界观**。
- **方案**：
  1. 新增 `game_agent/st_convert.py`，实现 `character_book_to_lore()` 与 `lore_to_character_book()`
     **两个方向**；往返转换以字段等价为断言（这是发现映射表错误最有效的手段）；
  2. `scripts/import_story.py` 增加 `--format sillytavern`；
  3. 校验落地：`entries` 必须是列表（ST 在导入时强制校验顶层含 `entries`，
     `src/endpoints/worldinfo.js:114-121`、`:143-149`），报错信息明确。
- **验收**：往返一致性测试（真卡语料放 `samples/`）；SillyTavern 实机导入试玩一次并在报告记录
  （仿 `docs/world2-report.md` 的验证附注体例）。

### E-2 包内资源引用的 URI 间接层

- **ST 参照**：`.charx`（zip 包）用 URI 前缀引用包内资源——`embeded://` / `embedded://` / `__asset:`
  （`src/charx.js:9-11`，**注释明确 `embeded` 的拼写是为了兼容 RisuAI 的既有导出**）；
  读取时统一解析并落盘到 `characters/<name>/` 等目录（`persistCharXAssets` `:309-399`）；
  解析时不识别的前缀会**报错**而非静默忽略（`getEmbeddedZipPathFromUri` `:112-131`）。
- **本项目现状**：世界包目前是纯 YAML、无外部资源引用。
- **何时需要**：一旦出现"世界包带自定义图/音频/长文本附件"，或者做单文件分发（导出存档、上传社区）。
- **方案**：定义 `asset://<相对路径>` 引用约定 + 一个统一的解析入口（加载期解析并校验存在性），
  **不要**让各处代码自己拼路径。
- **排 P2 的理由**：现在无此需求。但一旦要分发整包，URI 间接层是必需的——
  **这是"导出/分发"这一功能的核心设计，而 ST 已经踩过"没有间接层 → 换环境就失效"的坑**。
- **验收**：导出/分发测试中资源引用保持可用。

### E-3 「读宽、写严」的输入姿态

- **ST 参照**（三处合起来构成一条完整原则）：
  1. **多版本回退**：校验器逐级尝试 V1 → V2 → V3 判定来源（`TavernCardValidator.js:32-48`）；
  2. **类型归一**：`alternate_greetings` 是字符串就包成单元素数组，`tags` 是逗号串就切分
     （`src/endpoints/characters.js:573-577`、`:593`）；
  3. **缺省补齐**：`depth_prompt_depth` 缺省 4、`depth_prompt_role` 缺省 `'system'`（`:620-626`）。
- **对校验器设计的启示（本项目的空白）**：本项目的 `check-worldpack` 只有"严格校验"一条路径。
  但**素材导入是另一条路径**：直接拒绝等于把作者挡在门外。
  正确结构是**解析宽松 → 归一为标准模型 → 再严格校验**，
  并且把 `load_worldpack`（读宽）与 `check_worldpack`（写严）的职责边界写清楚。
- **方案**：`import_story.py` 的解析层接受宽松输入（ST 格式、字符串/数组混用、缺省字段），
  统一成标准模型后再走既有严格校验；**解析层与校验层分离**，避免校验器复杂度随来源增加而膨胀。
- **验收**：三份异形输入（字符串形式 tags / 缺省 depth / V1 老卡）解析后得到同一标准模型；
  解析层与校验层无交叉调用（可用 import 图或函数职责断言检查）。

---

## 附：其余值得记但暂不立项的点

来自提示词/宏/预设 / 扩展-工具调用 / 服务端安全三路的发现，本项目暂时用不上，但**记在这里以免将来重复调研**：

| 借鉴点 | ST 出处 | 本项目何时会用到 |
| --- | --- | --- |
| **marker + `prompts[]` / `prompt_order` 的「内容与位置分离」** | `PromptManager.js:80-199`，`Default.json:51-233` | Web 端做玩家可编辑提示词时。当前 `ENGINE_RULES` 冻结是对的，但接口形状可参考 |
| **ABSOLUTE + depth + order 注入三元组** | `PromptManager.js:31-40`，`openai.js:810-875`（注意 `injection_order` **数值越小越靠后** `:842`） | 若将来需要"某些世界包把状态栏插得更靠前"（长会话状态栏被淹没时），这个二元组是现成的参数化形状 |
| **`{{pick}}` 的位置感知确定性随机**（按 `chatHash + contentHash + offset + reroll_seed` 播种，嵌套下位置稳定） | `macros/definitions/core-macros.js:303-407` | 若引入任何世界包级别的文本随机化，必须用这种**可复现**的随机，而非 `Math.random()` |
| **本地(会话级)/全局(用户级)变量分界** | `variables.js:22-134`（`chat_metadata.variables` vs `extension_settings.variables.global`） | 多存档 / 玩家档案功能 |
| **每扩展独立设置槽** | `extensions.js:141` `extension_settings[<name>]` | 世界包的玩家级设置 |
| **工具实现即数据（DSL）**：`/tools-register` 用闭包定义工具，参数以 `arg.<name>` 注入作用域 | `slash-commands.js:1000-1149` | 已有 `conditions.py` 判定 DSL；可考虑同一套 DSL 三个消费者：作者调试 / 工具层 / Judge |
| **单一脚本语言服务多消费者** | STscript 既能被 Quick Runner 调用、也能被模型当工具调用（`/tools-invoke`）、还能定义工具 | 同上 |
| **向量/语义检索的「外部强制激活」接缝** | `world-info.js:1020-1027`（`WORLDINFO_FORCE_ACTIVATE` → `externalActivations` → `:4886-4890` **绕过所有关键词判定**无条件激活） | **中级成本、高价值**：本项目已有 BM25 记忆检索，接上就能让 lore 从"关键词命中"升级为"关键词 + 语义"双通道。ST 证明这个接缝必须是**旁路**，不能塞进主循环 |

---

## 附：评审的方法与限制

- 四路并行只读深挖，全部结论可追溯到 `file:line`；本次评审**未修改 SillyTavern 仓库任何文件**。
- **未在该 checkout 中找到**（已逐条核实，非推测）：
  World Info 条目级 AI 自动生成流程；World Info 扫描逻辑的单元测试（`checkWorldInfo` + `WorldInfoBuffer` +
  定时效果约 1300 行，**全项目最烧脑的部分零单元测试**，仅有 2 个 Playwright e2e 验证重命名后的引用改写）；
  `charaFormatV2` / `charaFormatV1` 常量与 JSON-Schema 校验；slash 命令的分类概念；
  `PARSER_FLAG` 的 `START_OF_INPUT` 等四个标志；随包发布的 web search / chat-history-search 函数工具；
  `{{summary}}` 内建宏（实为记忆扩展注册，扩展禁用即静默失效）；`tool_rp_*` 预设字段。
- **star 数未能核实**：本次环境对 `github.com` 的域名解析被策略拦截（仅 git 克隆可通），
  故本文档不引用任何 star / 用户量数字。
- **一条元建议**：ST 最强的工程实践恰恰是它的测试盲区（见上）。
  本项目的 648 项离线测试是**最明显的领先项**。因此 A、B、E 三系列**每一条都应先写测试再改**，
  尤其 A-2（四值逻辑）、A-3（正则键）、A-4（递归边界）、E-1（双向映射）、B-5（钩子）——
  这五处都是"只有边界条件才出错"的逻辑。
