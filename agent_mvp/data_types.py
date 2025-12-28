from dataclasses import dataclass, field
from typing import List, Dict, Any


@dataclass
class FactItem:
    """单条硬核事实。"""

    title: str
    summary: str
    sources: List[str] = field(default_factory=list)
    source_engine: str = ""  # google / bing / baidu 等
    source_site: str = ""  # wikipedia / baike 等
    source_link: str = ""
    translated_from: str = ""  # 如果是翻译文本，记录原始语言


@dataclass
class ResearchResult:
    """Step 1 输出：结构化硬核事实清单。"""

    topic: str
    outline: List[str]
    facts: List[FactItem]


@dataclass
class ScriptSection:
    """文案中的单个部分（带占位符）。"""

    heading: str
    body: str


@dataclass
class ScriptDraft:
    """Step 2 输出：带占位符的 B 站风格脚本。"""

    topic: str
    title: str
    sections: List[ScriptSection]
    placeholders: List[str]  # e.g. ["[素材位置：麻生矿业旧址纪录片]"]


@dataclass
class MaterialItem:
    """Step 3 输出中的单条素材。"""

    placeholder: str
    keyword: str
    title: str
    reason: str
    platform: str = "bilibili"  # bilibili / youtube / mock
    url: str  # 网页地址
    embed_url: str = ""  # 便于前端直接嵌入
    play_url: str = ""  # 可播放的直链 URL（mp4/flv），如果为空则回退到网页地址
    animation: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MaterialMatchResult:
    """素材匹配整体结果。"""

    topic: str
    materials: List[MaterialItem]


@dataclass
class FinalScript:
    """Step 4 输出：成品视频脚本。"""

    topic: str
    title: str
    content: str
    materials: List[MaterialItem]


