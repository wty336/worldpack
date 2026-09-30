# 世界包作者手册

> 本手册 = 你写一个新世界包所需的全部知识。**不需要读引擎代码。**
> 目标读者：从未接触过 `game_agent` 引擎的内容作者。
> 写完按 §9 验收单逐项打勾，即得到"可过质量门"的世界包。
> 三份真实范例：`world-packs/ancient_jianghu/`（武侠）、`world-packs/xianxia_wendao/`（仙侠）、
> `world-packs/urban_neon/`（赛博都市）——写哪个文件卡住了，就打开对应范例抄结构。

---

## 1. 世界包是什么

世界包 = 一个文件夹，纯 YAML 内容，**不含任何代码**。引擎（Harness）负责对话循环、
数值、剧情状态机、事件、结局、校验与审计；你负责定义：

- 世界观与文风（`world.yaml`）
- 数值与日程（`schedule.yaml`）
- 主线节点链与关键抉择（`mainline.yaml`）
- 事件（`events.yaml`）
- 结局（`endings.yaml`）
- NPC 角色卡（`npcs/*.yaml`）
- Judge 对抗语料（`judge_corpus.yaml`，发布前必备）

```
world-packs/你的包名/          # 只允许字母/数字/_/-
├── world.yaml
├── schedule.yaml
├── mainline.yaml
├── events.yaml
├── endings.yaml
├── judge_corpus.yaml
└── npcs/
    ├── 角色A.yaml
    └── 角色B.yaml
```

**核心原则**：你只写"设定与规则"，不写"逐句对话"——对话细节由 LLM 按你的设定实时生成。
`goal`/`completion` 必须写成引擎可验证的 flag（见 §3.3），不能写"看心情完成"。

---

## 2. 十分钟上手

```bash
# 1. 生成骨架（六个带注释的 YAML 模板，模板本身就是合法可运行样例）
uv run python -m game_agent init-worldpack my_world

# 2. 逐个文件填内容（骨架注释里有每个字段的说明）

# 3. 离线校验（无需 API，报错信息会告诉你哪里错了、怎么改）
uv run python -m game_agent check-worldpack world-packs/my_world

# 4. 开玩（需 DEEPSEEK_API_KEY）
uv run python -m game_agent play world-packs/my_world
```

> 设计流程建议：先写 `world.yaml`（世界观）与一个 NPC → 再写 `schedule.yaml`（数值/行动）
> → 再写 `mainline.yaml`（3 个节点足够）→ 事件/结局 → 最后补 Judge 语料。
> 每一步都可以跑 `check-worldpack` 立即校验，不要攒到最后。

---

## 3. 六个文件逐个详解

### 3.1 world.yaml —— 世界观核心

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | 必填 | 游戏名，如 `问道长生` |
| `era` | 必填 | 时代/地点一句话，注入开场与判定材料 |
| `start_scene` | 选填 | 开局场景（写入状态） |
| `player_role` | 选填 | 玩家身份一句话（**常驻状态栏**，防剧情漂移） |
| `player_goal` | 选填 | 玩家长期目标一句话（常驻状态栏） |
| `core_rules` | 列表 | 世界核心规则，如货币/修炼体系 |
| `style_guide` | 列表 | 文风要求，**短句、明确**（注入系统提示词，逐条生效） |
| `forbidden` | 列表 | 禁用元素表（OOC 校验 + 选项过滤的机器依据，见 §5 陷阱 4） |
| `opening` | 选填 | 开场叙事（多行用 `\|` 块） |
| `lore` | 列表 | Lorebook 条目，按关键词命中后**按需注入**（字段见 §3.1.1） |
| `max_recursion` | 选填 | 递归扫描层数，**默认 0 = 关闭**（二级知识，见 §3.1.1） |

示例（《问道长生》节选）：

```yaml
name: 问道长生
era: 苍梧界·青云仙宗（架空仙侠）
start_scene: 苍梧界·青云山下
player_role: 流落凡尘的少年，身怀半枚残缺剑印
player_goal: 拜入青云仙宗，查明剑印的来历，在仙途上立足
forbidden:
  - 现代事物（手机、微信、汽车、互联网、照片等）
  - 现代流行语与网络用语
  - 不可让任何角色说破自己是"角色"或"AI"，不可提及玩家、作者、游戏
lore:
  - {id: jianyin, keys: [剑印, 残碑], text: 你身怀的半枚剑印纹路古朴，与秘境深处的一方残碑同源。}
```

**lore 写作要点**：`keys` 是触发关键词，写 2~4 字的实义词（地点名/人名/物名），**不要用
单字泛词**（如"剑""水"）——命中太多会挤占注入预算。`text` 两三句内。

#### 3.1.1 lore 条目的完整字段（SillyTavern World Info 对标改造后）

```yaml
max_recursion: 1          # 顶层开关：>0 才启用递归（默认 0 关闭）

lore:
  # 基本形态（最常用，够 90% 的场景）
  - {id: jianyin, keys: [剑印, 残碑], text: 你身怀的半枚剑印与秘境残碑同源。}

  # 次键收窄：命中「东市」之外还要命中其一（避免"东市"出现在任何闲逛里都注入）
  - id: dongshi_hu
    keys: [东市]
    secondary_keys: [胡商, 驼队]
    logic: AND_ANY
    text: 东市的西域胡商常在此卸货交易。

  # 次键排除：命中「诗会」但**没提到**夺魁规则时才补这条常识
  - id: shihui_rule
    keys: [诗会]
    secondary_keys: [夺魁, 头名]
    logic: NOT_ANY
    text: 诗会夺魁者名满京城，另得彩头赏银。

  # 正则键：匹配日期/编号等模式化词（/…/ 成对即正则）
  - {id: day_ritual, keys: ["/第\\d+天/"], text: 每日晨课在卯时。}

  # 常驻：世界通则，无需关键词命中（货币/历法/忌讳）
  - {id: currency, keys: [灵石], text: 灵石是修真界通用货币。}
    constant: true

  # 引擎强制接管的设定：不占预算、不受上限约束（慎用，见下）
  - {id: engine_hint, keys: [剑印], text: 剑印的真相由主线节点揭示。}
    ignore_budget: true
```

| 字段 | 默认 | 作用 |
| --- | --- | --- |
| `secondary_keys` | `[]` | 次键。**声明了它就必须显式写 `logic`**——加载期拒绝默认值静默决定语义 |
| `logic` | `AND_ANY` | 四值：`AND_ANY` 任一命中 / `AND_ALL` 全部命中 / `NOT_ANY` **全部未**命中 / `NOT_ALL` 任一未命中 |
| `case_sensitive` | `true` | 关闭后匹配英文/拼音的大小写变体（对中文无影响） |
| `constant` | `false` | 跳过关键词判定、总是候选；**仍受预算约束**（常驻 ≠ 无限） |
| `ignore_budget` | `false` | 不受预算限制。与 `constant` 是**两个独立维度**：一个管"要不要关键词"，一个管"占不占额度" |
| `exclude_from_recursion` | `false` | 只能被直接关键词命中，不被递归层激活 |
| `no_recursion_trigger` | `false` | 本条正文不作为下一层扫描输入（不传染） |

**递归（`max_recursion`）解决的是"二级知识"**：命中 A 才需要知道 B，而正文里不会同时出现
这两个词。经典用法：`青云仙宗` 的正文提到「剑峰」，递归一层即可带出 `剑峰/洗剑池` 条目。
**默认关闭**——开启前先确认真有这种链式依赖，否则只是让注入集合变大。

**两条调用纪律**：
1. `ignore_budget` 是给引擎强制接管的设定留的口子，**滥用等于让预算形同虚设**——
   先问"这条能不能用 `constant` 解决"；
2. 次键用 `NOT_ANY` 时，`secondary_keys` 要写**同义近义的一组词**（`[夺魁, 头名, 榜首]`），
   因为"没提到"的判定靠的是这组词一个都没出现。

**预算口径**：单轮 lore 注入按 **token**（1 字≈1 token 保守上界）计，预算 =
`5% × 模型上下文窗口`，上限 2500 token；未声明窗口时回退 1500。所以 `text` 越短、
能同时命中的条目越多——**别把一条 lore 写成一段设定文**，拆成多条更划算。

**forbidden 写作要点**：宁多勿少，它是 OOC 校验唯一的机器可读依据。注意**语义随世界观反转**：
古代/仙侠包要禁"手机、微信、AI"；赛博都市包里 AI、终端、义体是**合法元素**，要禁的是
"修仙、魔法、内力"与"现实品牌、第四面墙"——禁什么取决于你的世界观，引擎不做任何预设。

### 3.2 schedule.yaml —— 数值与日程

```yaml
day_action_points: 2          # 每日行动点数（1 或 2 随意）

stats:                        # 玩家属性：key 完全自定义（引擎无内置属性名）
  intel:    {label: 智识, min: 0, max: 100, initial: 10}
  credits:  {label: 信用点, min: 0, max: 999999, initial: 50}

affections:                   # 好感：每个 key 必须对应 npcs/<key>.yaml 角色卡
  lin_che:  {label: 林澈, min: 0, max: 100, initial: 5}

flags:                        # 剧情旗标：全部在此声明初始值，正文只引用不新增
  deal_made: false            # 已达成还债协议（节点完成信号）

counters:                     # 计数器（批次 E，可选）：计数型机制状态
  flower_gifts: {label: 赠礼次数, initial: 0, min: 0, max: 99}

items:                        # 物品（批次 E，可选）：持有/失去型机制状态
  - {id: sword, label: 听雨剑, initial: true}   # initial: 开局是否已持有

actions:                      # 日程行动
  - id: scavenge
    label: 接单拾荒
    cost: 1
    check: {stat: intel, difficulty: 6, margin: 10}   # 可选检定（D1）
    effects:
      stats: {credits: {base: 15, spread: 5}}          # 成功档（D3 收益曲线）
    critical_effects:
      stats: {credits: {base: 22, spread: 5}}          # 大成功档
    failure_effects:
      stats: {credits: {base: 8, spread: 3}}           # 失败档
    scene: 夜澜市·数据街
    present: []
```

- **检定（check）**：`roll = 属性 + uniform(-noise, +noise)`；`roll ≥ difficulty+margin`
  → 大成功，`≥ difficulty` → 成功，否则失败。各档独立效果，缺省回落成功档。
- **门槛（requires）**：条件 DSL（§4）——如"已结识且货币≥30"，不满足则行动不可选。
  消费型行动（花钱换好感的）**必须有门槛**，否则玩家可免费刷好感。
- **收益曲线**：效果值写数字 = 确定性；写 `{base, spread, decay_every, decay_step}` =
  范围随机 + 按执行前属性值边际递减（最低 0）。作者定义的效果越界一律**饱和**，不会炸档。
- `scene`/`present`：行动后玩家所在场景与在场 NPC（id）。声明了地点表时
  （§3.7），此处应写表内的 id 或显示名——表外 scene 会**加载失败**。

> **`present` 只是"这一幕的初始在场"**，不是整局的在场清单。叙事进行中角色进出，
> 由模型经 `change_presence` 工具（引擎内置，无需声明）实时同步——所以在 `present`
> 里留空、让角色按剧情自然登场，是完全正常的写法（例如"图书室自习"就该是 `[]`）。
> 需要注意的只是：**在场决定角色卡与记忆的注入**，所以某个角色若应在某行动里
> 稳定出场（如"拜访阿零"），就把他写进该行动的 `present`。

#### tools: —— 自定义效果型工具（可选，批次 C）

给叙述模型的"领域动作"：剧情走到某处时由 LLM 在叙事中调用、引擎结算效果
（修炼包的`突破`、都市包的`黑入终端`这类不进每日日程的动作）。

```yaml
tools:
  - id: breakthrough            # 工具名（不得与引擎工具 change_stat 等重名）
    label: 尝试突破
    description: 剧情推进到瓶颈时，玩家可以尝试冲击境界
    requires: {all: [{stat: {cultivation: {gte: 30}}}]}   # 可选门槛（§4 DSL）
    cost: 0                     # 可选：消耗行动点（缺省 0 = 纯叙事动作）
    effects:
      stats: {cultivation: 5}
      flags: {breakthrough_done: true}    # 写 flag 也要已声明
    once: false                 # true = 整局只能成功执行一次（开锁/引爆类）
```

- 效果引用的 stats/affections/flags **必须已在 schedule 声明**（加载期校验）；
- 拒绝场景（门槛不满足/行动点不足/once 已用）以结构化错误回传给模型，
  模型按叙事处理"做不到"，不会崩回合；
- 自定义工具的效果也是**节点 completion 的合法 flag 写入路径**（可达性校验认它）。

#### counters / items —— 计数器与物品（可选，批次 E）

flags 只能表达"是/否"，counters 与 items 补上"多少次"与"有没有"：

- **counters**（计数器）：`effects` 里写 `{counters: {flower_gifts: 1}}` 增减
  （边界饱和，审计入 stat_log）；条件 DSL 引用 `{counter: {flower_gifts: {gte: 3}}}`
  ——典型用法：**事件 when 引用计数器**（"三次赠礼触发支线"）；
- **items**（物品）：`effects` 里写 `{items: {gain: [sword], lose: [jade]}}`；
  条件 DSL 引用 `{item: {sword: true}}`（已持有）/ `{item: {sword: false}}`（已失去）
  ——典型用法：**行动/自定义工具 requires 引用物品**（"当剑后不可再修炼剑法"）；
- 两者都**只能由代码路径写入**（LLM 不可写，与 flags 同纪律）；状态栏展示
  "计数/持有"两行，事实图接地（叙事引用不算编造）；

#### 约定（appointments）—— 引擎内置，无需声明

**约定是引擎真值，不是记忆文本**——这是玩家实测缺陷（NPC 反复重问已经约好的事）
的修复。作者**不需要做任何声明**：只要 `schedule.yaml` 声明了 `affections`，
叙述模型就自动获得 `make_appointment` 工具。

```text
叙事中双方说定具体日期 → 模型调用 make_appointment(npc, what, due_day)
  → 引擎校验（NPC 白名单 / 日期必须晚于今天 / 内容 ≤40 字 / 同一约定不重复 / 待履行 ≤5 条）
  → 落盘为 state.appointments（一等真值，随存档）
  → 此后每轮状态栏 <约定> **无条件常驻注入**（不参与记忆检索竞争）
```

注入形态（三种标记由引擎按当前天数现算，不依赖模型回忆）：

```text
<约定>
- 与江屿约定：去学园祭 · 第 6 天          # 未到期
- 与夏鸣约定：还护膝 · 今日到期            # due_day == day
- 与温砚约定：看画展 · 已逾期未履行（约定日在第 4 天）   # day > due_day
</约定>
```

**为什么必须是一等真值**（写包时别再把约定塞进记忆或忽略它）：

1. 记忆池 `rank_facts` 单轮只注入 10 条（3 常驻 + 7 按分排），约定在
   **新近 / 重要性 / 相关性** 三维全吃亏——几天后 recency 衰减，
   且**到期日那天没人会提「周五」二字，BM25 检索必然落空**；
2. 事实提取提示词把"剧情进展的瞬时状态"排除在外，约定常被判为不值得记；
3. 记忆模型里**没有"到期"概念**，即使记下也没人会把它捞出来。

**对作者的含义**：

- 约定内容由模型自己写（引擎不生成），作者只需在设计剧情时让 NPC 提出**具体日期**
  的邀约——"改天一起吃饭"这种模糊说法不会落成约定（提示词第 11 条明确禁止）；
- 「逾期未履行」是**可用的剧情节拍**：约定没赴约会被显式呈现给模型（引擎规则
  第 11 条要求它按既成事实处理——解释、致歉或承担后果），作者可在结局条件里
  用 `flags` 呼应这类支线；
- 约定**不是 flags**：它带期限且状态由引擎算，不要在 `flags` 里手写 `met_friday` 之类
  的替代品——那会退回到"模型自己记"的老问题。

- 节点 completion 也可以引用 counter/item 条件——可达性校验同样覆盖：
  计数器必须有增减路径、物品必须可得/可失，否则加载失败。

### 3.3 mainline.yaml —— 主线节点链（最重要的文件）

```yaml
nodes:
  - id: n1_clinic_deal
    title: 白噪诊所
    when: {all: []}                       # 触发条件（§4）；空 = 开场即触发
    goal: 与白噪诊所达成还债协议           # 给 LLM 的任务卡（一句话，可验证方向）
    completion:
      flags: {deal_made: true}            # 完成信号：必须代码可验证
    on_enter:
      scene: 夜澜市·白噪诊所
      present: [lin_che]
      briefing: |                          # 节点任务卡：背景+本节点要发生的事
        你按匿名消息来到白噪诊所。医生林澈告诉你：你脑中那块记忆碎片并非你自己的……
        本节点目标：与诊所达成还债协议（完成信号 flag: deal_made）。
    critical_choices:                      # 关键抉择：引擎强制接管，只给固定选项
      - id: how_to_repay
        prompt: 手术灯下，林澈推过来一份账单。你如何还债？
        options:
          - {text: 接下霓光科技的寻回任务, effects: {flags: {deal_made: true, job_runner: true}, stats: {credits: 10}}}
          - {text: 帮诊所跑黑市渠道, effects: {flags: {deal_made: true, clinic_helper: true}, affections: {lin_che: 3}}}
    free_scope: 还债方式的细节自由发挥；协议达成即本节点完成。
```

**三条铁律**：

1. **completion 的每个 flag 必须有写入路径**（关键选择选项效果 / 事件效果 / 日程行动效果）——
   校验器会拒绝"永远完不成"的节点（LLM 没有写 flag 的权限）；
2. **关键分叉全部走 `critical_choices`**：选项效果写 flag，是多结局可回溯性的根基。
   日常对话中 LLM 不能替你完成关键分叉；
3. 节点串行推进：上一个完成才检查下一个的 `when`。第一个节点 `when: {all: []}` 保证开场即有主线。

### 3.4 events.yaml —— 事件

```yaml
events:
  - id: ev_clinic_night
    title: 诊所夜话
    trigger:
      kind: condition                  # condition / schedule / time 三选一
      when:                            # condition 时必填：任意条件 DSL
        all: [{affection: {lin_che: {gte: 30}}}]
    priority: high                     # high / normal / low（同时满足时的仲裁）
    script: |                          # 事件任务卡：给 LLM 扩写的脚本
      深夜的诊所，林澈摘下目镜，第一次谈起她离开澜生生物的原因，以及"零号计划"最初的样子。
    effects:
      affections: {lin_che: 5}         # 效果由代码结算（不是 LLM）
    once: true                         # 只触发一次
```

三种触发：`condition`（条件满足时，数值变化后检查）/ `schedule`（某行动后按 `chance` 0~1
概率）/ `time`（日期推进到 `when` 中的 day 条件时——**time 的 when 只能含 day**）。
触发判定全部由引擎代码完成，LLM 不参与。

**`once` 的准确语义**（校验批修复后，此前 `once: false` 是**死字段**——写了也只触发一次）：

| 写法 | 语义 |
| --- | --- |
| `once: true`（缺省） | 整局只触发一次 |
| `once: false` + `schedule` | **每次该行动命中概率都会触发**（"借钱""顺手带零食"这类可重复桥段） |
| `once: false` + `time` | **每个满足 `when` 的日子都会触发**（每日例事）；条件务必写窄，否则会天天刷 |

> 副作用提醒：`once: false` 的 `schedule` 事件每次触发都会结算 `effects`，
> 收益量级要按"一局可能触发很多次"来估（例：+2 好感 × 十几次 = 可观）。

### 3.5 endings.yaml —— 结局

```yaml
endings:
  - id: ending_freefall
    title: 自由落体
    kind: auto                          # auto = 条件满足自动进入（当前只支持 auto）
    when:                               # 条件 DSL，代码每轮自动判定
      all:
        - {stat: {credits: {gte: 200}}}
        - {flags: {tower_deal: true, confrontation_done: true}}
    text: |                             # 结局文本（玩家看到的）
      你把记忆碎片卖了个好价钱……
      ——结局达成：自由落体
```

**注意**：结局按文件顺序判定，**先写到的优先**（把最苛刻/最想要的结局放前面）。
每个结局的 `when` 必须与 `critical_choices` 的 flag 呼应——"哪个选择通向哪个结局"
要能回溯。留一个低门槛兜底结局（如"第 N 天且某属性过低"），避免玩家永远玩不到头。

### 3.6 npcs/*.yaml —— 角色卡

```yaml
id: lin_che                          # 与 schedule.affections 的 key 一致
name: 林澈
identity: 白噪诊所的神经外科医生，前澜生生物研究员
personality: 冷静克制，冷面心热，医者底线极硬
speech_style: 语气平稳克制，医学术语精确，很少用语气词

secrets:                             # 只写不注入——剧情揭示前 LLM 看不到
  - 她当年参与了"零号计划"的初期研究，后因无法接受实验对象的下场而离开

boundaries:                          # 底线：OOC 校验依据
  - 不轻易谈论离开澜生生物的原因
forbidden:                           # 角色禁忌：OOC 校验依据
  - 不直接说破自己是"角色"或"由程序生成"

affection_stages:                    # 好感阶段语气：区间升序、无重叠、覆盖 [min, max]
  - {range: [0, 20], tone: 公事公办，保持距离}
  - {range: [21, 50], tone: 语气渐软，偶有关切}
  - {range: [51, 80], tone: 推心置腹，主动相帮}
  - {range: [81, 100], tone: 生死相托，愿为你破例}

memory_limit: 20
```

**阶段区间规则**（校验批起为**加载期硬校验**）：

- 区间必须**升序、不重叠、覆盖 `[affections.<id>.min, max]`**（角色卡的好感范围由
  `schedule.yaml` 声明，未声明则按 `[0, 100]`）；
- 相邻区间之间**允许 ≤1 的缝**（因为大家都写 `[0,20] [21,50]` 这种整数区间），
  **缝宽 >1 会被拒绝**——留下真空气隙时，落在里面的好感值语气会退化成
  「（无阶段定义）」；
- 好感是 **float**（`change_stat` 可传 0.5、收益曲线会产生小数），
  `20.5` 这类值由引擎归给**下段**（按整数区间的自然读法），所以写整数区间是安全的；
- `memory_limit` 必须 **≥1**：写 0 会让该 NPC 的记忆"写一条淘汰一条"却回报已写入，
  功能静默全灭。

**secrets 的用法（重要）**：`secrets` 是给你（作者）和未来剧情揭示机制用的，**不会注入
上下文**。因此：正常对话里 NPC 不得说出 secrets 内容；想让 NPC 在某个好感阶段透露秘密，
就把"已可透露"写进对应 tone 或事件脚本。同理，Judge 语料的正常用例**不得暗示 secrets**
（见 §7 陷阱 3）。

### 3.7 world.yaml 的 locations —— 地点表（可选，批次 D）

不声明时场景（scene）是自由字符串，行为与旧版一致；声明后场景升级为**受引擎校验的
一等公民**：玩家"走到哪"由 change_scene 工具提议（LLM 只能选表内 id），引擎复核后写入
状态栏真值。

```yaml
locations:
  - id: clinic          # 引擎/工具使用的标识（唯一）
    name: 夜澜市·白噪诊所   # 状态栏与场景卡的显示名（唯一）
    keys: [白噪诊所, 诊所]   # 在此地点时恒参与 lore 命中（§3.1 的 lore）
    description: 城北地下室里的非法义体诊所   # 给 change_scene 提议参考，不注入状态栏
```

- **声明后的硬规则**：`mainline` 节点 `on_enter.scene`、`schedule` 行动 `scene`、
  `world.start_scene` 都必须写**表内的 id 或显示名**，否则加载失败；
- lore 触发升级：所在地点的 `keys` 恒参与命中——走到诊所，关于诊所的 lore
  无需玩家恰好提到"诊所"两个字；
- 何时值得声明：世界包有 ≥5 个会反复出现的地点、或希望"移动"成为玩法的一部分。
  线性小包不必声明。

---

## 4. 条件表达式 DSL（when / completion / requires / 结局通用）

```yaml
{all: [条件, ...]}                    # 且
{any: [条件, ...]}                    # 或
{not: 条件}                           # 非
{day: {gte: 5, lte: 10}}              # 游戏内天数
{flags: {joined_sect: true}}          # 旗标相等（值必须是 true/false）
{stat: {xiu_wei: {gte: 35}}}          # 玩家属性比较（属性名 = schedule.stats 的 key）
{affection: {lin_che: {gte: 50}}}     # 好感比较（id = affections 的 key）
{} 或 {all: []}                       # 恒真
```

操作符：`eq / ne / gt / gte / lt / lte`。结构非法或引用未声明的名字会在**加载期直接报错**，
拼写错误不会静默通过。

> **但效果键要写对**（校验批实测的坑）：**条件侧**用单数键 `stat` / `affection`
> （`{stat: {xiu_wei: {gte: 35}}}`），**效果侧**用复数键 `stats` / `affections`
> （`effects: {stats: {xiu_wei: 5}}`）。写反了不会报错而是**静默无效**
> ——`effects: {stat: {...}}` 会被 pydantic 忽略（`extra="ignore"`），
> 行动看起来生效、实际什么都没发生。判据：跑一遍 `check-worldpack` 后看状态栏数值有没有动。

---

## 5. 作者编写守则（14 条）

1. 大纲只写**节点与关键选择**，不写完整对话——细节交给 LLM 扩写；
2. `goal`/`completion` 必须写成代码可验证的 flag（"获得诗会头名" → `poetry_top3: true`）；
3. **completion 可达性**：completion 要求的每个 flag 至少要有一条代码路径可写
   （关键选择选项效果 / 事件效果 / 日程行动效果）——LLM 没有 flag 白名单；
4. `forbidden` 表宁多勿少，但**随世界观定方向**（古代禁现代 / 都市禁奇幻），别照抄别的包；
   **每条至少写一个具体禁用词**（放括号或用顿号分隔）：整句概括性表述切不出可匹配词，
   那条防线会静默失效——加载期硬校验会拒绝这种写法；
5. 每个结局的 `when` 与 `critical_choices` 的 flag 呼应，保证可回溯；
6. 关键选择选项**全部**写入同一节点的完成 flag（否则玩家选了也完不成节点）；
   每个关键抉择**至少 1 个选项**（空 `options` 会让游戏永久死锁）；
7. 消费型行动（花钱换好感）必须配 `requires` 门槛，防免费刷数值；
8. 好感阶段区间**升序、不重叠、覆盖该 NPC 的 `[min, max]`**（缝宽 ≤1）；
   正常用例/叙事举止与所处好感阶段一致；
9. lore 的 `keys` 用实义词，不用单字泛词；写次键就**必须**写 `logic`（别依赖默认值）；
   正则键用 `/…/` 成对形式，单斜杠是字面量；
10. 属性 key/好感 id/flag 名全包统一（推荐 `snake_case` 或拼音），正文只引用不新增；
11. **数值可达性（校验批起为加载期硬校验）**：每个结局的数值阈值必须能在
    "该维度独占全部行动点"的上界之内达成，否则**确定不可达**、加载直接报错。
    预算 = `最大 day 门槛 × day_action_points`。**给每个可攻略 NPC 至少一条日常收益路径**
    （只有剧情事件加好感是不够的：纯剧情最多给到十几点，够不着 50 这类阈值）；
12. **实体 id 全局唯一**：节点 / 关键抉择 / 日程行动 / 事件 / 结局 五类各自不得重名
    （重复不会崩，但会语义错位：节点重复 → 第二个永久不可达；行动重复 → 点 B 执行 A）；
13. 每日行动点、行动收益、结局阈值**先算一遍数值闭环**——`check-worldpack` 会替你验算
    结局阈值是否够得着，但"够得着"不等于"不紧张"：算一下攻略一个角色要花掉多少行动点占比；
14. 世界包发布前必须过 §9 质量门。

---

## 6. 陷阱清单（全部是真实踩过的坑）

| # | 陷阱 | 后果 | 校验器/门禁会拦吗 |
| --- | --- | --- | --- |
| 1 | completion 的 flag 没有写入路径 | 节点永远完不成 | ✅ 加载期拒绝 |
| 2 | time 事件的 when 含 day 之外的条件 | 时间触发语义破坏 | ✅ 加载期拒绝 |
| 3 | 好感对象没有对应 `npcs/<id>.yaml` | 角色卡缺失 | ✅ 加载期拒绝 |
| 4 | 禁表词粒度：2~6 字短词才会被选项过滤提取——"AI"嵌在长句里不会成为过滤词 | 以为禁了其实没禁 | ❌ 按短词写禁表（"AI"单独成词） |
| 5 | 都市/近现代包照抄古代包的禁表（禁"手机/AI"） | 合法世界观元素被误禁，文风崩溃 | ❌ 自己对照世界观检查 |
| 6 | 结局顺序随便排 | 条件同时满足时进错结局 | ❌ 最想要的结局放最前 |
| 7 | `on_enter.present` / 行动 `present` 写了不存在的 NPC id | 加载失败 | ✅ 加载期拒绝 |
| 8 | lore `keys` 为空或 text 为空 | 注入逻辑失效 | ✅ 加载期拒绝 |
| 8a | lore 写了 `secondary_keys` 但没写 `logic` | 默认值静默决定语义，作者意图落空 | ✅ 加载期拒绝 |
| 8b | 正则键写错（`/(未闭合/`） | 该条永不命中（曾会静默失败） | ✅ 加载期拒绝 |
| 8c | `constant: true` 当成"无限注入"用 | 预算被常驻条目吃光，命中的补充设定挤不进来 | ❌ 离线预算测试会抓（见 §9） |
| 8d | `max_recursion` 开了但条目之间没有真实链式依赖 | 注入集合无谓变大 | ❌ 靠作者自查（默认关闭即不受影响） |
| 9 | 语料正常用例的叙事前提（已入门/已结识/约定）没进材料 | Judge 误判（它判得对，是你没给材料） | ❌ 真机门禁会抓（见 §7） |
| 10 | 语料正常用例出现"在场：无"却有角色互动 | 同上 | ❌ 真机门禁会抓 |
| 11 | 语料正常用例暗示 secrets（不注入材料） | 同上 | ❌ 真机门禁会抓 |
| 12 | 数值闭结算错（阈值过高/过低） | 结局不可达或过早结束 | ❌ 离线通关测试/真机冒烟会抓 |

---

## 7. E1 Judge 语料写作规范（judge_corpus.yaml）

Judge 语料 = 你们包的质量门禁标尺：故意违规的样本（应被 Judge 拦截）+ 正常样本（应放行），
真机跑 `judge_sensitivity.py` 测 Judge 判据对你这个世界的灵敏度。

**规模要求（离线测试强制）**：每类对抗样本 **≥5 条**、正常 **≥10 条**——
推荐与范例包对齐：**6 OOC + 6 设定矛盾 + 6 虚构事实 + 12 正常 = 30 条**。

```yaml
cases:
  - id: ooc_fourth_wall            # 类别：ooc / setting / confab / normal
    category: ooc
    narration: |-                  # 待判定的一段叙事
      阿零忽然凑近，压低声音：『告诉你个秘密——我们其实是被 AI 写出来的角色。』
    expected: false                # false = 应被拦截；true = 应放行
    day: 8
    scene: '夜澜市·旧终端区'
    present: [a_ling]              # 在场 NPC（决定材料里注入谁的角色卡）
    facts: ['你与阿零今日初遇']     # 玩家关键事实（进材料）
    npc_memories:                  # NPC 记忆（进材料）
      lin_che: ['玩家曾在白噪诊所接受义体手术']
    note: '违规点在哪、判定依据是什么（对抗样本必填）'
```

**三条材料纪律（真机门禁实测教训，违反必被抓）**：

1. **违规点必须在材料内可见**——Judge 只看 `build_materials`（场景卡+身份+状态栏+关键事实+
   在场角色卡），看不到你的 `forbidden` 表与 lore。用例要自证：OOC 靠角色卡底线/语气、
   设定矛盾靠状态数值/事实、虚构靠关键事实对照；
2. **正常用例的叙事前提必须显式进材料**：叙事是"已入门后随众弟子操练"→ 就加
   `facts: ['你已拜入青云仙宗']`；叙事是同游的亲近举止 → 就加 `affections: {xx: 55}`；
   叙事说"备份已清掉" → 材料里必须有"备份存在+清理之约"的前提。**否则 Judge 判
   "虚构事实"判得完全正确，是你用例没给前提**；
3. **`present` 为空（材料"在场：无"）时，叙事不得出现任何互动角色**；不得暗示
   `secrets` 内容（secrets 不注入材料）。

---

## 8. 接入统一冒烟（scripts/worldpack_smoke.py）

质量门第 4 步需要一个**包内 profile**——打开 `scripts/worldpack_smoke.py` 的 `PROFILES`，
以你的包目录名为 key 加一项：

```python
"my_world": {
    "picks": {"第一个抉择id": 0, "第二个抉择id": 1},   # 每个关键抉择选第几项（0 起）
    "action": "你的主力日程行动id",
    "days": 8,                                        # 冒烟日数预算
    "lines": ["日常对话台词1", "台词2", "台词3"],       # 3~5 句，按包风格写
    "forbidden_scan": ["本包禁表里的关键词…"],          # 硬失败扫描（跨体裁泄漏词等）
    "observe_modern": [],                             # 可选：合法世界观元素出现统计
    "target": "目标结局名（仅作日志展示）",
},
```

---

## 9. 发布验收单（质量门）

逐项执行，全部通过即"可过质量门"：

```bash
# ① 离线校验（schema + 交叉引用 + 可达性）
uv run python -m game_agent check-worldpack world-packs/<你的包>

# ② 全量离线测试（你的语料会自动纳入规模门禁：每类≥5/正常≥10）
uv run pytest

# ③ E1 Judge 门禁（真机，约 ¥0.1；目标：对抗 100% 拦截、正常 0% 误报）
uv run python scripts/judge_sensitivity.py --pack world-packs/<你的包>

# ④ 真机冒烟（通关一个目标结局 + 审计零偏差 + 禁表扫描零泄漏）
uv run python scripts/worldpack_smoke.py --pack world-packs/<你的包>

# ⑤（可选深度）100 回合长局：压缩/记忆/检索/审计不腐化（约 ¥1-2）
uv run python scripts/longrun_probe.py --pack world-packs/<你的包> --turns 100
```

**推荐加分项**：仿照 `tests/test_second_worldpack.py` 写一份你包的离线全流程测试
（FakeClient 通关 + 审计），让引擎机制对你的包有永久回归保护。

---

## 10. 范例索引

| 想知道…… | 看哪个文件 |
| --- | --- |
| 古风武侠包全貌 | `world-packs/ancient_jianghu/`（30 条语料的基准） |
| 检定三档 + 门槛 + 收益曲线 | `world-packs/xianxia_wendao/schedule.yaml` |
| 时间触发事件 | `world-packs/xianxia_wendao/events.yaml`（宗门小比） |
| 禁表"反向"写法（近未来） | `world-packs/urban_neon/world.yaml` |
| 双 NPC + 好感阶段写法 | `world-packs/urban_neon/npcs/*.yaml` |
| 语料"正常用例前提进材料" | 任意包的 `judge_corpus.yaml` 中带 `facts:` 的 normal 用例 |
| 收益曲线/结局条件 DSL | `world-packs/urban_neon/endings.yaml` + `schedule.yaml` |
| **counters / items / 自定义工具 / 地点表全用上** | `world-packs/campus_otome/`（材料→点心→赠礼闭环、`write_letter` once 工具、8 地点、5 节点 10 结局） |
| **多攻略对象的分线结局判定顺序** | `world-packs/campus_otome/endings.yaml`（真结局→普通→未果→友情→自我→兜底） |

---

*手册与代码冲突时，以 `check-worldpack` 的报错信息与代码为准，并回写本手册（最小 diff）。*
