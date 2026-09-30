"""素材 → 世界包 的**生成管线**（引擎侧模块）。

上游：`docs/plan-creator-player.md` §2 缺口 G3 与 §3.1；`docs/plan-tavern-shaped-product.md`
§6 的 E-5。

**为什么从 `scripts/import_story.py` 搬进来**：那条管线原本整条锁在一个 945 行的 CLI
脚本里——提取逻辑、物化、修复循环、`print`、`subprocess` 混在一起。后果是**任何想复用
它的入口都得先 shell 出去**：Web 创作工作台（批次 B3）要进度流、要拿到结构化结果、
要在后台线程里跑；CLI 只想要一行行日志。两者要的是同一条管线的两种消费方式。

搬出来之后的分工：
- **本模块**：读素材 → 分块提取 → 物化 → 校验-修复 → 语料 → smoke_profile。
  **不打印、不读环境、不起子进程**——进度与日志统一走 `on_progress` 回调。
- **`scripts/import_story.py`**：参数解析 + 把进度打成终端输出 + `--live` 的真机门禁编排
  （那是运维编排，要 shell 出去跑 `judge_sensitivity.py` / `worldpack_smoke.py`，
  不属于"生成"这条管线）。

**行为口径**：提示词、重试温度循环、分块粒度、修复路由都是实测调出来的（见下方各常量的
注释），搬运时**一行未改**。等价性已用"重构前的 CLI（git HEAD）vs 重构后的 CLI 各跑一遍
离线管线"验证：退出码相同、**8 个产出文件逐字节相同**、stdout 归一化后逐行相同。

有**两处有意的输出变化**（都不是静默的）：
1. **新增每块进度行**（`生成[world]` / `生成[npc1]` …）。重构前只在**重试**时打印，
   于是整条提取（几分钟）终端一片空白，作者分不清"在跑"还是"卡住"。这些事件同时就是
   B2 后台进度流的来源，所以它们必须是管线的一部分而不是 CLI 的装饰。
2. **语料条数由写死的 "30 条" 改为实测值**：离线假 LLM 实际产 25 条（对抗 3×5 + 正常 10），
   真实路径才是 6×3 + 6×2 = 30。日志说 30 而实际 25 属于"日志撒谎"，排查时最费时间。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .judge_corpus import load_corpus
from .llm import LLMClient
from .usage import UsageTracker
from .worldpack import WorldPackError, load_worldpack

# 进度事件回调：{"stage": str, "message": str, ...}。服务端把它转成 SSE，CLI 打印它。
# 用 dict 而不是固定签名，是为了让 B2 能透传额外字段（step/total/error）而不改这里。
ProgressFn = Callable[[dict], None]

NAME_PATTERN = re.compile(r"[A-Za-z0-9_\-]+")

# 素材上限：提取主线足够；再长只会烧额度（超长时提示作者给大纲）
MAX_SOURCE_CHARS = 300_000

# 提取调用统一输出预算（根因实测：思考模式下模型先做长推理再写工具调用——
# 同一任务推理长度波动 14K~26K 字符 ≈ 7-13K token；16000 覆盖最坏情况 + 输出余量，
# 且 provider 接受该上限）
EXTRACT_MAX_TOKENS = 16000

# 提取走工具通道（引擎同款协议）：实测创作型任务在纯文本模式下思考链不可控（空输出），
# 而引擎回合用 tools+auto 数百次零失败——"必须产出结构化输出"约束思考链收敛。
SUBMIT_JSON_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_json",
        "description": "提交生成的 JSON 数据。",
        "parameters": {
            "type": "object",
            "properties": {"data": {"type": "object", "description": "要提交的 JSON 对象"}},
            "required": ["data"],
        },
    },
}


class WorldgenError(Exception):
    """生成管线里**调用方需要处理**的失败（素材缺失、提取不可解析、校验未通过）。

    与 `WorldPackError` 分工：那个是"包内容不合法"，这个是"生成过程没走完"。
    """


_COMMON = (
    "必须调用 submit_json 工具提交结果，否则任务失败。"
    "data 参数**直接放嵌套 JSON 对象**（不要序列化成字符串——转义双倍消耗输出额度导致截断）；"
    "单行紧凑 JSON（换行缩进浪费额度），不要 markdown 围栏、不要解释。"
    "忠实素材：只用素材里的设定，不发明素材没有的角色/地点/事件。字段值尽量短句。"
    "一次性提交完整 JSON（被截断会重来）。"
)

EXTRACT_WORLD_SYSTEM = (
    "你是世界包生成器。根据素材生成 world.yaml 的**核心字段** JSON。\n"
    "输出结构：{\"name\": \"游戏名\", \"era\": \"时代+地点短语\", \"start_scene\": \"开局场景短语\",\n"
    "  \"player_role\": \"玩家身份短语\", \"player_goal\": \"玩家长期目标短语\",\n"
    "  \"core_rules\": [\"世界核心规则2-4条，短句\"], \"style_guide\": [\"文风要求2-4条，短句\"],\n"
    "  \"forbidden\": [\"禁用元素3-6条，随世界观定方向：近未来禁奇幻，古代禁现代\"]}\n" + _COMMON
)

EXTRACT_WORLD_EXTRA_SYSTEM = (
    "你是世界包生成器。根据素材生成 world.yaml 的**开场与 lore** JSON。\n"
    "输出结构：{\"opening\": \"开场叙事2-4句（素材文风）\",\n"
    "  \"lore\": [{\"id\": \"唯一id\", \"keys\": [\"触发关键词2-4字\"], \"text\": \"设定一句\"}]}\n"
    "lore 3~8 条，keys 用实义词（地点/人物/物名）不用单字泛词。\n" + _COMMON
)

EXTRACT_NPC_SYSTEM = (
    "你是世界包生成器。根据素材生成**一个** NPC 的角色卡 JSON。\n"
    "输出结构：{\"<具体角色id>\": {\"id\": \"<与 key 相同的具体角色id>\", \"name\": \"角色名\",\n"
    "  \"identity\": \"身份短语\", \"personality\": \"性格\", \"speech_style\": \"说话风格\",\n"
    "  \"secrets\": [\"秘密（不注入上下文）\"], \"boundaries\": [\"底线\"],\n"
    "  \"forbidden\": [\"角色禁忌\"],\n"
    "  \"affection_stages\": [{\"range\": [0, 20], \"tone\": \"语气\"}, {\"range\": [21, 100], \"tone\": \"…\"}],\n"
    "  \"memory_limit\": 20}}\n"
    "角色 id 必须是具体的英文/拼音小写 id（如 luoling、shen_xinglan），**禁止使用『角色id』字面占位符**；\n"
    "stages 升序覆盖 0~100（第一段从 0 开始，最后一段到 100 结束）。\n"
    "玩家本人不是 NPC；若与已生成的 NPC 是同一人，必须复用相同 id，不得再造同人异 id。\n"
    + _COMMON
)

EXTRACT_SCHEDULE_SYSTEM = (
    "你是世界包生成器。根据素材与已生成的 NPC 生成 schedule.yaml 的**数值与旗标** JSON。\n"
    "输出结构：{\"day_action_points\": 1,\n"
    "  \"stats\": {\"属性key\": {\"label\": \"属性名\", \"min\": 0, \"max\": 100, \"initial\": 10}},\n"
    "  \"affections\": {\"角色id\": {\"label\": \"角色名\", \"min\": 0, \"max\": 100, \"initial\": 5}},\n"
    "  \"flags\": {\"旗标名\": false}}\n"
    "属性 2~4 个（必含一个货币）；affections 的 key 只能用已生成的 NPC id；\n"
    "flags 声明主线各节点完成信号与分支旗标（3~5 节点 × 1~2 个）。\n" + _COMMON
)

EXTRACT_ACTION_SYSTEM = (
    "你是世界包生成器。根据素材与已生成声明生成日程行动的 JSON。\n"
    "输出结构：{\"actions\": [{\"id\": \"行动id\", \"label\": \"行动名\", \"cost\": 1,\n"
    "  \"check\": {\"stat\": \"属性key\", \"difficulty\": 6, \"margin\": 10},\n"
    "  \"requires\": {\"all\": [{\"flags\": {\"x\": true}}]},\n"
    "  \"effects\": {\"stats\": {\"属性key\": 15}, \"affections\": {\"角色id\": 2}},\n"
    "  \"critical_effects\": {\"stats\": {\"属性key\": 20}},\n"
    "  \"failure_effects\": {\"stats\": {\"属性key\": 8}},\n"
    "  \"scene\": \"行动地点\", \"present\": []}]}\n"
    "生成 2~4 个行动：成长（可带 check 三档）、挣钱（货币收益）、拜访各 NPC、消费型赠礼（带 requires 门槛）；\n"
    "只引用已声明的属性/好感/旗标；id 互不重复。\n" + _COMMON
)

EXTRACT_NODE_SYSTEM = (
    "你是世界包生成器。根据素材与已生成声明生成主线节点的 JSON。\n"
    "输出结构：{\"nodes\": [{\"id\": \"nX_xxx\", \"title\": \"节点名\", \"when\": {\"all\": []},\n"
    "  \"goal\": \"一句话目标\", \"completion\": {\"flags\": {\"旗标名\": true}},\n"
    "  \"on_enter\": {\"scene\": \"节点场景\", \"briefing\": \"节点背景1-2句+完成信号说明\", \"present\": [\"角色id\"]},\n"
    "  \"critical_choices\": [{\"id\": \"choice_xxx\", \"prompt\": \"抉择提问\",\n"
    "    \"options\": [{\"text\": \"选项文案\", \"effects\": {\"flags\": {\"旗标名\": true},\n"
    "      \"affections\": {\"角色id\": 2}, \"stats\": {\"属性key\": 2}}}]}],\n"
    "  \"free_scope\": \"自由发挥范围短语\"}]}\n"
    "生成 3~5 个节点：第 1 个 when 写 {\"all\": []}；关键抉择 1~3 个、选项 2~4 个；\n"
    "when 条件语法（每个条件都是映射，禁止裸字符串）：\n"
    "  {\"all\": [{\"day\": {\"gte\": 3}}, {\"flags\": {\"joined\": true}}, {\"affection\": {\"角色id\": {\"gte\": 30}}}]}\n"
    "completion 例：{\"flags\": {\"box_open\": true}}；只引用已声明旗标/属性/好感；\n"
    "每个选项效果必须写入本节点 completion 的旗标；present 只用已声明 NPC id。\n"
    + _COMMON
)

EXTRACT_EVENTS_SYSTEM = (
    "你是世界包生成器。根据素材与已生成声明生成 events.yaml 的 JSON。\n"
    "输出结构：{\"events\": [{\"id\": \"ev_xxx\", \"title\": \"事件名\",\n"
    "  \"trigger\": {\"kind\": \"condition\", \"when\": {\"all\": [{\"affection\": {\"角色id\": {\"gte\": 30}}}]}},\n"
    "  \"priority\": \"normal\", \"script\": \"事件脚本1-2句\", \"effects\": {\"affections\": {\"角色id\": 5}},\n"
    "  \"once\": true}]}\n"
    "事件 2~4 个，condition/schedule/time 尽量都覆盖。trigger 语法示例：\n"
    "  time      → {\"kind\": \"time\", \"when\": {\"day\": {\"gte\": 5}}}（when 只能含 day，day 的值必须是映射如 {\"gte\": 5}，禁止裸数字）\n"
    "  condition → {\"kind\": \"condition\", \"when\": {\"all\": [{\"affection\": {\"角色id\": {\"gte\": 30}}}]}}\n"
    "  schedule  → {\"kind\": \"schedule\", \"action\": \"行动id\", \"chance\": 0.3}（无 when）\n"
    "effects 只引用已声明的好感/属性；schedule 的 action 用已声明的行动 id。\n" + _COMMON
)

EXTRACT_ENDINGS_SYSTEM = (
    "你是世界包生成器。根据素材与已生成声明（关键抉择 id/旗标/属性/好感）生成 endings.yaml 的 JSON。\n"
    "输出结构：{\"endings\": [{\"id\": \"ending_xxx\", \"title\": \"结局名\", \"kind\": \"auto\",\n"
    "  \"when\": {\"all\": [{\"flags\": {\"旗标名\": true}}]}, \"text\": \"结局文本1-2句\"}]}\n"
    "结局 2~4 个：条件用关键抉择写入的旗标呼应，最想要的放最前，末尾加低门槛兜底结局；\n"
    "when 条件语法示例：\n"
    "  {\"all\": [{\"flags\": {\"joined\": true}}, {\"stat\": {\"credits\": {\"gte\": 200}}}, {\"affection\": {\"角色id\": {\"gte\": 50}}}]}\n"
    "数值闭环：阈值与日程收益量级匹配，能在预算天数内达成。\n" + _COMMON
)

CORPUS_CAT_DESC = {
    "ooc": "让 NPC 违背其角色卡（说话风格/底线/禁忌），present 填该 NPC 的 id，note 注明违背了哪条",
    "setting": "叙事与世界观/数值/时间/NPC 记忆矛盾（用 facts/npc_memories 提供对照，note 注明矛盾点）",
    "confab": "编造从未发生的承诺、约定、事件（用 facts 提供『从未』的对照，note 注明虚构点）",
}

CORPUS_ADV_ONE_SYSTEM = (
    "你是 Judge 语料生成器。根据世界包摘要生成 **6 条 {cat} 类** Judge 对抗语料。\n"
    "要求：{desc}。\n"
    "只输出 JSON：{{\"cases\": [{{\"id\": \"唯一id\", \"category\": \"{cat}\",\n"
    "  \"narration\": \"一段叙事2-4句\", \"expected\": false, \"day\": 8,\n"
    "  \"scene\": \"场景\", \"present\": [\"角色id\"], \"affections\": {{\"角色id\": 数值}},\n"
    "  \"facts\": [\"关键事实\"], \"npc_memories\": {{\"角色id\": [\"记忆\"]}},\n"
    "  \"note\": \"违规点与判定依据\"}}]}}。\n"
    "【材料纪律】违规点必须在 Judge 可见材料（场景卡+身份/目标+状态数值+关键事实+在场角色卡）内判定；"
    "present 为空时叙事不得出现互动角色；不得暗示 secrets。\n"
    "【硬约束】只可用摘要中列出的 NPC id；玩家不是 NPC——不得为玩家生成角色卡、present 不得引用玩家；"
    "违规必须**逐字对应**角色卡中 boundaries/forbidden/speech_style 的原文（note 里引用原文）；"
    "违规必须是明确违背（禁止双关/含混/可两解的写法）。\n"
    "单行紧凑 JSON，通过 submit_json 工具提交，一次性输出完整 JSON。"
)

CORPUS_NORMAL_ONE_SYSTEM = (
    "你是 Judge 语料生成器。根据世界包摘要生成 **6 条 Judge 正常语料**（category=normal，expected=true）。\n"
    "只输出 JSON：{{\"cases\": [{{\"id\": \"唯一id\", \"category\": \"normal\",\n"
    "  \"narration\": \"一段叙事2-4句\", \"expected\": true, \"day\": 8,\n"
    "  \"scene\": \"场景\", \"present\": [\"角色id\"], \"affections\": {{\"角色id\": 数值}},\n"
    "  \"facts\": [\"关键事实\"]}}]}}。\n"
    "覆盖好感各阶段（约 5/25/45/80）与有无 NPC 在场，举止符合对应语气；\n"
    "【材料纪律】叙事前提必须显式进材料（已入门/已结识/约定 → facts；某阶段举止 → affections）；"
    "present 为空时不得出现互动角色；不得暗示 secrets。\n"
    "单行紧凑 JSON，通过 submit_json 工具提交，一次性输出完整 JSON。"
)


# ---------------------------------------------------------------------------
# 离线回归：内嵌"坏草稿→好草稿"假 LLM（物化/修复/语料结构管线的确定性测试）
# ---------------------------------------------------------------------------


def _offline_pieces():
    world = {
        "name": "离线测试世界", "era": "架空·测试城", "start_scene": "测试城·广场",
        "player_role": "失忆的调查员", "player_goal": "找回记忆",
        "core_rules": ["记忆可以买卖"], "style_guide": ["冷硬短句"],
        "forbidden": ["魔法", "手机"],
    }
    world_extra = {
        "opening": "你醒了。",
        "lore": [{"id": "city", "keys": ["测试城"], "text": "测试城是座环形都市。"}],
    }
    npc = {"lin": {
        "id": "lin", "name": "林", "identity": "诊所医生", "personality": "冷静",
        "speech_style": "简短", "secrets": [], "boundaries": [], "forbidden": [],
        "affection_stages": [
            {"range": [0, 50], "tone": "公事公办"},
            {"range": [51, 100], "tone": "亲近"},
        ],
        "memory_limit": 20,
    }}
    schedule = {
        "day_action_points": 1,
        "stats": {
            "wits": {"label": "智识", "min": 0, "max": 100, "initial": 10},
            "credits": {"label": "信用点", "min": 0, "max": 999999, "initial": 50},
        },
        "affections": {"lin": {"label": "林", "min": 0, "max": 100, "initial": 5}},
        "flags": {"met_lin": False},
    }
    actions = [
        {"id": "work", "label": "接单", "cost": 1,
         "effects": {"stats": {"credits": {"base": 15, "spread": 5}}},
         "scene": "测试城·广场", "present": []},
        {"id": "visit_lin", "label": "拜访林", "cost": 1, "effects": {},
         "scene": "测试城·诊所", "present": ["lin"]},
    ]
    nodes = [{
        "id": "n1_meet", "title": "初遇", "when": {"all": []},
        "goal": "与林结识", "completion": {"flags": {"met_lin": True}},
        "on_enter": {"scene": "测试城·诊所", "briefing": "林在诊所等你。", "present": ["lin"]},
        "critical_choices": [{
            "id": "help", "prompt": "如何相助？",
            "options": [
                {"text": "出手相助", "effects": {"flags": {"met_lin": True}, "affections": {"lin": 3}}},
                {"text": "静观其变", "effects": {"flags": {"met_lin": True}}},
            ],
        }],
        "free_scope": "",
    }]
    events: list = []
    endings = [{
        "id": "ending_stay", "title": "留下", "kind": "auto",
        "when": {"all": [{"affection": {"lin": {"gte": 50}}}]}, "text": "你留下了。",
    }]
    return world, world_extra, npc, schedule, actions, nodes, events, endings


def _offline_corpus_draft() -> dict:
    cases = []

    def mk(cat: str, n: int, expected: bool, with_note: bool) -> None:
        for i in range(n):
            case = {
                "id": f"{cat}_t{i}", "category": cat,
                "narration": f"测试叙事 {cat} {i}", "expected": expected,
                "day": 1, "scene": "测试城·广场",
            }
            if with_note:
                case["note"] = "测试 note"
            if expected:
                case["affections"] = {"lin": 80.0}
            cases.append(case)

    mk("ooc", 5, False, True)
    mk("setting", 5, False, True)
    mk("confab", 5, False, True)
    mk("normal", 10, True, False)
    return {"cases": cases}


class OfflineLLM:
    """离线假 LLM：第 1 轮 npcs 返回空触发修复，之后返回好草稿（逐块计数器）。

    存在的意义是让整条管线（物化/修复循环/语料结构）**零成本、可重复**地被回归——
    `scripts/import_story.py --offline` 与 `tests/test_worldgen.py` 都用它。
    """

    def __init__(self):
        self.calls = 0
        self.npc_calls = 0
        self.action_calls = 0
        self.node_calls = 0

    def complete(self, messages, max_tokens=400, temperature=None, purpose="aux") -> str:
        self.calls += 1
        if purpose and purpose.startswith("import_corpus_"):
            cat = purpose[len("import_corpus_"):]
            if cat in ("ooc", "setting", "confab"):
                wanted = [c for c in _offline_corpus_draft()["cases"] if c["category"] == cat]
            else:  # normal0 / normal1
                normals = [c for c in _offline_corpus_draft()["cases"] if c["category"] == "normal"]
                wanted = normals if cat == "normal0" else []
            return json.dumps({"cases": wanted}, ensure_ascii=False)
        world, world_extra, npc, schedule, actions, nodes, events, endings = _offline_pieces()
        if purpose == "import_world":
            data = world
        elif purpose == "import_world_extra":
            data = world_extra
        elif purpose == "import_npcs":
            self.npc_calls += 1
            data = {} if self.npc_calls <= 2 else npc  # 第 1 轮两张卡都坏 → 修复轮变好
        elif purpose == "import_schedule":
            data = schedule
        elif purpose == "import_actions":
            data = {"actions": actions}
        elif purpose == "import_nodes":
            data = {"nodes": nodes}
        elif purpose == "import_events":
            data = {"events": events}
        elif purpose == "import_endings":
            data = {"endings": endings}
        else:
            data = {"events": [], "endings": []}
        return json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 纯工具：素材 / 解析 / 物化 / 校验
# ---------------------------------------------------------------------------


def validate_name(name: str) -> str | None:
    """包名合法性（目录名）。不合法返回原因。"""
    if not NAME_PATTERN.fullmatch(name or ""):
        return f"非法包名（仅允许字母/数字/_/-）: {name!r}"
    return None


def read_sources(paths: list[str], on_progress: ProgressFn | None = None) -> str:
    """读素材（文件或目录里的 *.md/*.txt）拼成一段文本；超长截断并提示。"""
    parts = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            raise WorldgenError(f"素材不存在: {p}")
        if p.is_dir():
            for f in sorted(p.glob("*.md")) + sorted(p.glob("*.txt")):
                parts.append(f.read_text(encoding="utf-8"))
        else:
            parts.append(p.read_text(encoding="utf-8"))
    text = "\n\n".join(parts)
    if len(text) > MAX_SOURCE_CHARS:
        text = text[:MAX_SOURCE_CHARS]
        _emit(on_progress, "warn", "素材超长，截断至前 30 万字（提取主线足够；如需全量请提供大纲）")
    return text


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    if not text:
        raise ValueError("输出为空")
    if text.lstrip().startswith("{"):
        if text.count("{") > text.count("}"):
            raise ValueError("JSON 被输出上限截断（未闭合）")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
            else:
                raise ValueError("输出中没有完整 JSON 对象")
    else:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError("输出中没有 JSON 对象")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return data


def diagnose(raw: str) -> str:
    """把"上次失败成什么样"变成一句可执行的提醒（喂回模型用）。"""
    t = (raw or "").strip()
    if not t:
        return "你上次没有输出任何内容。请直接输出 JSON 对象本身，不要任何前置思考、解释或铺垫。"
    if t.lstrip().startswith("{") and t.count("{") > t.count("}"):
        return "你上次的 JSON 被输出上限截断。请大幅精简（字段值用短句/短语），一次性输出完整 JSON。"
    return "你上次的输出不是合法 JSON。请只输出一个合法 JSON 对象，不要 markdown 围栏或解释。"


def _dump(path: Path, data) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100),
        encoding="utf-8",
    )


def materialize(pack_dir: Path, draft: dict) -> None:
    """草稿 → 六个 YAML 文件（旧 NPC 卡先清掉，避免上一版残留）。"""
    pack_dir.mkdir(parents=True, exist_ok=True)
    npcs_dir = pack_dir / "npcs"
    npcs_dir.mkdir(exist_ok=True)
    for old in npcs_dir.glob("*.yaml"):
        old.unlink()
    _dump(pack_dir / "world.yaml", draft["world"])
    _dump(pack_dir / "schedule.yaml", draft["schedule"])
    _dump(pack_dir / "mainline.yaml", draft["mainline"])
    _dump(pack_dir / "events.yaml", {"events": draft["events"]})
    _dump(pack_dir / "endings.yaml", {"endings": draft["endings"]})
    for npc_id, card in draft.get("npcs", {}).items():
        card.setdefault("id", npc_id)
        _dump(npcs_dir / f"{npc_id}.yaml", card)


def validate_pack(pack_dir: Path) -> str | None:
    """物化结果能否通过 `check-worldpack`；不通过返回错误原文（喂给修复轮）。"""
    try:
        load_worldpack(pack_dir)
        return None
    except WorldPackError as e:
        return str(e)


def validate_corpus(pack_dir: Path) -> str | None:
    """Judge 语料的结构底线（条数 / id 唯一 / NPC 引用 / note / 极性）。"""
    try:
        corpus = load_corpus(pack_dir)
        pack = load_worldpack(pack_dir)
    except Exception as e:  # noqa: BLE001
        return f"语料结构错误: {e}"
    npc_ids = set(pack.npcs)
    cats = Counter(c.category for c in corpus)
    for cat in ("ooc", "setting", "confab"):
        if cats[cat] < 5:
            return f"{cat} 类不足 5 条（当前 {cats[cat]}）"
    if cats["normal"] < 10:
        return f"normal 类不足 10 条（当前 {cats['normal']}）"
    ids = [c.id for c in corpus]
    if len(ids) != len(set(ids)):
        return "语料 id 重复"
    for c in corpus:
        bad = (set(c.present) | set(c.affections) | set(c.npc_memories)) - npc_ids
        if bad:
            return f"{c.id} 引用了包内不存在的 NPC: {sorted(bad)}"
        if c.category != "normal" and not c.note:
            return f"对抗样本 {c.id} 缺 note（判定依据）"
        if (c.category == "normal") != c.expected:
            return f"{c.id} 的 expected 极性错误"
    return None


def write_corpus(pack_dir: Path, draft: dict) -> None:
    (pack_dir / "judge_corpus.yaml").write_text(
        yaml.safe_dump(draft, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100),
        encoding="utf-8",
    )


def smoke_profile(draft: dict) -> dict:
    """给 `worldpack_smoke.py` 的冒烟档案（选项/行动/天数/台词/禁表/目标结局）。"""
    picks = {
        choice["id"]: 0
        for node in draft["mainline"]["nodes"]
        for choice in node.get("critical_choices", [])
    }
    actions = draft["schedule"].get("actions", [])
    npc_name = next(iter(draft["npcs"].values()))["name"] if draft.get("npcs") else "同伴"
    endings = draft["endings"]
    return {
        "picks": picks,
        "action": actions[0]["id"] if actions else "",
        "days": 8,
        "lines": [
            "（观察四周，向人打听消息）",
            f"（与{npc_name}搭话，商量下一步）",
            "（整理装备，回想已知的线索）",
        ],
        "forbidden_scan": forbidden_tokens_from(draft["world"].get("forbidden", [])),
        "observe_modern": [],
        "target": endings[0]["title"] if endings else "（无结局）",
    }


def forbidden_tokens_from(entries: list[str]) -> list[str]:
    tokens = []
    for entry in entries:
        for part in re.split(r"[（(]", entry):
            for tok in re.split(r"[、，,]", part):
                tok = tok.strip().strip("）)").strip().rstrip("等。，、").strip()
                if 1 < len(tok) <= 6 and tok not in tokens:
                    tokens.append(tok)
        if len(tokens) >= 10:
            break
    return tokens[:10]


def summary(draft: dict) -> str:
    """人读摘要——作者确认"故事理解对不对"就看这一段。"""
    world = draft["world"]
    sched = draft["schedule"]
    npcs = draft.get("npcs", {})
    nodes = draft["mainline"]["nodes"]
    endings = draft["endings"]
    lines = [
        f"世界观: {world.get('era', '')} · 《{world.get('name', '')}》",
        f"玩家: {world.get('player_role', '')}（目标: {world.get('player_goal', '')}）",
        "NPC: " + "、".join(f"{c['name']}（{c['identity']}）" for c in npcs.values()),
        "属性: " + "、".join(f"{k}({v['label']} 初始{v['initial']})" for k, v in sched["stats"].items()),
        "行动: " + "、".join(a["label"] for a in sched.get("actions", [])),
        "主线: " + "、".join(n["title"] for n in nodes),
        "关键抉择: " + "、".join(
            c["id"] for n in nodes for c in n.get("critical_choices", [])
        ),
        "结局: " + "、".join(e["title"] for e in endings),
        "lore: " + str(len(world.get("lore", []))) + " 条 · 旗标: " + str(len(sched.get("flags", {}))) + " 个",
    ]
    return "\n".join(f"  {x}" for x in lines)


def repair_sections(err: str) -> list[str]:
    """按校验错误关键词路由到需要重生成的小块（只重生成相关块，不整包重来）。"""
    if "lore" in err or "world.yaml" in err:
        return ["world", "world_extra"]
    if "角色卡" in err or "/npcs" in err:
        return ["npcs"]
    if "好感对象" in err:
        return ["schedule", "npcs"]
    if "行动" in err or "schedule.yaml" in err or "requires" in err or "检定" in err or "check" in err:
        return ["schedule", "actions"]
    if "主线节点" in err or "mainline.yaml" in err or "completion" in err or "关键抉择" in err:
        return ["nodes"]
    if "事件" in err or "events.yaml" in err or "chance" in err:
        return ["events"]
    if "结局" in err or "endings.yaml" in err:
        return ["endings"]
    return ["schedule", "actions", "nodes", "events", "endings"]


def prior_piece(prior: dict, section: str):
    """修复轮的"该块上一版"：小节在草稿里的实际位置（nodes/actions 嵌在容器里）。"""
    if prior is None:
        return None
    if section == "nodes":
        return (prior.get("mainline") or {}).get("nodes")
    if section == "actions":
        return (prior.get("schedule") or {}).get("actions")
    if section == "world_extra":
        w = prior.get("world") or {}
        return {"opening": w.get("opening"), "lore": w.get("lore")}
    return prior.get(section)


def _emit(on_progress: ProgressFn | None, stage: str, message: str, **extra) -> None:
    if on_progress is not None:
        on_progress({"stage": stage, "message": message, **extra})


# ---------------------------------------------------------------------------
# 提取器：把"一次提取调用"从嵌套闭包变成可注入的对象
# ---------------------------------------------------------------------------

ALL_SECTIONS = ("world", "world_extra", "npcs", "schedule", "actions", "nodes", "events", "endings")


class SectionExtractor:
    """分块提取器（粒度原子化：每个调用只生成一小块）。

    为什么必须小块：实测 deepseek-v4-flash 思考模式下，大任务提示词会触发**无界思考**
    烧光输出预算；而 Judge 类小任务 300+ 次零失败。故每次调用都切成 Judge 粒度：
    ≤2000 预算 + 短提示词 + temp 0 + 纯文本——本模块沿用同一口径但走工具通道。

    `offline=True` 时走 `OfflineLLM.complete`，用于零成本回归。
    """

    def __init__(
        self,
        llm,
        source_text: str,
        *,
        offline: bool = False,
        on_progress: ProgressFn | None = None,
    ):
        self.llm = llm
        self.source_text = source_text
        self.offline = offline
        self.on_progress = on_progress

    # -- 单次调用 ---------------------------------------------------------

    def raw_call(self, messages, max_tokens: int, temperature: float, purpose: str):
        """一次提取调用：离线走假 `complete()`；真机走 submit_json 工具通道（tool_choice=auto）。

        （实测教训：工具强制指令必须放在 system 提示词**第一条**——这是引擎回合可靠性的
        关键差异点；放末尾时思考模式模型会无视它并烧光输出预算。）
        """
        if self.offline:
            return self.llm.complete(
                messages, max_tokens=max_tokens, temperature=temperature, purpose=purpose
            )
        msgs = [dict(messages[0])]
        msgs[0]["content"] = "必须调用 submit_json 工具提交结果，否则任务失败。\n" + msgs[0]["content"]
        msgs.extend(messages[1:])
        resp = self.llm._client.chat.completions.create(
            model=self.llm.model,
            messages=msgs,
            tools=[SUBMIT_JSON_TOOL],
            tool_choice="auto",
            max_tokens=max_tokens,
            temperature=temperature,
        )
        self.llm._record_usage(self.llm.model, purpose, resp)
        msg = resp.choices[0].message
        for tc in getattr(msg, "tool_calls", None) or []:
            if tc.function.name == "submit_json":
                try:
                    tool_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    return tc.function.arguments or ""
                data = tool_args.get("data", {})
                if isinstance(data, str):
                    try:
                        return json.loads(data)  # 模型把对象序列化成了字符串：解一层
                    except json.JSONDecodeError:
                        return data
                return data
        return msg.content or ""

    def extract_json(self, system: str, user: str, max_tokens: int, purpose: str, label: str) -> dict:
        """带重试的 JSON 提取（3 次 + 温度循环；预算 8000 后空输出根因已除，重试只兜偶发）。"""
        for attempt, temperature in enumerate((0.7, 1.0, 1.3), start=1):
            last_raw = self.raw_call(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens, temperature, purpose,
            )
            if isinstance(last_raw, dict):
                return last_raw
            try:
                return parse_json(last_raw)
            except ValueError as e:
                head = (str(last_raw)).strip()[:120].replace("\n", "\\n") or "（空输出）"
                _emit(
                    self.on_progress, "retry",
                    f"[{label}·重试 {attempt}] {e} —— 原始输出头部: {head}",
                    label=label, attempt=attempt,
                )
        raise ValueError(f"{label} 3 次尝试均无法解析")

    # -- 分块生成 ---------------------------------------------------------

    def user_for(self, section: str, extra: str, draft: dict, prior: dict | None, err: str | None,
                 targets: set[str]) -> str:
        user = f"<素材>\n{self.source_text}\n</素材>\n"
        if draft.get("npcs") or draft.get("schedule"):
            decl = {
                "属性": {k: v.get("label") for k, v in draft.get("schedule", {}).get("stats", {}).items()},
                "好感对象": {k: v.get("label") for k, v in draft.get("schedule", {}).get("affections", {}).items()},
                "旗标": sorted(draft.get("schedule", {}).get("flags", {})),
                "行动id": [a["id"] for a in draft.get("schedule", {}).get("actions", [])],
                "NPC": {k: v.get("name") for k, v in draft.get("npcs", {}).items()},
                "关键抉择id": [
                    c["id"] for n in draft.get("mainline", {}).get("nodes", [])
                    for c in n.get("critical_choices", [])
                ],
            }
            user += (
                f"\n<已生成声明（引用这些名字时保持精确一致）>\n"
                f"{json.dumps(decl, ensure_ascii=False)}\n</已生成声明>\n"
            )
        if err and prior and section in targets:
            user += (
                f"\n<该块上一版>\n{json.dumps(prior_piece(prior, section), ensure_ascii=False)}\n</该块上一版>\n"
                f"<校验错误>\n{err}\n</校验错误>\n"
            )
        return user + f"\n{extra}"

    def generate_sections(self, prior: dict | None = None, err: str | None = None) -> dict:
        """生成（或按错误修复）草稿。`err` 非空 = 修复轮，只碰 `repair_sections` 命中的块。"""
        draft: dict = dict(prior) if prior else {}
        targets = set(repair_sections(err)) if err else set(ALL_SECTIONS)

        def gen(system: str, extra: str, max_tokens: int, purpose: str, label: str) -> dict:
            _emit(self.on_progress, "extract", label, label=label, purpose=purpose)
            return self.extract_json(
                system, self.user_for(purpose, extra, draft, prior, err, targets),
                max_tokens, purpose, label,
            )

        if "world" in targets:
            draft["world"] = gen(
                EXTRACT_WORLD_SYSTEM, "请生成 world 核心字段的 JSON。",
                EXTRACT_MAX_TOKENS, "import_world", "生成[world]",
            )
        if "world_extra" in targets:
            extra = gen(
                EXTRACT_WORLD_EXTRA_SYSTEM, "请生成 opening 与 lore 的 JSON。",
                EXTRACT_MAX_TOKENS, "import_world_extra", "生成[world_extra]",
            )
            draft.setdefault("world", {}).update(extra)
        if "npcs" in targets:
            npcs: dict = {}
            for i in range(1, 4):
                seen = "、".join(c.get("name", "?") for c in npcs.values()) or "（无）"
                card = gen(
                    EXTRACT_NPC_SYSTEM,
                    f"请生成素材中的第 {i} 个主要可玩角色的角色卡 JSON"
                    f"（{{\"角色id\": {{...}}}}；已生成 NPC：{seen}；素材中没有第 {i} 个角色则输出 {{}}）。",
                    EXTRACT_MAX_TOKENS, "import_npcs", f"生成[npc{i}]",
                )
                if not card:
                    break
                for npc_id, card_data in card.items():
                    if "角色id" in str(npc_id) or "角色id" in str(card_data.get("id", "")):
                        continue  # 占位符卡：模型未替换 <角色id>，丢弃（由校验错误驱动修复）
                    npcs[npc_id] = card_data
            draft["npcs"] = npcs
        if "schedule" in targets:
            draft["schedule"] = gen(
                EXTRACT_SCHEDULE_SYSTEM, "请生成 schedule 数值与旗标的 JSON。",
                EXTRACT_MAX_TOKENS, "import_schedule", "生成[schedule]",
            )
        if "actions" in targets:
            draft.setdefault("schedule", {})["actions"] = gen(
                EXTRACT_ACTION_SYSTEM, "请生成 2~4 个日程行动的 JSON。",
                EXTRACT_MAX_TOKENS, "import_actions", "生成[actions]",
            ).get("actions", [])
        if "nodes" in targets:
            draft["mainline"] = {
                "nodes": gen(
                    EXTRACT_NODE_SYSTEM, "请生成 3~5 个主线节点的 JSON。",
                    EXTRACT_MAX_TOKENS, "import_nodes", "生成[nodes]",
                ).get("nodes", [])
            }
        if "events" in targets:
            draft["events"] = gen(
                EXTRACT_EVENTS_SYSTEM, "请生成 events 的 JSON。",
                EXTRACT_MAX_TOKENS, "import_events", "生成[events]",
            ).get("events", [])
        if "endings" in targets:
            draft["endings"] = gen(
                EXTRACT_ENDINGS_SYSTEM, "请生成 endings 的 JSON。",
                EXTRACT_MAX_TOKENS, "import_endings", "生成[endings]",
            ).get("endings", [])
        return draft

    def generate_corpus(self, draft: dict, *, rounds: int = 2) -> dict:
        """生成 30 条 Judge 语料（对抗 3 类 ×6 + 正常 2 批 ×6），并按结构校验重试。"""
        corpus_summary = json.dumps({
            "world": {k: draft["world"].get(k) for k in ("name", "era", "forbidden", "player_role", "player_goal")},
            "npcs": {k: {f: v.get(f) for f in ("name", "identity", "personality", "speech_style", "boundaries", "forbidden", "affection_stages")}
                     for k, v in draft.get("npcs", {}).items()},
        }, ensure_ascii=False)
        corpus_draft: dict | None = None
        for i in range(rounds):
            try:
                cases: list = []
                for cat, desc in CORPUS_CAT_DESC.items():
                    _emit(self.on_progress, "corpus", f"语料[{cat}]", label=f"语料[{cat}]")
                    batch = self.extract_json(
                        CORPUS_ADV_ONE_SYSTEM.format(cat=cat, desc=desc),
                        f"<世界包摘要>\n{corpus_summary}\n</世界包摘要>\n\n请生成 6 条 {cat} 类对抗语料。",
                        max_tokens=EXTRACT_MAX_TOKENS, purpose=f"import_corpus_{cat}", label=f"语料[{cat}]",
                    )
                    cases += batch.get("cases", [])
                for part in range(2):
                    _emit(self.on_progress, "corpus", f"语料[正常{part + 1}]", label=f"语料[正常{part + 1}]")
                    batch = self.extract_json(
                        CORPUS_NORMAL_ONE_SYSTEM,
                        f"<世界包摘要>\n{corpus_summary}\n</世界包摘要>\n\n请生成 6 条正常语料（第 {part + 1} 批）。",
                        max_tokens=EXTRACT_MAX_TOKENS, purpose=f"import_corpus_normal{part}", label=f"语料[正常{part + 1}]",
                    )
                    cases += batch.get("cases", [])
                corpus_draft = {"cases": cases}
            except ValueError as e:
                _emit(self.on_progress, "corpus", f"[语料] {e}")
                continue
            return {"draft": corpus_draft, "summary": corpus_summary, "error": None}
        return {"draft": corpus_draft, "summary": corpus_summary, "error": "提取失败"}


# ---------------------------------------------------------------------------
# 管线入口
# ---------------------------------------------------------------------------


@dataclass
class GenerateOptions:
    rounds: int = 4          # 校验-修复最大轮数
    with_corpus: bool = False
    draft_only: bool = False


@dataclass
class GenerateResult:
    pack_dir: Path
    draft: dict
    repairs: int = 0
    corpus_written: int = 0
    stages: list[str] = field(default_factory=list)  # 走过的阶段（服务端可用作时间线）

    @property
    def summary(self) -> str:
        return summary(self.draft)


def generate(
    pack_dir: Path,
    source_text: str,
    llm,
    *,
    options: GenerateOptions | None = None,
    offline: bool = False,
    on_progress: ProgressFn | None = None,
    extractor: SectionExtractor | None = None,
) -> GenerateResult:
    """完整生成管线：提取 → 物料化 → 校验-修复 → 语料 → smoke_profile。

    **不打印、不读环境、不起子进程**：进度全部经 `on_progress`。
    `draft_only=True` 时只落 `draft.json` 并返回（作者确认理解无误后再正式生成）。

    调用方（`scripts/import_story.py` 的 CLI，或 Web 创作工作台的后台任务）负责
    "把事件变成输出"这一层。
    """
    opts = options or GenerateOptions()
    ext = extractor or SectionExtractor(llm, source_text, offline=offline, on_progress=on_progress)
    result = GenerateResult(pack_dir=pack_dir, draft={})

    _emit(on_progress, "start", f"素材 {len(source_text)} 字 → 分块提取世界包草稿……")
    try:
        draft = ext.generate_sections()
    except ValueError as e:
        raise WorldgenError(f"提取失败: {e}") from e
    result.draft = draft
    result.stages.append("extract")

    if opts.draft_only:
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / "draft.json").write_text(
            json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result.stages.append("draft_only")
        _emit(on_progress, "draft", f"草稿已保存 → {pack_dir / 'draft.json'}")
        return result

    materialize(pack_dir, draft)
    result.stages.append("materialize")

    for _ in range(opts.rounds):
        err = validate_pack(pack_dir)
        if err is None:
            break
        result.repairs += 1
        _emit(on_progress, "repair", f"[修复 {result.repairs}] {err.splitlines()[0][:120]}",
              round=result.repairs)
        try:
            draft = ext.generate_sections(prior=draft, err=err)
        except ValueError as e:
            raise WorldgenError(f"修复轮解析失败: {e}") from e
        result.draft = draft
        materialize(pack_dir, draft)
    if validate_pack(pack_dir) is not None:
        raise WorldgenError(f"{opts.rounds} 轮修复后仍未通过校验（手动查看错误并调整素材/重跑）")
    result.stages.append("validate")
    _emit(on_progress, "validate",
          "[✓] check-worldpack 通过" + (f"（修复 {result.repairs} 轮）" if result.repairs else ""))

    if opts.with_corpus:
        _emit(on_progress, "corpus", "生成 Judge 语料（30 条）……")
        out = ext.generate_corpus(draft)
        if out["draft"] is not None:
            write_corpus(pack_dir, out["draft"])
            result.corpus_written = len(out["draft"].get("cases", []))
        if out["draft"] is None or validate_corpus(pack_dir) is not None:
            raise WorldgenError("语料结构未达标（重跑 with_corpus，或手工补充）")
        result.stages.append("corpus")
        # 报**实际**条数而不是硬编码 30：离线假 LLM 只产 25 条（对抗 3×5 + 正常 10），
        # 真实路径才是 6×3 + 6×2 = 30。日志说 30 而实际 25 属于"日志撒谎"，
        # 排查时最费时间的那一类。
        _emit(on_progress, "corpus", f"[✓] Judge 语料 {result.corpus_written} 条结构通过")

    _dump(pack_dir / "smoke_profile.yaml", smoke_profile(draft))
    result.stages.append("smoke_profile")
    _emit(on_progress, "smoke", "[✓] smoke_profile.yaml 已生成（worldpack_smoke.py 自动读取）")
    _emit(on_progress, "done", f"[✓] 世界包已生成 → {pack_dir}")
    return result


def build_llm(offline: bool) -> tuple[Any, UsageTracker | None]:
    """按模式建 LLM：离线用假 LLM（零成本、可重复），真机用引擎的 LLMClient。

    成本单独落 `saves/usage-import.jsonl`（与游戏账本分开，便于回答"生成一个包花了多少"）。
    """
    if offline:
        return OfflineLLM(), None
    from .config import load_settings  # 延迟导入：离线路径不需要配置

    tracker = UsageTracker("saves/usage-import.jsonl")
    return LLMClient.from_settings(load_settings(), [], tracker=tracker), tracker
