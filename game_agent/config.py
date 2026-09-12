"""配置加载：从 .env 或环境变量读取 LLM 接入设置。

C1（P1）模型分层：主生成 / Judge / 压缩可各配一个模型，
judge_model / compress_model 为空时回退主模型（design.md §14.2）。
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
    no_thinking_side_channel: bool = False  # 侧信道关思考（DEEPSEEK_DISABLE_THINKING=1）

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def model_for(self, purpose: str) -> str:
        """按用途取有效模型：judge/compress 有专属配置则用之，否则主模型。"""
        if purpose == "judge" and self.judge_model:
            return self.judge_model
        if purpose == "compress" and self.compress_model:
            return self.compress_model
        return self.model


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
    no_thinking = os.environ.get("DEEPSEEK_DISABLE_THINKING", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    return Settings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        judge_model=judge_model,
        compress_model=compress_model,
        no_thinking_side_channel=no_thinking,
    )
