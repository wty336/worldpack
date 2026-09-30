"""配置加载：从 .env 或环境变量读取 LLM 接入设置。

C1（P1）模型分层：主生成 / Judge / 压缩可各配一个模型，
judge_model / compress_model 为空时回退主模型（design.md §14.2）。
B3（Track B）：extract / reflect / dedup 三个侧信道同样支持专属模型路由
（plan-local-14b §8.2 的前置：混跑本地小模型只差环境变量）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# DeepSeek V4 默认值：OpenAI 兼容端点 + 快速模型（关键节点可换 deepseek-v4-pro）
# 注：deepseek-chat / deepseek-reasoner 已于 2026-07-24 停用
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    judge_model: str = ""  # C1：Judge 用模型（空 = 回退主模型）
    compress_model: str = ""  # C1：压缩/摘要用模型（空 = 回退主模型）
    extract_model: str = ""  # B3（Track B）：事实提取用模型（空 = 回退主模型）
    reflect_model: str = ""  # B3（Track B）：关系洞察用模型（空 = 回退主模型）
    dedup_model: str = ""  # B3（Track B）：语义去重用模型（空 = 回退主模型）
    no_thinking_side_channel: bool = False  # 侧信道关思考（DEEPSEEK_DISABLE_THINKING=1）
    trace_path: str = ""  # B1（Track B）：LLM 调用 trace 落盘路径（空 = 关闭）
    # J 系列：模型上下文窗（token；0 = 未声明 → 溢出预检关闭，只保留被拒后的恢复）。
    # 放在末尾且带默认值：Settings 是 frozen dataclass，既有测试/调用方全用关键字
    # 构造，追加字段不破坏位置参数顺序。
    context_window: int = 0

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def model_for(self, purpose: str) -> str:
        """按用途取有效模型：各 purpose 有专属配置则用之，否则主模型。"""
        dedicated = {
            "judge": self.judge_model,
            "compress": self.compress_model,
            "extract": self.extract_model,
            "reflect": self.reflect_model,
            "dedup": self.dedup_model,
        }.get(purpose)
        return dedicated or self.model


def _positive_int(raw: str) -> int:
    """把环境变量解析成正整数；空/非法/非正 → 0（= 未声明）。

    为什么非法值不报错、而是退回"未声明"：这是**可选**的优化开关，不是必需配置。
    一个拼错的数字不该让整个游戏起不来——退化为"预检关闭"即可（被拒后的恢复路径
    仍然生效）。允许 ``65536`` 与 ``65_536`` 两种写法。
    """
    text = (raw or "").strip().replace("_", "")
    if not text:
        return 0
    try:
        value = int(text)
    except ValueError:
        return 0
    return value if value > 0 else 0


def resolve_context_window(settings: Settings, purpose: str = "turn", probe=None) -> int:
    """解析主回合的上下文窗（token）；未知返回 0（= 溢出预检关闭）。

    优先级：**显式配置 > 端点自报 > 未知**。

    - 显式配置（``DEEPSEEK_CONTEXT_WINDOW``）永远最高：使用者写下来的就是意图，
      不能被探测结果覆盖；
    - 未显式配置时问端点自报的 ``max_model_len``——本地 vLLM 等端点会报，
      而这正是"换了量化/``max_model_len`` 之后要重新对齐"的那个数。探测复用
      `endpoint.fingerprint`（已有能力，按 (base_url, model) 缓存）；
    - 都不行则返回 0 → 预检关闭，仍保留被拒后的压缩重试恢复（fail-soft）。
    """
    if settings.context_window > 0:
        return settings.context_window
    if probe is None:
        from .endpoint import context_window_from_endpoint

        probe = context_window_from_endpoint
    try:
        probed = int(probe(settings, purpose) or 0)
    except Exception:  # noqa: BLE001 —— 观测层探测失败不得影响启动
        return 0
    return probed if probed > 0 else 0


def load_settings(env_path: str | Path | None = None) -> Settings:
    """加载 .env（若存在），返回 Settings。API Key 缺失时 has_api_key 为 False。"""
    if env_path is not None:
        load_dotenv(env_path)
    else:
        load_dotenv()  # 从工作目录找 .env
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    base_url = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip()
    model = os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip()
    judge_model = os.environ.get("DEEPSEEK_JUDGE_MODEL", "").strip()
    compress_model = os.environ.get("DEEPSEEK_COMPRESS_MODEL", "").strip()
    extract_model = os.environ.get("DEEPSEEK_EXTRACT_MODEL", "").strip()
    reflect_model = os.environ.get("DEEPSEEK_REFLECT_MODEL", "").strip()
    dedup_model = os.environ.get("DEEPSEEK_DEDUP_MODEL", "").strip()
    no_thinking = os.environ.get("DEEPSEEK_DISABLE_THINKING", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    trace_path = os.environ.get("GAME_AGENT_TRACE", "").strip()
    context_window = _positive_int(os.environ.get("DEEPSEEK_CONTEXT_WINDOW", ""))
    return Settings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        judge_model=judge_model,
        compress_model=compress_model,
        extract_model=extract_model,
        reflect_model=reflect_model,
        dedup_model=dedup_model,
        no_thinking_side_channel=no_thinking,
        trace_path=trace_path,
        context_window=context_window,
    )
