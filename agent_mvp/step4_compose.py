"""
Step 4: 最终合成

将文案与素材链接合并，输出成品脚本（文本），并自动进行内容安全过滤。
"""

import os
import re
import json
from pathlib import Path
from typing import List, Tuple, Set

from .data_types import ScriptDraft, MaterialMatchResult, FinalScript, MaterialItem
from .config import CONFIG


# ==================== 内容安全过滤功能 ====================

def _get_sensitive_words_file() -> Path:
    """获取敏感词 JSON 文件路径"""
    current_dir = Path(__file__).parent
    project_root = current_dir.parent
    return project_root / "config" / "sensitive_words.json"


def _get_animation_manifest_file() -> Path:
    """获取动画预设清单路径"""
    base = CONFIG.animation.manifest_path
    if base.is_absolute():
        return base
    current_dir = Path(__file__).parent.parent
    return current_dir / base


def _load_animation_presets() -> List[dict]:
    """加载动画预设列表"""
    path = _get_animation_manifest_file()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "presets" in data:
            presets = data.get("presets", [])
        elif isinstance(data, list):
            presets = data
        else:
            presets = []
        if not isinstance(presets, list):
            return []
        return presets
    except Exception:
        return []


def _attach_animation(materials: List[MaterialItem], enable_animations: bool) -> List[MaterialItem]:
    """为素材附加动画元信息（简单轮询分配）。"""
    if not enable_animations:
        return materials
    presets = _load_animation_presets()
    if not presets:
        return materials
    enriched: List[MaterialItem] = []
    for idx, m in enumerate(materials):
        if m.animation:
            enriched.append(m)
            continue
        preset = presets[idx % len(presets)]
        m.animation = preset if isinstance(preset, dict) else {}
        enriched.append(m)
    return enriched


def _load_sensitive_words() -> Set[str]:
    """加载敏感词列表（仅支持 JSON 格式）
    
    支持的 JSON 格式：
    1. 简单数组：`["敏感词1", "敏感词2"]`
    2. 对象格式：`{"words": ["敏感词1", "敏感词2"]}`
    3. 分类格式：`{"categories": {"政治敏感": ["词1"], "历史不当": ["词2"]}}`
    4. 扁平结构：`{"政治": ["词1"], "历史": ["词2"]}`
    
    Returns:
        敏感词集合（去重、去空、转小写）
    """
    words_file = _get_sensitive_words_file()
    
    if not words_file.exists():
        # 如果文件不存在，返回空集合（不阻止运行）
        return set()
    
    words: Set[str] = set()
    
    try:
        with open(words_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        # 支持多种 JSON 格式
        if isinstance(data, dict):
            # 格式1: {"words": ["词1", "词2"]}
            if "words" in data and isinstance(data["words"], list):
                words.update(str(w).lower().strip() for w in data["words"] if w)
            
            # 格式2: {"categories": {"政治": ["词1", "词2"], "历史": [...]}}
            if "categories" in data and isinstance(data["categories"], dict):
                for category_words in data["categories"].values():
                    if isinstance(category_words, list):
                        words.update(str(w).lower().strip() for w in category_words if w)
            
            # 格式3: 扁平结构 {"政治": ["词1"], "历史": ["词2"]}
            for key, value in data.items():
                if key not in ["words", "categories", "lastUpdateDate", "description", "version", "notes"] and isinstance(value, list):
                    words.update(str(w).lower().strip() for w in value if w)
        
        # 格式4: 直接是数组 ["词1", "词2"]
        elif isinstance(data, list):
            words.update(str(w).lower().strip() for w in data if w)
        
    except json.JSONDecodeError as e:
        print(f"警告：敏感词 JSON 文件格式错误: {e}")
        return set()
    except Exception as e:
        print(f"警告：加载敏感词文件失败: {e}")
        return set()
    
    # 过滤空字符串
    words = {w for w in words if w}
    return words


# 缓存敏感词列表（避免重复读取文件）
_sensitive_words_cache: Set[str] | None = None


def _get_sensitive_words() -> Set[str]:
    """获取敏感词列表（带缓存）"""
    global _sensitive_words_cache
    if _sensitive_words_cache is None:
        _sensitive_words_cache = _load_sensitive_words()
    return _sensitive_words_cache


def _check_sensitive_words(text: str) -> List[Tuple[str, int]]:
    """检查文本中的敏感词
    
    Args:
        text: 待检查的文本
        
    Returns:
        发现的敏感词列表，每个元素为 (敏感词, 出现位置) 的元组
    """
    sensitive_words = _get_sensitive_words()
    if not sensitive_words:
        return []
    
    text_lower = text.lower()
    found_words: List[Tuple[str, int]] = []
    
    for word in sensitive_words:
        # 使用正则表达式查找（支持中英文边界）
        pattern = re.escape(word)
        # 匹配单词边界（中文、英文、数字、标点）
        pattern = rf"(?<![a-zA-Z0-9\u4e00-\u9fa5]){pattern}(?![a-zA-Z0-9\u4e00-\u9fa5])"
        
        matches = list(re.finditer(pattern, text_lower))
        for match in matches:
            found_words.append((word, match.start()))
    
    # 按位置排序
    found_words.sort(key=lambda x: x[1])
    return found_words


def _filter_sensitive_content(final_script: FinalScript, 
                              replacement: str = "***",
                              strict_mode: bool = False) -> Tuple[FinalScript, List[str]]:
    """过滤 FinalScript 中的敏感内容
    
    Args:
        final_script: 待过滤的最终脚本
        replacement: 敏感词替换字符串（默认 "***"）
        strict_mode: 严格模式，如果发现敏感词是否抛出异常（默认 False，只替换）
        
    Returns:
        (过滤后的 FinalScript, 发现的敏感词列表)
    """
    # 检查标题
    title_warnings: List[str] = []
    filtered_title = final_script.title
    if final_script.title:
        title_matches = _check_sensitive_words(final_script.title)
        if title_matches:
            for word, pos in title_matches:
                title_warnings.append(f"标题中发现敏感词: '{word}' (位置: {pos})")
                # 替换敏感词
                filtered_title = re.sub(
                    re.escape(word),
                    replacement,
                    filtered_title,
                    flags=re.IGNORECASE
                )
    
    # 检查正文内容
    content_warnings: List[str] = []
    filtered_content = final_script.content
    if final_script.content:
        content_matches = _check_sensitive_words(final_script.content)
        if content_matches:
            for word, pos in content_matches:
                content_warnings.append(f"正文中发现敏感词: '{word}' (位置: {pos})")
                # 替换敏感词
                filtered_content = re.sub(
                    re.escape(word),
                    replacement,
                    filtered_content,
                    flags=re.IGNORECASE
                )
    
    # 检查素材标题和理由
    material_warnings: List[str] = []
    filtered_materials = []
    for m in final_script.materials:
        material_has_issue = False
        
        # 检查素材标题
        if m.title:
            title_matches = _check_sensitive_words(m.title)
            if title_matches:
                for word, pos in title_matches:
                    material_warnings.append(f"素材标题中发现敏感词: '{word}' (素材: {m.title[:30]}...)")
                    material_has_issue = True
        
        # 检查素材理由
        if m.reason:
            reason_matches = _check_sensitive_words(m.reason)
            if reason_matches:
                for word, pos in reason_matches:
                    material_warnings.append(f"素材理由中发现敏感词: '{word}' (素材: {m.title[:30]}...)")
                    material_has_issue = True
        
        # 如果素材有问题，可以选择替换或保留（这里选择保留，只记录警告）
        filtered_materials.append(m)
    
    all_warnings = title_warnings + content_warnings + material_warnings
    
    # 严格模式：如果发现敏感词，抛出异常
    if strict_mode and all_warnings:
        raise ValueError(
            f"检测到敏感内容，无法通过审核。发现的敏感词：\n" + 
            "\n".join(all_warnings)
        )
    
    # 创建过滤后的 FinalScript
    filtered_script = FinalScript(
        topic=final_script.topic,
        title=filtered_title,
        content=filtered_content,
        materials=filtered_materials,
    )
    
    return filtered_script, all_warnings


# ==================== Step 4 主功能 ====================


def compose_final_script(script: ScriptDraft, materials: MaterialMatchResult, 
                         enable_safety_filter: bool = True,
                         strict_mode: bool = False,
                         enable_animations: bool = True) -> FinalScript:
    """合成最终脚本并自动进行敏感词过滤
    
    Args:
        script: 脚本草稿
        materials: 素材匹配结果
        enable_safety_filter: 是否启用内容安全过滤（默认 True）
        strict_mode: 严格模式，发现敏感词时是否抛出异常（默认 False，只替换）
        enable_animations: 是否附加动画提示
    
    Returns:
        过滤后的 FinalScript（如果启用过滤）
    """
    # 附加动画提示
    material_items = _attach_animation(materials.materials, enable_animations)

    # 先把素材按占位符组织成一个映射，方便渲染
    placeholder_to_materials = {}
    for m in material_items:
        placeholder_to_materials.setdefault(m.placeholder, []).append(m)

    # 拼接正文
    lines = [f"【标题】{script.title}", "", "【正文】", ""]

    for sec in script.sections:
        lines.append(f"—— {sec.heading} ——")

        body_lines = sec.body.splitlines()
        for line in body_lines:
            stripped = line.strip()
            if stripped in placeholder_to_materials:
                # 插入素材推荐
                lines.append("")  # 空行分隔
                lines.append(f"{stripped}")
                for m in placeholder_to_materials[stripped]:
                    lines.append(f"  - 推荐素材：{m.title}")
                    lines.append(f"    理由：{m.reason}")
                    lines.append(f"    平台：{m.platform}")
                    lines.append(f"    链接：{m.url}")
                    if m.embed_url:
                        lines.append(f"    嵌入：{m.embed_url}")
                    if m.animation:
                        anim_name = m.animation.get("name") or m.animation.get("id") or "推荐动画"
                        lines.append(f"    动画：{anim_name}")
                lines.append("")  # 素材后再空一行
            else:
                lines.append(line)

        lines.append("")  # 每个 section 后空一行

    content = "\n".join(lines)

    # 创建初始的 FinalScript
    final_script = FinalScript(
        topic=script.topic,
        title=script.title,
        content=content,
        materials=material_items,
    )
    
    # 自动进行内容安全过滤
    if enable_safety_filter:
        filtered_script, warnings = _filter_sensitive_content(
            final_script, 
            replacement="***", 
            strict_mode=strict_mode
        )
        # 将警告信息存储在 extra 字段中（如果需要的话）
        # 这里直接返回过滤后的脚本，警告信息可以在调用处处理
        return filtered_script
    
    return final_script


# ==================== 导出函数（供外部使用） ====================

def check_sensitive_words(text: str) -> List[Tuple[str, int]]:
    """检查文本中的敏感词（供外部调用）
    
    Args:
        text: 待检查的文本
        
    Returns:
        发现的敏感词列表，每个元素为 (敏感词, 出现位置) 的元组
    """
    return _check_sensitive_words(text)
