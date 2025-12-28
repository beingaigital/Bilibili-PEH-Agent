"""
配置与常量。

真实环境下，这里可以放入：
- Qwen / 其他大模型的 API Key 与模型名称
- Bilibili 开放平台的访问凭证
当前 MVP 仅做结构示例，不会发起真实网络请求。
"""

from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class LLMConfig:
    provider: str = "qwen"  # 示例用
    model_research: str = "qwen-plus"
    model_writer: str = "qwen-plus"
    # 从 .env / 环境变量中读取 DashScope / Qwen 的 API Key
    api_key_env: str = "DASHSCOPE_API_KEY"

    @property
    def api_key(self) -> str | None:  # Python 3.10+ 可去掉 | None 写成 Optional[str]
        return os.getenv(self.api_key_env)


@dataclass
class BilibiliConfig:
    api_base: str = "https://api.bilibili.com"  # 占位


@dataclass
class YouTubeConfig:
    """YouTube Data API 配置（可选）。"""

    api_key_env: str = "YOUTUBE_API_KEY"

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env)


@dataclass
class SearchConfig:
    """搜索相关配置。"""

    prefer_wikipedia: bool = True
    google_timeout: int = 12


@dataclass
class AnimationConfig:
    """动画插入相关配置。"""

    manifest_path: Path = Path("config/animations.json")
    assets_dir: Path = Path("assets/animations")
    enable_by_default: bool = True


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    bilibili: BilibiliConfig = field(default_factory=BilibiliConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    animation: AnimationConfig = field(default_factory=AnimationConfig)


CONFIG = AppConfig()
# 尝试自动加载项目根目录下的 .env
_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_PATH = _ROOT / ".env"
if _DOTENV_PATH.exists():
    load_dotenv(_DOTENV_PATH)


