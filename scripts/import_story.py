"""B：素材导入工具——作者上传小说/大纲/设定，生成可过质量门的世界包。

用法：
  uv run python scripts/import_story.py <素材文件...> --name my_world [选项]

流程（素材 → 过检世界包）：
  ① LLM 提取（**粒度原子化**：每个调用只生成一小块——实测 deepseek-v4-flash 思考模式下，
     大任务提示词会触发无界思考烧光输出预算；Judge 类小任务 300+ 次零失败，故每调用
     都切成 Judge 粒度：≤2000 预算 + 短提示词 + temp 0 + 纯文本）；
  ② 物化 + 校验-修复循环：写 YAML → load_worldpack 校验，错误按关键词路由到相关小块
     重生成（≤4 轮，每轮只碰相关块）；
  ③ 语料生成（--with-corpus）：30 条 Judge 语料（对抗/正常分两次调用）+ 结构校验；
  ④ 冒烟 profile：生成 <包>/smoke_profile.yaml（worldpack_smoke.py 自动读取）；
  ⑤ 可选 --live：顺跑 E1 Judge 门禁 + 真机冒烟（约 ¥1-2）。

选项：
  --name      包目录名（必填，只允许字母/数字/_/-）
  --pack-dir  输出根目录（默认 world-packs）
  --rounds    校验-修复最大轮数（默认 4）
  --with-corpus   生成 Judge 语料
  --live      生成后立即跑真机门禁（需 --with-corpus 与 API Key）
  --draft-only    只输出提取草稿 JSON（作者确认用），不物化
  --offline   离线回归模式：内嵌"坏草稿→好草稿"假 LLM，测物化/修复/语料结构管线
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml

from game_agent.config import load_settings
from game_agent.judge_corpus import load_corpus
from game_agent.llm import LLMClient
from game_agent.storyline import _forbidden_tokens
from game_agent.usage import UsageTracker
from game_agent.worldpack import WorldPackError, load_worldpack

_NAME_PATTERN = re.compile(r"[A-Za-z0-9_\-]+")

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
    events = []
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


class _OfflineLLM:
    """离线假 LLM：第 1 轮 npcs 返回空触发修复，之后返回好草稿（逐块计数器）。"""

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
# 工具主体
# ---------------------------------------------------------------------------


def _read_sources(paths: list[str]) -> str:
    parts = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            raise SystemExit(f"[✗] 素材不存在: {p}")
        if p.is_dir():
            for f in sorted(p.glob("*.md")) + sorted(p.glob("*.txt")):
                parts.append(f.read_text(encoding="utf-8"))
        else:
            parts.append(p.read_text(encoding="utf-8"))
    text = "\n\n".join(parts)
    if len(text) > 300_000:
        text = text[:300_000]
        print("[!] 素材超长，截断至前 30 万字（提取主线足够；如需全量请提供大纲）")
    return text


def _parse_json(text: str) -> dict:
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


def _diagnose(raw: str) -> str:
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


def _materialize(pack_dir: Path, draft: dict) -> None:
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


def _validate(pack_dir: Path) -> str | None:
    try:
        load_worldpack(pack_dir)
        return None
    except WorldPackError as e:
        return str(e)


def _validate_corpus(pack_dir: Path) -> str | None:
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


def _write_corpus(pack_dir: Path, draft: dict) -> None:
    (pack_dir / "judge_corpus.yaml").write_text(
        yaml.safe_dump(draft, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100),
        encoding="utf-8",
    )


def _smoke_profile(pack_dir: Path, draft: dict) -> dict:
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
        "forbidden_scan": _forbidden_tokens_from(draft["world"].get("forbidden", [])),
        "observe_modern": [],
        "target": endings[0]["title"] if endings else "（无结局）",
    }


def _forbidden_tokens_from(entries: list[str]) -> list[str]:
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


def _summary(draft: dict) -> str:
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


def _repair_sections(err: str) -> list[str]:
    """按校验错误关键词路由到需要重生成的小块。"""
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


def _prior_piece(prior: dict, section: str):
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


def _failed_cases(report: dict) -> tuple[list, list, list]:
    """从门禁报告挑出需要修的用例：漏判 / 误报 / **不可判定**。

    `hit is None` = 该用例无法判定（判官空响应或截断，升级重试后仍不可用）。
    它既不是"漏判"也不是"误报"——不得据此改写语料（会把好用例改坏），
    单独返回由调用方报数并中止自动修复。
    """
    cases = report.get("cases", [])
    missed = [c for c in cases if c["category"] != "normal" and c.get("hit") is False]
    fps = [c for c in cases if c["category"] == "normal" and c.get("hit") is True]
    unknown = [c for c in cases if c.get("hit") is None]
    return missed, fps, unknown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="素材导入：小说/大纲/设定 → 世界包（工具 B）")
    parser.add_argument("sources", nargs="+", help="素材文件（txt/md）或目录")
    parser.add_argument("--name", required=True, help="包目录名（字母/数字/_/-）")
    parser.add_argument("--pack-dir", default="world-packs", help="输出根目录")
    parser.add_argument("--rounds", type=int, default=4, help="校验-修复最大轮数")
    parser.add_argument("--with-corpus", action="store_true", help="生成 30 条 Judge 语料")
    parser.add_argument("--live", action="store_true", help="生成后立即跑真机门禁（需 API Key）")
    parser.add_argument("--draft-only", action="store_true", help="只输出草稿 JSON，不物化")
    parser.add_argument("--offline", action="store_true", help="离线回归模式（内嵌假 LLM）")
    args = parser.parse_args(argv)

    if not _NAME_PATTERN.fullmatch(args.name):
        print(f"[✗] 非法包名（仅允许字母/数字/_/-）: {args.name!r}")
        return 1
    pack_dir = Path(args.pack_dir) / args.name

    source_text = _read_sources(args.sources)
    if not args.offline and not load_settings().has_api_key:
        print("[✗] 未配置 DEEPSEEK_API_KEY（离线回归请加 --offline）")
        return 1

    if args.offline:
        llm = _OfflineLLM()  # type: ignore[assignment]
        tracker = None
    else:
        tracker = UsageTracker("saves/usage-import.jsonl")
        llm = LLMClient.from_settings(load_settings(), [], tracker=tracker)

    def raw_call(messages, max_tokens: int, temperature: float, purpose: str):
        """一次提取调用：离线走假 complete()；真机走 submit_json 工具通道（tool_choice=auto）。

        （实测教训：工具强制指令必须放在 system 提示词**第一条**——引擎回合可靠性的
        关键差异点；放末尾时思考模式模型会无视它并烧光输出预算。）
        """
        if args.offline:
            return llm.complete(messages, max_tokens=max_tokens, temperature=temperature, purpose=purpose)
        msgs = [dict(messages[0])]
        msgs[0]["content"] = "必须调用 submit_json 工具提交结果，否则任务失败。\n" + msgs[0]["content"]
        msgs.extend(messages[1:])
        resp = llm._client.chat.completions.create(
            model=llm.model,
            messages=msgs,
            tools=[SUBMIT_JSON_TOOL],
            tool_choice="auto",
            max_tokens=max_tokens,
            temperature=temperature,
        )
        llm._record_usage(llm.model, purpose, resp)
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

    def extract_json(system: str, user: str, max_tokens: int, purpose: str, label: str) -> dict:
        """带重试的 JSON 提取（3 次 + 温度循环；预算 8000 后空输出根因已除，重试只兜偶发）。"""
        for attempt, temperature in enumerate((0.7, 1.0, 1.3), start=1):
            last_raw = raw_call(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens, temperature, purpose,
            )
            if isinstance(last_raw, dict):
                return last_raw
            try:
                return _parse_json(last_raw)
            except ValueError as e:
                head = (str(last_raw)).strip()[:120].replace("\n", "\\n") or "（空输出）"
                print(f"[{label}·重试 {attempt}] {e} —— 原始输出头部: {head}")
        raise ValueError(f"{label} 3 次尝试均无法解析")

    def generate_sections(prior: dict | None = None, err: str | None = None) -> dict:
        draft: dict = dict(prior) if prior else {}
        targets = (
            set(_repair_sections(err)) if err
            else {"world", "world_extra", "npcs", "schedule", "actions", "nodes", "events", "endings"}
        )

        def user_for(section: str, extra: str) -> str:
            user = f"<素材>\n{source_text}\n</素材>\n"
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
                    f"\n<该块上一版>\n{json.dumps(_prior_piece(prior, section), ensure_ascii=False)}\n</该块上一版>\n"
                    f"<校验错误>\n{err}\n</校验错误>\n"
                )
            return user + f"\n{extra}"

        def gen(system: str, extra: str, max_tokens: int, purpose: str, label: str) -> dict:
            return extract_json(system, user_for(purpose, extra), max_tokens, purpose, label)

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

    # ① 提取（粒度原子化）
    print(f"素材 {len(source_text)} 字 → 分块提取世界包草稿……")
    try:
        draft = generate_sections()
    except ValueError as e:
        print(f"[✗] 提取失败: {e}")
        return 1

    if args.draft_only:
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / "draft.json").write_text(
            json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("[草稿] 提取摘要（请作者确认故事理解无误）：")
        print(_summary(draft))
        print(f"\n草稿已保存 → {pack_dir / 'draft.json'}（确认后用原命令去掉 --draft-only 生成）")
        return 0

    # ② 物化 + 校验-修复循环
    _materialize(pack_dir, draft)
    repairs = 0
    for _ in range(args.rounds):
        err = _validate(pack_dir)
        if err is None:
            break
        repairs += 1
        print(f"[修复 {repairs}] {err.splitlines()[0][:120]}")
        try:
            draft = generate_sections(prior=draft, err=err)
        except ValueError as e:
            print(f"[✗] 修复轮解析失败: {e}")
            return 1
        _materialize(pack_dir, draft)
    if _validate(pack_dir) is not None:
        print(f"[✗] {args.rounds} 轮修复后仍未通过校验（手动查看错误并调整素材/重跑）")
        return 1
    print("[✓] check-worldpack 通过" + (f"（修复 {repairs} 轮）" if repairs else ""))

    # ③ 语料（--with-corpus）
    if args.with_corpus:
        print("生成 Judge 语料（30 条）……")
        corpus_summary = json.dumps({
            "world": {k: draft["world"].get(k) for k in ("name", "era", "forbidden", "player_role", "player_goal")},
            "npcs": {k: {f: v.get(f) for f in ("name", "identity", "personality", "speech_style", "boundaries", "forbidden", "affection_stages")}
                     for k, v in draft.get("npcs", {}).items()},
        }, ensure_ascii=False)
        corpus_draft = None
        for i in range(2):
            try:
                cases: list = []
                for cat, desc in CORPUS_CAT_DESC.items():
                    batch = extract_json(
                        CORPUS_ADV_ONE_SYSTEM.format(cat=cat, desc=desc),
                        f"<世界包摘要>\n{corpus_summary}\n</世界包摘要>\n\n请生成 6 条 {cat} 类对抗语料。",
                        max_tokens=EXTRACT_MAX_TOKENS, purpose=f"import_corpus_{cat}", label=f"语料[{cat}]",
                    )
                    cases += batch.get("cases", [])
                for part in range(2):
                    batch = extract_json(
                        CORPUS_NORMAL_ONE_SYSTEM,
                        f"<世界包摘要>\n{corpus_summary}\n</世界包摘要>\n\n请生成 6 条正常语料（第 {part + 1} 批）。",
                        max_tokens=EXTRACT_MAX_TOKENS, purpose=f"import_corpus_normal{part}", label=f"语料[正常{part + 1}]",
                    )
                    cases += batch.get("cases", [])
                corpus_draft = {"cases": cases}
            except ValueError as e:
                print(f"[语料] {e}")
                continue
            _write_corpus(pack_dir, corpus_draft)
            err = _validate_corpus(pack_dir)
            if err is None:
                break
            print(f"[语料修复 {i + 1}] {err}")
        if corpus_draft is None or _validate_corpus(pack_dir) is not None:
            print("[✗] 语料结构未达标（重跑 --with-corpus，或手工补充）")
            return 1
        print("[✓] Judge 语料 30 条结构通过")

    # ④ 冒烟 profile
    _dump(pack_dir / "smoke_profile.yaml", _smoke_profile(pack_dir, draft))
    print("[✓] smoke_profile.yaml 已生成（worldpack_smoke.py 自动读取）")

    print("\n[✓] 世界包已生成 → " + str(pack_dir))
    print("提取摘要（请作者确认故事理解无误）：")
    print(_summary(draft))

    if tracker is not None:
        print("\n" + tracker.cost_report())

    # ⑤ 可选 --live：真机门禁（含门禁驱动的语料修复，最多 2 轮）
    if args.live:
        if not args.with_corpus:
            print("[✗] --live 需要 --with-corpus（先有语料才有门禁）")
            return 1
        print("\n===== 真机质量门 =====")

        def latest_report():
            candidates = sorted(
                Path("reports").glob("judge_sensitivity_*.json"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            for p in candidates:
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                if data.get("pack") == draft["world"].get("name"):
                    return data
            return None

        def run_gate() -> bool:
            r = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("judge_sensitivity.py")),
                 "--pack", str(pack_dir)],
                check=False,
            )
            return r.returncode == 0

        gate_ok = run_gate()
        for repair_round in range(2):
            if gate_ok:
                break
            report = latest_report()
            if report is None:
                print("[✗] 门禁失败且未找到门禁报告（无法自动修复语料）")
                return 1
            missed, fps, unknown = _failed_cases(report)
            if not missed and not fps:
                if unknown:
                    print(
                        f"[✗] 门禁未通过，但有 {len(unknown)} 条用例**不可判定**"
                        f"（未知 ≠ 失败）——请重跑门禁或检查判官可用性，不自动改语料"
                    )
                else:
                    print("[✗] 门禁未通过但无逐案失败信息（判据问题，非语料）")
                return 1

            def fix_text(cases: list, kind: str) -> str:
                lines = []
                for c in cases:
                    last = next(
                        (r["verdict"] for r in reversed(c.get("rounds", [])) if r.get("verdict")),
                        "（判定为空）",
                    )
                    lines.append(f"- {c['id']}（{c['category']}）：{last[:100]}")
                return (
                    f"<门禁失败用例>\n" + "\n".join(lines) + "\n</门禁失败用例>\n"
                    f"这些用例被 Judge {kind}——请重写为更明确、无歧义的版本。"
                )

            print(f"[语料修复轮 {repair_round + 1}] 漏判 {len(missed)} 条 / 误报 {len(fps)} 条")
            rebuilt: dict = {}
            for cat in ("ooc", "setting", "confab"):
                bad = [c for c in missed if c["category"] == cat]
                if bad:
                    rebuilt[cat] = extract_json(
                        CORPUS_ADV_ONE_SYSTEM.format(cat=cat, desc=CORPUS_CAT_DESC[cat]),
                        f"<世界包摘要>\n{corpus_summary}\n</世界包摘要>\n\n"
                        + fix_text(bad, "漏判（应拦却放过）")
                        + f"\n请重写这 {len(bad)} 条 {cat} 类对抗语料（输出 {{\"cases\": [...]}}）。",
                        max_tokens=EXTRACT_MAX_TOKENS, purpose=f"import_corpus_fix_{cat}",
                        label=f"语料修复[{cat}]",
                    )
            if fps:
                rebuilt["normal"] = extract_json(
                    CORPUS_NORMAL_ONE_SYSTEM,
                    f"<世界包摘要>\n{corpus_summary}\n</世界包摘要>\n\n"
                    + fix_text(fps, "误判为违规（应放行）")
                    + f"\n请重写这 {len(fps)} 条正常语料（输出 {{\"cases\": [...]}}）。",
                    max_tokens=EXTRACT_MAX_TOKENS, purpose="import_corpus_fix_normal",
                    label="语料修复[normal]",
                )
            old = load_corpus(pack_dir)
            by_id = {c.id: c for c in old}
            fixed_cases = []
            for c in old:
                if c.id in {b["id"] for b in (missed + fps)}:
                    continue  # 丢弃失败用例
                fixed_cases.append(c)
            for cat, batch in rebuilt.items():
                fixed_cases += batch.get("cases", [])
            # 结构校验后落盘（修复批是 dict，旧用例是 JudgeCase——统一转 dict）
            def _as_dict(c) -> dict:
                return c if isinstance(c, dict) else vars(c)

            _write_corpus(pack_dir, {"cases": [_as_dict(c) for c in fixed_cases]})
            err = _validate_corpus(pack_dir)
            if err is not None:
                print(f"[✗] 语料修复后结构仍不达标：{err}（请手工补充）")
                return 1
            gate_ok = run_gate()
        if not gate_ok:
            print("[✗] E1 Judge 门禁在自动修复后仍未通过（手工修语料或重跑）")
            return 1

        # 真机冒烟（门禁过了再跑，避免白烧）
        print("----- 真机冒烟 -----")
        r = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("worldpack_smoke.py")),
             "--pack", str(pack_dir)],
            check=False,
        )
        if r.returncode != 0:
            print("[✗] 真机冒烟未通过（退出码 1）")
            return 1
        print("[✓] 真机质量门通过（Judge 门禁 + 冒烟）")

    print("\n下一步（作者可自行复查）：")
    print("  uv run pytest")
    print(f"  uv run python -m game_agent play {pack_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
