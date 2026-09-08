# 文字对话养成游戏 Agent

> 一个引擎适配所有背景：古代、玄幻、都市……引擎只做一次，世界包换一换就能开新游戏。

玩家通过自由对话与 NPC 互动、推进主线剧情、养成数值与好感、走向多结局。引擎（Harness）与内容（世界包）彻底分层。

## 设计文档

- [总体设计文档](docs/design.md)——架构、上下文工程、数值系统、剧情状态机、世界包规范
- [M1 实施计划](docs/plan-m1.md)——最小可玩原型的工作包与验收标准
- [M1 复盘](docs/m1-postmortem.md)——18 个问题的现象、根因、解法与预防（踩坑地图）
- [M2a 复盘](docs/m2a-postmortem.md)——五次迭代的测量教训与验收口径修订
- [M2b 复盘](docs/m2b-postmortem.md)——压缩与语义校验的 300 轮长局验收（含潜伏 bug 与漂移观察）
- [M1.5 计划](docs/plan-m1-5.md)——体验基线与债务清理（已全部达成）
- [M2 计划](docs/plan-m2.md)——长线可玩三段拆分（M2a/b/c）
- [改进路线图](docs/improvement-roadmap.md)——外部对标评审结论与分批改进清单（P0~P3）
- [P0 执行报告](docs/p0-report.md)——批次 1：E1 Judge 灵敏度验证 + F1 硬编码修复（已完成）
- [P2 执行报告](docs/p2-report.md)——批次 3：D 系列玩法数值深度（行动检定/消费闭环/收益曲线，已完成）
- [P1 执行报告](docs/p1-report.md)——批次 2：A 系列记忆升级 + C 系列模型分层与成本记账（已完成）
- [P3 执行报告](docs/p3-report.md)——批次 4：B1 Lorebook + E3 世界包脚手架 + F5 Web 前端（已完成，路线图收官）
- [审查修复计划](docs/review-fix-plan.md)——四批次代码审查发现（1 Critical / 7 Major / 6 Minor）与分批修复方案
- [第二个世界包验证报告](docs/world2-report.md)——M3「换包即玩」：《问道长生》仙侠包 + 通用性三级验证（已完成）
- [第三个世界包验证报告](docs/world3-report.md)——forbidden 表反转与文风注入的近现代世界观验证：《霓虹深处》赛博都市包（已完成）
- [世界包作者手册](docs/worldpack-manual.md)——写给内容作者的完整手册：schema/守则/陷阱/语料规范/验收单（写新世界包从这里开始）

## 目录结构

```
game_agent/        # 引擎包（与内容无关的 Harness 层）
world-packs/       # 世界包（纯 YAML 内容，换一包换一个世界）
  ancient_jianghu/ # 第一个包：武侠《江湖旧梦》
  xianxia_wendao/  # 第二个包：仙侠《问道长生》（M3 通用性验证）
  urban_neon/      # 第三个包：赛博都市《霓虹深处》（forbidden 表反转验证）
tests/             # 单元测试与验收脚本
```

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
# 深度检查（可选，约 ¥1-2）：100 回合长局——压缩/记忆/检索/审计不腐化
uv run python scripts/longrun_probe.py --pack world-packs/xianxia_wendao --turns 100
```

## 当前状态

- **M1 ✅ · M1.5 ✅ · M2a ✅ · M2b ✅**：记忆显式化 + 剧情摘要压缩 + LLM-Judge 语义校验全部达成——300 轮零熔断、成本曲线变平、轨道事实 3/3、133 测试全绿
- **P0 ✅（2026-09-07）**：E1 Judge 灵敏度验证（对抗语料 30 条，三类拦截率 100%、误报 0%）+ F1 引擎层硬编码修复（`docs/p0-report.md`）
- **P2 ✅（2026-09-07）**：D 系列玩法数值深度——行动检定（三档）、消费闭环（备礼探访）、收益曲线（范围随机+边际递减）；159 测试全绿（`docs/p2-report.md`）
- **P1 ✅（2026-09-07）**：A 系列记忆升级（检索式注入/重要性/反思层/语义去重）+ C 系列模型分层与 usage 成本记账；189 测试全绿（`docs/p1-report.md`）
- **P3 ✅（2026-09-07）**：B1 Lorebook 按需注入 + E3 世界包脚手架 + F5 Web 前端（`python -m game_agent web`）；205 测试全绿（`docs/p3-report.md`）——改进路线图四批全部完成
- **M3（第二个世界包）✅（2026-09-07）**：仙侠包《问道长生》（world-packs/xianxia_wendao）落地——全新属性 schema（道心/修为/灵石）、双 NPC、3 节点、时间触发事件、4 结局；引擎零逻辑耦合，仅修复 Web 壳标题硬编码与 `--pack` 通道（G1/G2）；离线 224 测试全绿 + 真机冒烟通关「问道长生」（审计零偏差、禁用元素零泄漏、成本约 ¥0.13）——"换一个世界包 = 换一个游戏"得到验证（`docs/world2-report.md`）
- **第三个世界包 ✅（2026-09-07）**：赛博都市包《霓虹深处》（world-packs/urban_neon）——forbidden 表**语义反转**（手机/AI 从禁用变合法世界观元素，改禁奇幻元素/烂梗/现实品牌/第四面墙）+ 英文属性 key + 每日 2 行动点；真机验证禁表零泄漏、第四面墙零突破、文风古风残留零（冷硬黑色电影风成立）；期间发现并修复引擎通用缺口——长叙事回合 `finish_reason=length` 截断导致协议熔断（重试提示点明"缩短叙事"）；233 测试全绿 + 真机通关「自由落体」（`docs/world3-report.md`）
- **M2c（可选）待定**：Web 前端 ✅ 已随 P3 落地 / 多人场景 / 时间触发事件内容（见 docs/plan-m2.md）

## 许可

待定
