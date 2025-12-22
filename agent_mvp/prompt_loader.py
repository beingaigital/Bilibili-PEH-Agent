"""
提示词加载器

统一管理所有模块的提示词，从 prompt 目录下的 YAML 文件读取。
"""

import os
from pathlib import Path
from typing import Optional, Dict

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


def _get_prompt_dir() -> Path:
    """获取 prompt 目录的路径"""
    # 获取当前文件所在目录（agent_mvp）
    current_dir = Path(__file__).parent
    # prompt 目录在项目根目录下的 config 目录中
    project_root = current_dir.parent
    return project_root / "config" / "prompt"


def load_prompt_yaml(filename: str) -> Dict[str, str]:
    """加载 YAML 格式的提示词文件
    
    Args:
        filename: 文件名（例如 "step1_keywords.yaml"）
    
    Returns:
        包含 'system' 和 'user' 键的字典
    
    Raises:
        FileNotFoundError: 如果文件不存在
        ValueError: 如果 YAML 格式不正确或缺少必要字段
    """
    if not _HAS_YAML:
        raise ImportError("需要安装 PyYAML: pip install pyyaml")
    
    prompt_dir = _get_prompt_dir()
    file_path = prompt_dir / filename
    
    if not file_path.exists():
        raise FileNotFoundError(f"提示词文件不存在: {file_path}")
    
    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    if not isinstance(data, dict):
        raise ValueError(f"YAML 文件格式错误: {filename}，应为字典格式")
    
    if "system" not in data or "user" not in data:
        raise ValueError(f"YAML 文件缺少必要字段: {filename}，需要 'system' 和 'user' 字段")
    
    return {
        "system": str(data["system"]).rstrip(),
        "user": str(data["user"]).rstrip()
    }


def load_prompt_system(filename: str) -> str:
    """加载 System Message
    
    Args:
        filename: YAML 文件名（例如 "step1_keywords.yaml"）
    
    Returns:
        System Message 内容
    """
    data = load_prompt_yaml(filename)
    return data["system"]


def load_prompt_user(filename: str) -> str:
    """加载 User Prompt 模板
    
    Args:
        filename: YAML 文件名（例如 "step1_keywords.yaml"）
    
    Returns:
        User Prompt 模板内容
    """
    data = load_prompt_yaml(filename)
    return data["user"]


def load_prompt(filename: str) -> str:
    """加载提示词文件（兼容旧版本，支持 .txt 和 .yaml）
    
    如果文件是 .yaml 格式，返回 user prompt。
    如果文件是 .txt 格式，返回文件内容。
    
    Args:
        filename: 文件名（例如 "step1_keywords.yaml" 或 "step1_keywords.txt"）
    
    Returns:
        提示词内容（去除末尾空白）
    
    Raises:
        FileNotFoundError: 如果文件不存在
    """
    prompt_dir = _get_prompt_dir()
    file_path = prompt_dir / filename
    
    if not file_path.exists():
        raise FileNotFoundError(f"提示词文件不存在: {file_path}")
    
    # 如果是 YAML 文件，返回 user prompt
    if filename.endswith(".yaml") or filename.endswith(".yml"):
        if not _HAS_YAML:
            raise ImportError("需要安装 PyYAML: pip install pyyaml")
        data = load_prompt_yaml(filename)
        return data["user"]
    
    # 否则按文本文件读取（兼容旧版本）
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    return content.rstrip()


def format_prompt(template: str, **kwargs) -> str:
    """格式化提示词模板
    
    Args:
        template: 提示词模板（支持 {variable} 占位符）
        **kwargs: 要替换的变量
    
    Returns:
        格式化后的提示词
    """
    return template.format(**kwargs)

