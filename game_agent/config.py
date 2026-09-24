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
    )
