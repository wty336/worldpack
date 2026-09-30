# LLM Agent Runtime：文字互动养成游戏引擎

[![CI](https://github.com/wty336/worldpack/actions/workflows/ci.yml/badge.svg)](https://github.com/wty336/worldpack/actions/workflows/ci.yml)

> 一套手写的 Agent Harness，驱动长线多轮文字互动游戏：**模型只管生成，代码掌握真值**。
> 引擎只做一次，内容（世界包）换一换就能开新游戏——已验证武侠 / 仙侠 / 赛博都市 / 现代校园 / 校园乙游 / 80 年代 / 太空科幻八种世界配置复用同一引擎。

为什么文字养成游戏是好的 Agent 测试台（也是本项目所有工程决策的来源）：

1. **长会话**：300+ 轮的养成局，上下文工程被迫做真——静态前缀字节级冻结吃 KV Cache（实测命中 99.3%），增量摘要压缩把输入从线性 ~280K 压到 20–31K 持平（零熔断）；
2. **强状态真值**：数值/flag/物品有代码维护的唯一真值，幻觉有可判定的对错——LLM 只能经工具契约**提议**变更，引擎校验后落盘，审计不变量 `after == before + delta` 全程可回放；
3. **对抗性输入**：玩家天然是提示注入来源——输入侧声明为"角色扮演内容而非引擎指令"，密语金丝雀 + 禁表校验 + 数值白名单三层防御，实测零泄露；
4. **领域可替换**：引擎零内容耦合是被世界包**验证过的**——`forbidden` 表语义反转（古代禁手机/AI，赛博都市反禁奇幻元素）、加载期可达性拒绝，证明约束是纯数据不是硬编码。

## 架构

```
┌──────────────────────────── 前端层 ────────────────────────────┐
│  CLI (REPL)    Web (FastAPI + SSE)    MCP Server (stdio)       │
├──────────────────────── 引擎层 Harness ────────────────────────┤
│ 对话循环        上下文组装（静态前缀冻结 + 动态追加）              │
│ 剧情状态机      ToolRegistry（声明式工具层，批次C）                │
│ 事件/日程/检定   记忆系统（检索注入 / 冲突消解 / 反思洞察）         │
│ 结局判定        LLM-as-Judge（事实图缺席检查 + 三态语义判定）      │
│ 审计回放        压缩 / usage 记账 / trace / token 校准            │
├──────────────────────── 内容层（纯 YAML）───────────────────────┤
│  world-packs/*：world / npcs / mainline / events / endings /    │
│  schedule（可选 tools / locations / counters / items）           │
├──────────────────────── 模型层（可替换）────────────────────────┤
│  OpenAI 兼容 API，按用途分层路由（turn/judge/compress/extract/…）│
└─────────────────────────────────────────────────────────────────┘
```

一轮交互的闭环：**输入 → 上下文组装 → LLM 工具调用（change_stat / remember / query_world → submit_narration 收尾）→ 代码校验落盘 → 剧情状态机 / 事件检查 → 结局判定**。关键剧情节点由引擎强制接管（固定选项 + flag 复核），节点之间自由生成。

## 演示（真机冒烟实录摘录）

```text
【关键抉择·how_to_help】几个纨绔正纠缠一位姑娘。你如何解围？

那女子立于阶下，脊背挺直，眉眼清冷……"多谢公子仗义。公子面生，想必是初到长安。"
——她顿了顿，语气平和，字字妥帖，却仍隔着一层公事般的疏离。

[状态] 属性 {'charm': 10.0, 'martial': 21.1, 'silver': 50.0} · 好感 {'shen_qingqiu': 16.0}
[✓] 数值零偏差审计通过（13 条变更记录）
[✓] 禁表扫描通过（无世界观外元素）
```

质量口径：**875 项离线测试全绿** · E1 对抗门禁 110 语料×3 轮（三类拦截 100%、正常误报 6%）· 300 轮长局零熔断 · 单局成本 ¥2–4、一次质量门 ¥0.1–0.3 · 缓存命中 99.3%。完整证据见 `docs/plan-design-hardening.md`、`docs/p1-report.md`、`docs/m2b-postmortem.md`。

![Web 前端真机截图](docs/assets/web.png)

*Web 前端（真机截图）：关键剧情节点由引擎强制接管（只给固定选项）；下方是引擎注入给模型的状态栏原文——场景 / 身份锚点 / 属性好感 / **计划块**（plan-and-execute 的 `[→]` 指针，由 flag 真值推进）/ 在场角色卡。玩家看到的与模型看到的同源，这正是"代码掌握真值"的可视化。*

## 快速开始

```bash
# 1. 配置 API Key
cp .env.example .env        # 填入 DEEPSEEK_API_KEY

# 2. 安装依赖（uv）
uv sync

# 3. 校验世界包（离线，无需 API）
uv run python -m game_agent check-worldpack                     # 默认《江湖旧梦》
uv run python -m game_agent check-worldpack world-packs/xianxia_wendao  # 换包校验

# 4. 跑测试
uv run pytest

# 5. 开玩（需 API Key）
uv run python -m game_agent play world-packs/xianxia_wendao     # CLI 玩《问道长生》
uv run python -m game_agent play world-packs/urban_neon         # CLI 玩《霓虹深处》
uv run python -m game_agent web --pack world-packs/xianxia_wendao  # Web 玩《问道长生》
uv run python -m game_agent mcp world-packs/xianxia_wendao      # MCP server（外部客户端可玩）
```

## 写新世界包

```bash
uv run python -m game_agent init-worldpack my_world   # 生成带注释骨架
# 按 [世界包作者手册](docs/worldpack-manual.md) 填六个文件 + Judge 语料
uv run python -m game_agent check-worldpack world-packs/my_world   # 离线校验
```

## 质量门（换包/发布前四连，见 docs/plan-worldpack-qa.md）

```bash
uv run python -m game_agent check-worldpack world-packs/<pack>          # L1 离线校验
uv run pytest                                                           # L2 全量离线测试
uv run python scripts/judge_sensitivity.py --pack world-packs/<pack>    # E1 Judge 门禁（真机）
uv run python scripts/worldpack_smoke.py --pack world-packs/<pack>      # 真机冒烟（通关+审计+禁表）
# 一键版（Track B/B2）：分层门禁 + 报告卡（reports/qa_gate_*.json）
uv run python scripts/qa_gate.py --pack world-packs/<pack> [--levels l1,l2,l3] [--offline] [--dry-run]
# 深度检查（可选，约 ¥1-2）：100 回合长局——压缩/记忆/检索/审计不腐化
uv run python scripts/longrun_probe.py --pack world-packs/xianxia_wendao --turns 100
# 素材导入（工具 B）：小说/大纲/设定 → 世界包（生成语料为草稿质量，过门禁按报告手工修）
uv run python scripts/import_story.py 素材.md --name my_world --with-corpus --live
# 约定工具真机探针（约 ¥0.06/3 次）：模型是否真的把"约好某天做某事"交给引擎
uv run python scripts/appointment_probe.py --runs 3
```

推送与 PR 由 CI 自动执行 L1+L2（badge 在页首）。

## 设计文档

- [总体设计文档](docs/design.md)——架构、上下文工程、数值系统、剧情状态机、世界包规范
- [世界包作者手册](docs/worldpack-manual.md)——写给内容作者的完整手册：schema/守则/陷阱/语料规范/验收单（写新世界包从这里开始）
- [设计加固计划](docs/plan-design-hardening.md)——六批改进的完整档案：证据面收口 / 校验闭环 / ToolRegistry+MCP / 地点一等公民 / counters+items / token 校准 + 子代理审查修复记录（**全部落地**）
- [改进路线图](docs/improvement-roadmap.md)——外部对标评审结论与分批改进清单（P0~P3，已完成）
- [审查修复计划](docs/review-fix-plan.md)——四批次代码审查发现（1 Critical / 7 Major / 6 Minor）与分批修复方案
- [M1 实施计划](docs/plan-m1.md) / [M2 计划](docs/plan-m2.md) / [M1.5 计划](docs/plan-m1-5.md)——里程碑工作包与验收标准
- [M1 复盘](docs/m1-postmortem.md) / [M2a 复盘](docs/m2a-postmortem.md) / [M2b 复盘](docs/m2b-postmortem.md)——测量教训与踩坑地图
- P0~P3 执行报告（[p0](docs/p0-report.md) / [p1](docs/p1-report.md) / [p2](docs/p2-report.md) / [p3](docs/p3-report.md)）——路线图四批：Judge 灵敏度 / 记忆升级+成本记账 / 数值深度 / Lorebook+脚手架+Web
- [world2 报告](docs/world2-report.md) / [world3 报告](docs/world3-report.md)——第二/三个世界包的通用性验证（forbidden 表语义反转）
- [SillyTavern 借鉴清单](docs/sillytavern-borrow.md)——对上游 1.19.0 的一次全量源码评审：值得借什么（A/B/C/E 系列）、**明确不借什么**（D 系列，含三条"看似缺失实为架构优势"）
- [创作者/玩家模式方案](docs/plan-creator-player.md)——剧本平台化：目录层 / 会话选包 / 两个工作台 / 准入闸门（引擎零改动）
- [对标 dsh-tavern：差距分析与改造方案](docs/plan-dsh-tavern-parity.md)——同类产品的实测拆解、四条路线的取舍与取舍理由（含"为什么不做成 DSH 插件"的宿主侧硬证据）
- [做成 dsh-tavern 那种效果](docs/plan-tavern-shaped-product.md)——三条能力（选卡自由游玩 / 绑定素材沿主线 / 对话式改卡）的实现路径、
  前端选型（Vue 3 + Vite 三阶段）与引擎侧改造清单（含执行记录）

## 目录结构

```
game_agent/        # 引擎包（与内容无关的 Harness 层，31 个模块）
world-packs/       # 世界包（纯 YAML 内容，换一包换一个游戏，8 个在库）
  ancient_jianghu/ # 武侠《江湖旧梦》
  xianxia_wendao/  # 仙侠《问道长生》
  urban_neon/      # 赛博都市《霓虹深处》
  campus_otome/    # 校园乙游《青槐高中·告白之前》（3 攻略 + 1 闺蜜 · 5 养成轴 · 10 结局）
  …                # 现代校园 / 80 年代 / 太空科幻 / 中性探针
tests/             # 离线测试与守卫（875 项，CI 执行）
docs/              # 设计文档 / 计划 / 复盘（26 份 ADR 级记录）
scripts/           # 质量门 / 冒烟 / 长局探针 / 素材导入等 38 个工具
eval-sets/         # 评测语料（**运行时门禁用**：注入防御 / 记忆冲突 / 去重 / extract）
archive/finetune/  # 已归档的微调线（数据 / 产线 / 训练器 / 计划 / 证据，见其 README）
```

## 当前状态

- **设计加固六批 ✅（2026-09-25）**：判官证据面（事实图纳入摘要/选择日志、连贯性材料）、校验闭环（确定性层每轮常开 + LLM 层降频采样 + 反馈复查）、ToolRegistry 声明式工具层 + MCP server、地点一等公民、counters/items 机制表达力、token 校准；子代理全量审查 1C/2M/10m 全部修复。真机冒烟零偏差零泄漏（`docs/plan-design-hardening.md`）
- **约定真值 ✅（2026-09-25）**：修掉玩家实测缺陷——NPC 反复重问**已经约好**的事（"周五去学园祭"约完又被问"周五有空吗"）。根因不是模型记性差，而是约定**不是引擎真值**：记忆池单轮只注入 10 条（约定在新近/重要性/相关性三维全吃亏，且**到期日没人提"周五"，BM25 检索必然落空**）、提取提示词把"剧情进展的瞬时状态"排除在外、记忆模型**没有"到期"概念**。修法：`state.appointments` 一等真值 + `make_appointment` 工具（模型提议 → 引擎校验 → 落盘）+ 状态栏 `<约定>` **无条件常驻注入**（到期/逾期由引擎按天数现算）+ 引擎规则第 11 条（约定纪律）。守卫 `tests/test_appointments.py`（15 项，含变异验证）
- **M1 ✅ · M1.5 ✅ · M2a ✅ · M2b ✅**：记忆显式化 + 剧情摘要压缩 + LLM-Judge 语义校验——300 轮零熔断、成本曲线变平（当时 133 测试，逐步增至本代 875）
- **改进路线图四批（P0~P3）✅（2026-09-07）**：Judge 灵敏度硬门禁 / 记忆升级+模型分层+成本记账 / 数值深度 / Lorebook+脚手架+Web 前端
- **M3 通用性验证 ✅（2026-09-07）**：仙侠、赛博都市（forbidden 表语义反转）相继验证"换包即玩"，后续三个包（校园/80 年代/太空）零引擎改动通过
- **校园乙游包《青槐高中·告白之前》✅**：现代校园 × 乙游养成的通用性验证点——3 名攻略对象 + 1 名闺蜜、5 条养成轴（学力/仪态/才艺/体力/零花）、好感阶段驱动角色语气、路线 × 秘密 × 亲笔信决定 10 个结局。机制面覆盖地点表（8）/ counters（同行·赠礼）/ items（材料→点心→赠礼闭环）/ 检定三档 / 自定义工具 `write_letter`（once + 计数门槛）/ 三类事件触发；**零引擎改动**，18 项守卫（`tests/test_fourth_worldpack.py`）+ 30 条 Judge 语料（6/6/6/12+）
- **微调/训练线已归档（2026-09）**：曾规划把侧信道（judge / compress / extract / reflect / dedup）蒸馏进本地小模型，数据工厂（`scenario_factory`）与 184 MB 数据集均已建；**现决定不做微调**，相关内容整体移入 `archive/finetune/`（移动而非删除，含还原方法）。**未产出任何微调成果，本仓库不宣称训练结果。**
  ⚠️ **归档目录不入库**（2026-10 决定，见 `.gitignore`）：`archive/` 226.6 MB，虽已有规则排除其中 166.4 MB，
  仍为控制仓库体积而整目录忽略。**归档线只在本地保存**，`git clone` 拿不到；若要发布用 `git add -f archive/`。
- **平台化批次 1 ✅（2026-10）**：面向"多会话 / 多剧本 / 可消费内容"的三个阻塞项，引擎侧改动最小化。
  ① **前端拆分成真实文件**：`web.py` 里 248 行的内联 `INDEX_HTML` 字符串 → `game_agent/webui/{index.html,app.css,app.js}`
  （`StaticFiles` 托管；`index_html()` 每请求注入自由输入文案，改前端不必重启；`frontend_bundle()` 保留给结构守卫）。
  ② **记账按会话隔离**：`UsageTracker` 曾是全进程单例、多会话共写 `usage-web.jsonl`（多剧本下是数据错误）→
  每会话一个账本 `saves/usage-<sid>.jsonl` + 条目带 `session` 轴 + 新增 `GET /api/{sid}/cost`。
  ③ **存档带剧本身份戳**：`save_version` 2→3，落 `pack: {id, digest}`；读档不一致**拒绝并提示**（不静默降级）。
  摘要只吃玩法内容（world/schedule/mainline/events/endings + `npcs/*.yaml`，**刻意不含 `judge_corpus`**），
  换行归一使 CRLF/LF 同摘要（跨机可对账）。
  守卫：`tests/test_pack_identity.py`（14 项）+ `tests/test_web_accounting.py`（8 项）+ `tests/test_web_frontend.py`（+4 项）。
  设计与路线图见 `docs/plan-dsh-tavern-parity.md`、`docs/plan-tavern-shaped-product.md`。

## 许可

待定
