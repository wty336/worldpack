# 文字对话养成游戏 Agent

> 一个引擎适配所有背景：古代、玄幻、都市……引擎只做一次，世界包换一换就能开新游戏。

玩家通过自由对话与 NPC 互动、推进主线剧情、养成数值与好感、走向多结局。引擎（Harness）与内容（世界包）彻底分层。

## 设计文档

- [总体设计文档](docs/design.md)——架构、上下文工程、数值系统、剧情状态机、世界包规范
- [M1 实施计划](docs/plan-m1.md)——最小可玩原型的工作包与验收标准
- [M1 复盘](docs/m1-postmortem.md)——18 个问题的现象、根因、解法与预防（踩坑地图）
- [M1.5 计划](docs/plan-m1-5.md)——体验基线与债务清理（已全部达成）
- [M2 计划](docs/plan-m2.md)——长线可玩三段拆分（M2a/b/c）

## 目录结构

```
game_agent/        # 引擎包（与内容无关的 Harness 层）
world-packs/       # 世界包（纯 YAML 内容，换一包换一个世界）
tests/             # 单元测试与验收脚本
```

## 快速开始

```bash
# 1. 配置 API Key
cp .env.example .env        # 填入 DEEPSEEK_API_KEY

# 2. 安装依赖（uv）
uv sync

# 3. 校验世界包（离线，无需 API）
uv run python -m game_agent check-worldpack

# 4. 跑测试
uv run pytest
```

## 当前状态

- **M1 ✅ / M1.5 ✅**：全部验收通过（两结局通关、数值审计、注入防御、OOC 0 违规、121 测试）
- **M2a 进行中**：记忆显式化离线地基已就位（MemorySystem + remember 工具 + 状态栏关键事实常驻 + NPC 记忆注入 + 记忆回归脚本），待 120 回合真机复测（基线保持率 50% → 目标 ≥80%）

## 许可

待定
