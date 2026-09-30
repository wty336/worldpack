"""J 系列守卫测试：上下文溢出的**预检**与**压缩后重试**（离线假客户端）。

## 为什么值得单独一条恢复路径

溢出（provider 以"输入过长"拒绝请求）是**确定性**的上下文问题，不是模型发挥失常。
旧路径下它与协议失败同命运：同一份超长上下文被重试 3 次（每次都可能再被拒）→
熔断 → `_txn_rollback` 把本轮工具效果全部撤销 → 玩家看到"本轮跳过"，
而这三个贵调用白烧。

对齐 pi-agent 的 compact-and-retry：**压缩一次，原地重试**，且只允许一次。

## 契约

1. `is_context_overflow` 只认"输入过长"类错误，**不得**把限流/网络错误误判为溢出
   （误判会去压缩有效历史，代价比不恢复更大）；
2. 预检：估算越过"窗口 − 输出预算 − 余量"时，**在一次请求被拒之前**就压缩；
3. 恢复：请求被拒 → 压缩 → 重试；本轮工具效果**照常提交**（不是回滚）；
4. 只重试一次：压缩后仍熔断 → 走原有熔断回滚（行为与改前一致）；
5. `context_window=0`（未声明）时全部关闭，行为与改前逐字一致。
"""

from __future__ import annotations

import json
from pathlib import Path

from fakes import FakeClient, msg, resp, tool_call

from game_agent.budgets import is_context_overflow
from game_agent.game import Game
from game_agent.llm import LLMClient, LLMTurnError, build_tools
from game_agent.state import GameState
from game_agent.worldpack import load_worldpack

PACK_PATH = Path(__file__).resolve().parent.parent / "world-packs" / "baseline_probe"


class _OverflowError(Exception):
    """模拟 OpenAI 兼容端点的 400 上下文超限（真实措辞）。"""


OVERFLOW_MESSAGE = (
    "Error code: 400 - {'error': {'message': \"This model's maximum context length "
    "is 65536 tokens. However, your messages resulted in 71234 tokens. Please "
    'reduce the length of the messages.\', \'type\': \'invalid_request_error\'}}'
)


def _submit(narration="她望着你。"):
    return tool_call(
        "c2", "submit_narration",
        {"narration": narration, "choices": ["甲", "乙", "丙"], "plot_signal": "normal"},
    )


CHANGE = tool_call(
    "c1", "change_stat",
    {"target": "player", "stat": "charm", "delta": 3, "reason": "打扮"},
)


def _game(responses, context_window: int = 0, keep_turns: int = 6, compress_threshold: int = 0):
    pack = load_worldpack(PACK_PATH)
    state = GameState.from_pack(pack)
    client = FakeClient(responses)

    # 允许夹具用"异常实例"当成一条脚本响应：假客户端按真实 SDK 语义 raise 它，
    # 而不是把它当响应对象返回（否则会在读取 .choices 时炸成 AttributeError）。
    # 用子类包一层，保住 `self`（直接替换实例方法会丢掉绑定）。
    class _RaisingCompletions(type(client.chat.completions)):
        def create(self, **kwargs):  # noqa: ANN001
            if self.responses and isinstance(self.responses[0], BaseException):
                raise self.responses.pop(0)
            return super().create(**kwargs)

    client.chat.completions.__class__ = _RaisingCompletions
    llm = LLMClient(client, model="fake", tools=build_tools(pack.schedule))
    return Game(
        pack, state, llm,
        keep_turns=keep_turns,
        compress_threshold=compress_threshold,
        context_window=context_window,
    )


def _push_turns(game: Game, n: int, pad: int = 0) -> None:
    """直接压入 n 个"玩家回合"（不消耗假响应），用于构造可压缩的历史。"""
    for i in range(n):
        game.history.append({"role": "user", "origin": "player", "content": f"玩家第 {i} 轮"})
        game.history.append(
            {"role": "assistant", "origin": "model", "content": f"叙事第 {i} 轮" + "字" * pad}
        )


# ---------------------------------------------------------------------------
# 1. 错误分类：只认"输入过长"
# ---------------------------------------------------------------------------


def test_overflow_classifier_accepts_real_provider_wording():
    assert is_context_overflow(_OverflowError(OVERFLOW_MESSAGE))
    assert is_context_overflow(_OverflowError("maximum context length exceeded"))
    assert is_context_overflow(_OverflowError("输入过长，请缩短上下文"))


def test_overflow_classifier_rejects_other_failures():
    """限流/网络/配额错误**不得**被当成溢出——误判会去压缩有效历史。"""
    assert not is_context_overflow(_OverflowError("Rate limit reached for requests"))
    assert not is_context_overflow(TimeoutError("connection timed out"))
    assert not is_context_overflow(_OverflowError("Insufficient balance"))
    assert not is_context_overflow(ValueError("参数非法"))


# ---------------------------------------------------------------------------
# 2. 恢复路径：被拒 → 压缩 → 重试，效果照常提交
# ---------------------------------------------------------------------------


def test_overflow_is_recovered_by_compaction_and_retry():
    """核心契约：溢出不再熔断回滚，而是压缩后重试成功。"""
    game = _game(
        [
            _OverflowError(OVERFLOW_MESSAGE),  # 第一次请求被拒
            resp(msg(content="（增量摘要）玩家与沈清秋初识。")),  # 压缩调用
            resp(msg(tool_calls=[CHANGE, _submit("溢出后重试成功")])),  # 重试
        ],
        context_window=100_000,
        keep_turns=2,
    )
    _push_turns(game, 4)

    view = game._llm_round()

    assert view.narration == "溢出后重试成功", "溢出应被恢复，而不是熔断成保守回合"
    assert game.state.stats["charm"] == 13.0, "重试轮的工具效果必须提交（不是回滚）"
    assert any("剧情摘要" in str(m.get("content")) for m in game.history)
    assert game._overflow_retry_used is True


def test_overflow_retry_happens_only_once_then_melts_down():
    """压缩后仍被拒 → 只重试一次，随后走原有熔断口径（撤销 + 保守回合）。"""
    game = _game(
        [
            _OverflowError(OVERFLOW_MESSAGE),
            resp(msg(content="（增量摘要）……")),
            _OverflowError(OVERFLOW_MESSAGE),  # 压缩后仍被拒
        ],
        context_window=100_000,
        keep_turns=2,
    )
    _push_turns(game, 4)

    view = game._llm_round()

    assert "已跳过" in (view.narration or ""), "应退化为熔断兜底回合"
    assert game._meltdown_round is True


def test_plain_failure_still_melts_down_without_compaction():
    """非溢出错误（如协议连续失败）不得触发压缩重试——行为与改前一致。"""
    game = _game(
        [
            resp(msg(content="没有工具调用")),
            resp(msg(content="没有工具调用")),
            resp(msg(content="没有工具调用")),
        ],
        context_window=100_000,
        keep_turns=2,
    )
    _push_turns(game, 4)
    view = game._llm_round()
    assert "已跳过" in (view.narration or "")
    assert game._overflow_retry_used is False, "非溢出错误不该走压缩重试"


def test_overflow_without_compressible_history_still_melts_down():
    """压缩压不动（历史太短）→ 不硬试，交熔断兜底（不重复烧钱）。"""
    game = _game(
        [_OverflowError(OVERFLOW_MESSAGE), resp(msg(content="（增量摘要）……"))],
        context_window=100_000,
        keep_turns=6,
    )
    view = game._llm_round()  # 历史为空：find_turn_cut 返回 0 → 压缩 no-op
    assert "已跳过" in (view.narration or "")


# ---------------------------------------------------------------------------
# 3. 预检：在被拒之前就压缩
# ---------------------------------------------------------------------------


def test_precheck_compacts_before_request_when_budget_exceeded():
    """窗口很小时，超预算的历史应在**发出请求之前**被压缩。

    窗口取 20000：预算 = 20000 − max(4096, 2000) = 15904，正好让"输出预留"
    参与计算，同时避免窗口小于输出预留时预算被夹成 0（那会关闭预检，是另一条测试）。
    """
    game = _game(
        [
            resp(msg(content="（增量摘要）压缩后的剧情。")),
            resp(msg(tool_calls=[_submit("预检后正常叙事")])),
        ],
        context_window=20_000,
        keep_turns=2,
    )
    _push_turns(game, 5, pad=4000)  # 估算远超预算 15904

    assert game._context_budget() == 20_000 - 4096
    assert game._budget_exceeded(game._context_budget()), "夹具应处于超预算状态"
    before = len(game.history)
    view = game._llm_round()

    assert view.narration == "预检后正常叙事"
    assert len(game.history) < before, "预检应已压缩历史"
    assert any("剧情摘要" in str(m.get("content")) for m in game.history)


def test_precheck_disabled_when_window_unknown():
    """context_window=0（未声明）→ 溢出预检关闭，行为与改前一致。"""
    game = _game(
        [resp(msg(tool_calls=[_submit("正常叙事")]))],
        context_window=0,
        compress_threshold=0,
    )
    _push_turns(game, 8, pad=500)
    assert game._context_budget() == 0
    assert game._budget_exceeded(0) is False
    view = game._llm_round()
    assert view.narration == "正常叙事"


def test_context_budget_reserves_output_and_margin():
    """预算 = 窗口 − max(输出下限, 窗口×余量)，不得把整个窗口当输入预算。"""
    game = _game([], context_window=10_000)
    # 10%（1000）< 输出下限（4096）→ 取 4096
    assert game._context_budget() == 10_000 - 4096
    big = _game([], context_window=100_000)
    assert big._context_budget() == 100_000 - 10_000  # 10% > 4096 → 取 10%


def test_context_budget_clamps_to_zero_for_tiny_window():
    """窗口比输出预留还小 → 预算为 0（关闭预检而不是算出负数）。

    这种情况下"压缩到不超预算"没有意义（地板本身就超），只能靠 provider 报错走
    恢复路径，或由调用方换更大的窗口。
    """
    assert _game([], context_window=4_096)._context_budget() == 0
    assert _game([], context_window=100)._context_budget() == 0


# ---------------------------------------------------------------------------
# 4. 估算口径：不得低估
# ---------------------------------------------------------------------------


def test_estimate_never_shrinks_below_raw_estimate():
    """校准因子 <1 时也不下调估算——低估会让溢出预检形同虚设。"""
    from game_agent.game import estimate_context_tokens

    history = [{"role": "user", "content": "字" * 100}]
    assert estimate_context_tokens(history, 1.0) == 100
    assert estimate_context_tokens(history, 2.0) == 200
    assert estimate_context_tokens(history, 0.5) == 100


# ---------------------------------------------------------------------------
# 5. 配置接线：DEEPSEEK_CONTEXT_WINDOW → Settings → Game
# ---------------------------------------------------------------------------


def test_context_window_parser_accepts_valid_forms():
    from game_agent.config import _positive_int

    assert _positive_int("65536") == 65536
    assert _positive_int("  32768  ") == 32768
    assert _positive_int("65_536") == 65536  # 允许下划线分隔的写法


def test_context_window_parser_degrades_to_undeclared():
    """非法/非正值 → 0（= 未声明），不得让拼错的配置把游戏搞起不来。"""
    from game_agent.config import _positive_int

    for raw in ("", "   ", "abc", "32k", "-1", "0", "1.5", None):
        assert _positive_int(raw) == 0, raw


def test_settings_reads_context_window_from_env(tmp_path, monkeypatch):
    """load_settings 必须把 DEEPSEEK_CONTEXT_WINDOW 读进 Settings.context_window。

    （chdir 到 tmp_path：避免加载真实仓库 .env，保证断言只反映本轮注入的变量。）
    """
    from game_agent.config import load_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_CONTEXT_WINDOW", "32768")
    assert load_settings().context_window == 32768

    monkeypatch.setenv("DEEPSEEK_CONTEXT_WINDOW", "")
    assert load_settings().context_window == 0  # 留空 = 未声明


def test_settings_context_window_defaults_to_zero(tmp_path, monkeypatch):
    """不设该变量时默认 0 —— 既有部署行为逐字不变（预检关闭）。"""
    from game_agent.config import load_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_CONTEXT_WINDOW", raising=False)
    assert load_settings().context_window == 0


def test_cli_and_web_wire_context_window_from_settings():
    """接线守卫：CLI / Web 的 Game 构造必须把**解析后**的窗口传下去。

    这条防的是"配置项加了但没人用"——那是加了等于没加，且很难在真机上发现
    （预检静默关闭，只在长局里表现为"偶尔被 provider 拒一次"）。

    注意断言的是 `resolve_context_window(settings)`：窗口的正确来源是
    "显式配置 > 端点自报 max_model_len"，直接传 `settings.context_window` 会丢掉
    端点自报那一半（云端端点不报时才是 0，本地 vLLM 本可以自动拿到）。
    """
    import inspect

    from game_agent import cli, web

    for module, fn_name in ((cli, "_cmd_play"), (cli, "_cmd_mcp"), (web, "_make_game")):
        src = inspect.getsource(getattr(module, fn_name))
        assert "context_window=resolve_context_window(settings)" in src, f"{fn_name} 未接线"


def test_resolve_context_window_prefers_explicit_config():
    """显式配置优先于端点自报——使用者写下来的就是意图，不能被探测覆盖。"""
    from game_agent.config import Settings, resolve_context_window

    s = Settings(api_key="k", base_url="https://x", model="m", context_window=4096)
    assert resolve_context_window(s, probe=lambda *a, **k: 999_999) == 4096


def test_resolve_context_window_falls_back_to_endpoint():
    """未显式配置时用端点自报值（复用 fingerprint 的 max_model_len）。"""
    from game_agent.config import Settings, resolve_context_window

    s = Settings(api_key="k", base_url="http://127.0.0.1:8000/v1", model="local-14b")
    assert resolve_context_window(s, probe=lambda *a, **k: 32768) == 32768


def test_resolve_context_window_soft_fails_to_zero():
    """端点不报 / 探测抛错 → 0（预检关闭），绝不猜一个窗口出来。"""
    from game_agent.config import Settings, resolve_context_window

    s = Settings(api_key="k", base_url="https://api.deepseek.com", model="deepseek-v4-flash")
    assert resolve_context_window(s, probe=lambda *a, **k: 0) == 0

    def boom(*_a, **_k):
        raise TimeoutError("connect timeout")

    assert resolve_context_window(s, probe=boom) == 0


def test_endpoint_context_window_is_cached(monkeypatch):
    """探测结果按 (base_url, model) 缓存：Web 每请求都会建 Game，不能每请求探测。

    两种"端点答了"的结果都要缓存：① 带 max_model_len；② 答了但没这个字段
    （云端 DeepSeek 就是这种——不缓存会让每回合白花一次网络往返）。
    只有**探测抛错**才不缓存（端点稍后可达要能自愈）。
    """
    from game_agent import endpoint
    from game_agent.config import Settings

    calls: list[str] = []

    def fake_list_models(base_url, api_key, timeout):  # noqa: ANN001
        calls.append(base_url)
        if "fail" in base_url:
            raise TimeoutError("nope")
        if "nofield" in base_url:
            return [{"id": "m", "served_by": "deepseek", "model_root": None, "max_model_len": None}]
        return [{"id": "m", "served_by": "vllm", "model_root": "r", "max_model_len": 32768}]

    monkeypatch.setattr(endpoint, "_list_models", fake_list_models)
    monkeypatch.setattr(endpoint, "_DWINDOW_CACHE", {})

    ok = Settings(api_key="k", base_url="http://ok/v1", model="m")
    assert endpoint.context_window_from_endpoint(ok) == 32768
    assert endpoint.context_window_from_endpoint(ok) == 32768
    assert len(calls) == 1, "第二次应命中缓存"

    nofield = Settings(api_key="k", base_url="http://nofield/v1", model="m")
    assert endpoint.context_window_from_endpoint(nofield) == 0
    assert endpoint.context_window_from_endpoint(nofield) == 0
    assert calls.count("http://nofield/v1") == 1, "端点答了（只是没这个字段）→ 也应缓存"

    bad = Settings(api_key="k", base_url="http://fail/v1", model="m")
    assert endpoint.context_window_from_endpoint(bad) == 0
    assert endpoint.context_window_from_endpoint(bad) == 0
    assert calls.count("http://fail/v1") == 2, "探测失败不得缓存（端点稍后可达要能自愈）"
