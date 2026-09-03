"""配置加载：从 .env 或环境变量读取 LLM 接入设置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# DeepSeek 默认值：OpenAI 兼容端点 + 快速模型（可用 deepseek-reasoner 覆盖）
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)


def load_settings(env_path: str | Path | None = None) -> Settings:
    """加载 .env（若存在），返回 Settings。API Key 缺失时 has_api_key 为 False。"""
    if env_path is not None:
        load_dotenv(env_path)
    else:
        load_dotenv()  # 从工作目录找 .env
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    base_url = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip()
    model = os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip()
    return Settings(api_key=api_key, base_url=base_url, model=model)
