"""Judge 三态判定（离线）：空 = 未知，不是"通过"。

背景：`parse_verdict("")` 原返回 `True`（通过）——思考模式吃光预算导致空响应时，
判官**静默放行**（假阴性），门禁也把空轮记成"未拦截"。
修复：判定改为三态 `True / False / None(未知)`；未知轮不进多数票分母，
未知用例不进分类分母（报告单独报数），且**未知不得当作通过放行**。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from fakes import FakeClient, msg, resp

from game_agent.judge import JudgeSystem, parse_verdict
from game_agent.judge_corpus import load_corpus, majority_hit
from game_agent.llm import LLMClient
from game_agent.worldpack import load_worldpack

REPO_ROOT = Path(__file__).resolve().parent.parent
PACK_PATH = REPO_ROOT / "world-packs" / "ancient_jianghu"


def _judge(responses):
    return JudgeSystem(LLMClient(FakeClient(responses), "fake", []))


def _load_script(name: str, filename: str):
    """按 test_import_story.py 的同款做法加载 scripts/ 下的脚本（不执行 main）。"""
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# parse_verdict / JudgeSystem.check：三态
# ---------------------------------------------------------------------------


def test_parse_verdict_empty_is_unknown():
    assert parse_verdict("")[0] is None
    assert parse_verdict("   \n ")[0] is None
    assert parse_verdict("通过")[0] is True
    assert parse_verdict("OOC：角色说出网络用语。")[0] is False


def test_judge_double_empty_is_unknown_not_pass():
    """两次都空（升级预算也被吃光）→ 未知，绝不当成通过。"""
    judge = _judge([resp(msg(content="")), resp(msg(content=""), finish_reason="length")])
    ok, verdict = judge.check("某叙事", "材料")
    assert ok is None and verdict == ""
    assert len(judge.llm._client.chat.completions.calls) == 2  # 确实重试过


def test_judge_truncated_verdict_escalates_and_uses_retry():
    """截断的判定不可信 → 升级重试，并采用重试结果。"""
    from game_agent.budgets import EMPTY_RETRY_TOKENS

    judge = _judge(
        [
            resp(msg(content="问题类型：OOC（半"), finish_reason="length"),
            resp(msg(content="通过"), finish_reason="stop"),
        ]
    )
    ok, verdict = judge.check("某叙事", "材料")
    assert ok is True and verdict == "通过"
    calls = judge.llm._client.chat.completions.calls
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == EMPTY_RETRY_TOKENS


def test_judge_api_error_is_unknown():
    class _Boom:
        def __init__(self):
            self.chat = type(
                "C", (), {"completions": type("CC", (), {"create": self._fail})()}
            )()

        @staticmethod
        def _fail(**kwargs):
            raise RuntimeError("api down")

    judge = JudgeSystem(LLMClient(_Boom(), "fake", []))
    assert judge.check("任意叙事", "材料") == (None, "")  # 未知 ≠ 通过


# ---------------------------------------------------------------------------
# 多数票：未知轮不进分母
# ---------------------------------------------------------------------------


def test_majority_hit_ignores_unknown_rounds():
    assert majority_hit([False, None, None]) is True  # 唯一已知轮被拦（min_known 默认 1）
    assert majority_hit([True, None, None]) is False  # 唯一已知轮通过 → 未拦
    assert majority_hit([False, False, True]) is True  # 2/3 拦截
    assert majority_hit([False, True, True]) is False  # 1/3 拦截
    assert majority_hit([None, None, None]) is None  # 全部未知 → 不可判定
    assert majority_hit([]) is None


def test_majority_hit_requires_min_known_rounds():
    """证据不足不得定案：rounds=3 时要求 ≥2 轮已知（2026-09-12 教训）。

    只改"未知不进分母"而不设下限时，1 轮已知即可定案 → 单次噪声把误报率抬高 17~22pp。
    """
    assert majority_hit([False, None, None], min_known=2) is None
    assert majority_hit([True, None, None], min_known=2) is None
    assert majority_hit([False, False, None], min_known=2) is True
    assert majority_hit([True, True, None], min_known=2) is False
    assert majority_hit([False, None, None]) is True  # 默认 min_known=1 时仍可定案（向后兼容）


# ---------------------------------------------------------------------------
# 消费方：门禁脚本 / 语料修复工具
# ---------------------------------------------------------------------------


class _StubJudge:
    """按序吐出预置判定（None = 未知），不触发任何 API。"""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)

    def check(self, narration, materials):
        return self.verdicts.pop(0), "v"


def test_gate_run_case_excludes_unknown_rounds():
    """门禁：未知轮不进分母；**已知轮 <2（rounds=3）不定案**（证据不足，2026-09-12 收严）。"""
    mod = _load_script("judge_sensitivity_under_test", "judge_sensitivity.py")
    pack = load_worldpack(PACK_PATH)
    case = load_corpus(PACK_PATH)[0]

    hit, detail = mod._run_case(_StubJudge([False, False, None]), pack, case, 3)
    assert hit is True and len(detail) == 3

    hit, _ = mod._run_case(_StubJudge([True, True, None]), pack, case, 3)
    assert hit is False

    hit, _ = mod._run_case(_StubJudge([False, None, None]), pack, case, 3)
    assert hit is None  # 仅 1 轮已知 → 不可判定（单轮噪声不得翻案）

    hit, _ = mod._run_case(_StubJudge([None, None, None]), pack, case, 3)
    assert hit is None  # 全部未知


def test_gate_summary_counts_unknown_separately():
    mod = _load_script("judge_sensitivity_under_test2", "judge_sensitivity.py")
    results = [
        {"id": "o1", "category": "ooc", "hit": True, "rounds": []},
        {"id": "o2", "category": "ooc", "hit": None, "rounds": []},
        {"id": "s1", "category": "setting", "hit": True, "rounds": []},
        {"id": "c1", "category": "confab", "hit": True, "rounds": []},
        {"id": "n1", "category": "normal", "hit": False, "rounds": []},
    ]
    summary, failed = mod._summarize(results)
    # 未知用例不进分母，单独报数
    assert summary["ooc"] == {
        "n": 1, "intercepted": 1, "rate": 1.0, "pass": True, "unknown": 1,
    }
    assert summary["normal"] == {
        "n": 1, "false_positives": 0, "rate": 0.0, "pass": True, "unknown": 0,
    }
    assert failed is False

    # 某类全部不可判定 → 该类不通过（"无法验证" ≠ "达标"，门禁不得沉默放行）
    summary2, failed2 = mod._summarize(
        [
            {"id": "o3", "category": "ooc", "hit": None, "rounds": []},
            {"id": "s2", "category": "setting", "hit": True, "rounds": []},
            {"id": "c2", "category": "confab", "hit": True, "rounds": []},
            {"id": "n2", "category": "normal", "hit": False, "rounds": []},
        ]
    )
    assert summary2["ooc"]["pass"] is False and failed2 is True


def test_partial_category_run_is_not_a_gate():
    """`--category confab` 之类的部分运行：只统计跑到的类别，不作门禁口径。

    动机（2026-09-12）：语料只改了 confab，重测两侧却要付整包 flash 的钱
    （整包 ≈¥0.83~1.67，单类 ≈¥0.25~0.5）；但部分运行绝不能被误读成"门禁通过"。
    """
    mod = _load_script("judge_sensitivity_under_test3", "judge_sensitivity.py")
    results = [
        {"id": "c1", "category": "confab", "hit": True, "rounds": []},
        {"id": "c2", "category": "confab", "hit": False, "rounds": []},
    ]

    summary, failed = mod._summarize(results, ["confab"])

    assert set(summary) == {"confab"}  # 没跑的类别既不判过也不判不过
    assert summary["confab"]["pass"] is False and failed is True

    # 单类满分也不代表门禁通过（其他类别根本没测）
    ok_summary, _ = mod._summarize(
        [{"id": "c3", "category": "confab", "hit": True, "rounds": []}], ["confab"]
    )
    assert "normal" not in ok_summary and "ooc" not in ok_summary


def test_import_story_repair_skips_unknown_cases():
    mod = _load_script("import_story_under_test", "import_story.py")
    report = {
        "cases": [
            {"id": "a", "category": "ooc", "hit": False},
            {"id": "b", "category": "normal", "hit": True},
            {"id": "c", "category": "ooc", "hit": None},
        ]
    }
    missed, fps, unknown = mod._failed_cases(report)
    assert [c["id"] for c in missed] == ["a"]
    assert [c["id"] for c in fps] == ["b"]
    assert [c["id"] for c in unknown] == ["c"]
