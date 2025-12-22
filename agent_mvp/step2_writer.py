"""
Step 2: 金牌编剧

真实环境：
- 使用大模型（这里接入通义千问 Qwen-plus）将硬核事实改写为 B 站风格文案
- 自动在合适位置插入素材占位符

当前实现：
- 优先调用 Qwen-plus 生成完整脚本
- 若未配置 API Key，则回退到本地模板规则
"""

import re
from typing import List

from .data_types import ResearchResult, ScriptDraft, ScriptSection
from .config import CONFIG
from .prompt_loader import load_prompt, load_prompt_user, load_prompt_system, format_prompt

try:
    # 使用阿里云百炼的 OpenAI 兼容接口
    from openai import OpenAI  # type: ignore

    _HAS_OPENAI = True
except Exception:  # pragma: no cover - 仅用于环境检测
    OpenAI = None  # type: ignore
    _HAS_OPENAI = False


def _build_intro_section(research: ResearchResult) -> ScriptSection:
    topic = research.topic
    body = (
        f"各位观众老爷们好，今天咱们来聊聊「{topic}」。\n"
        "别看标题听着严肃，实际上这背后既有资本操作，又有历史阴影，"
        "还能顺带吐槽一下当代政客的迷之发言。"
    )
    return ScriptSection(heading="开场", body=body)


def _build_body_sections(research: ResearchResult) -> List[ScriptSection]:
    sections: List[ScriptSection] = []

    for idx, fact in enumerate(research.facts, start=1):
        heading = f"第 {idx} 部分：{fact.title}"
        body_lines = [
            f"{fact.summary}",
            "",
            "这里如果按 B 站口味展开，大概会是：先用一两句梗点把观众抓住，",
            "接着用时间线串起关键事件，中间穿插一两句阴阳怪气，最后再来个升华小结。",
            "",
            "[素材占位：与本段内容相关的历史纪录片或新闻画面]",
        ]
        sections.append(ScriptSection(heading=heading, body="\n".join(body_lines)))

    return sections


def _build_outro_section(research: ResearchResult) -> ScriptSection:
    body = (
        "好了，以上就是本期关于这段历史的“硬核八卦”。\n"
        "如果你看完觉得有点后背发凉，那说明这段历史离我们并不遥远——\n"
        "历史从来不是博物馆里的展品，而是会在当下政经新闻里，悄悄换个马甲继续出现。"
        "\n\n[素材占位：主角近期相关发言或新闻片段]"
    )
    return ScriptSection(heading="结尾", body=body)


def extract_placeholders(sections: List[ScriptSection]) -> List[str]:
    """提取脚本中的所有占位符（支持多种格式）。
    
    支持的格式：
    - [素材占位：...]
    - [画面：...]
    - [画面描述：...]
    """
    placeholders: List[str] = []
    # 正则表达式：匹配 [画面：...] 或 [素材占位：...] 等格式
    placeholder_pattern = re.compile(r'\[(?:画面|素材占位|画面描述)[^：:]*[：:]([^\]]+)\]')
    
    for sec in sections:
        for line in sec.body.splitlines():
            stripped = line.strip()
            # 方法1：使用正则表达式提取
            matches = placeholder_pattern.findall(stripped)
            if matches:
                for match in matches:
                    # 重构完整的占位符
                    full_match = placeholder_pattern.search(stripped)
                    if full_match:
                        placeholders.append(full_match.group(0))
            # 方法2：兼容旧格式（简单字符串匹配）
            elif "[素材占位" in stripped or "[画面" in stripped:
                # 提取完整的占位符（从 [ 到 ]）
                start = stripped.find("[")
                end = stripped.find("]", start)
                if start != -1 and end != -1:
                    placeholder = stripped[start:end + 1]
                    if placeholder not in placeholders:
                        placeholders.append(placeholder)
    
    # 去重并保持顺序
    seen = set()
    unique_placeholders = []
    for p in placeholders:
        if p not in seen:
            seen.add(p)
            unique_placeholders.append(p)
    
    return unique_placeholders


def _extract_title_from_body(body: str, default_topic: str, debug: bool = False) -> str:
    """从脚本内容中提取标题
    
    优先从【标题】标记中提取，如果没有则生成通用标题
    
    Args:
        body: 脚本正文内容
        default_topic: 默认主题（用于生成标题）
        debug: 是否输出调试信息
    
    Returns:
        提取或生成的标题
    """
    # 尝试从【标题】标记中提取
    title_pattern = re.compile(r'【标题】\s*(.+?)(?:\n|$)')
    match = title_pattern.search(body)
    if match:
        title = match.group(1).strip()
        # 清理可能的格式标记
        title = title.replace("【", "").replace("】", "").strip()
        if title and len(title) > 5:  # 确保标题有意义
            if debug:
                print(f"  ✅ 从脚本中提取标题: {title}")
            return title
    
    # 如果没有找到标题标记，生成通用标题
    # 根据主题类型选择合适的标题格式
    if "黑历史" in default_topic or "丑闻" in default_topic or "内幕" in default_topic:
        title = f"关于「{default_topic}」的一段深度复盘"
    elif "历程" in default_topic or "发展" in default_topic or "历史" in default_topic:
        title = f"「{default_topic}」：一段不为人知的故事"
    elif "人物" in default_topic or "家族" in default_topic:
        title = f"「{default_topic}」：背后的真相"
    else:
        # 通用格式：去掉"黑历史"等不当词汇
        title = f"「{default_topic}」：深度解析"
    
    if debug:
        print(f"  ℹ️ 使用生成的标题: {title}")
    return title


def _parse_script_into_sections(body: str) -> List[ScriptSection]:
    """将 Qwen 生成的脚本内容解析为多个 sections（使用正则表达式增强兼容性）。
    
    识别多种章节标记格式：
    - ## 标题、### 标题（Markdown 格式）
    - 1. 标题、一、标题（编号格式）
    - 【开场】、【结尾】等标记
    - --- 分隔符
    """
    sections: List[ScriptSection] = []
    lines = body.splitlines()
    
    current_heading = "正文"
    current_body_lines: List[str] = []
    
    # 正则表达式：匹配 Markdown 标题（##、### 等）
    markdown_heading_pattern = re.compile(r'^#{1,6}\s+(.+)$')
    # 正则表达式：匹配编号标题（1. 标题、一、标题等）
    numbered_heading_pattern = re.compile(r'^[\d一二三四五六七八九十]+[\.、]\s*(.+)$')
    # 正则表达式：匹配【标题】格式
    bracket_heading_pattern = re.compile(r'^【([^】]+)】')
    # 正则表达式：匹配【标题】标记（用于提取视频标题，不作为章节）
    title_pattern = re.compile(r'^【标题】\s*(.+?)(?:\s*$|$)')
    
    for line in lines:
        stripped = line.strip()
        
        # 0. 跳过【标题】标记行（标题已在外部提取）
        if title_pattern.match(stripped):
            continue
        
        # 1. 识别 Markdown 标题（##、### 等）
        match = markdown_heading_pattern.match(stripped)
        if match:
            if current_body_lines:
                sections.append(ScriptSection(
                    heading=current_heading,
                    body="\n".join(current_body_lines).strip()
                ))
            current_heading = match.group(1).strip()
            current_body_lines = []
            continue
        
        # 2. 识别编号标题（1. 标题、一、标题等）
        match = numbered_heading_pattern.match(stripped)
        if match:
            if current_body_lines:
                sections.append(ScriptSection(
                    heading=current_heading,
                    body="\n".join(current_body_lines).strip()
                ))
            current_heading = match.group(1).strip()
            current_body_lines = []
            continue
        
        # 3. 识别【标题】格式（可能包含冒号）
        match = bracket_heading_pattern.match(stripped)
        if match:
            if current_body_lines:
                sections.append(ScriptSection(
                    heading=current_heading,
                    body="\n".join(current_body_lines).strip()
                ))
            heading_text = match.group(1).strip()
            # 去掉冒号后的内容
            if "：" in heading_text or ":" in heading_text:
                heading_text = heading_text.split("：")[0].split(":")[0].strip()
            current_heading = heading_text or "正文"
            current_body_lines = []
            # 如果标记后还有内容，也加入当前 section
            remaining = stripped[match.end():].strip()
            if remaining:
                current_body_lines.append(remaining)
            continue
        
        # 4. 识别分隔符：---、===、***（至少 3 个字符）
        if re.match(r'^[-=*]{3,}$', stripped):
            if current_body_lines:
                sections.append(ScriptSection(
                    heading=current_heading,
                    body="\n".join(current_body_lines).strip()
                ))
                current_heading = "正文"
                current_body_lines = []
            continue
        
        # 普通内容行
        current_body_lines.append(line)
    
    # 保存最后一个 section
    if current_body_lines:
        sections.append(ScriptSection(
            heading=current_heading,
            body="\n".join(current_body_lines).strip()
        ))
    
    # 如果没有解析出多个 sections，就把整个内容作为一个 section
    if not sections:
        sections.append(ScriptSection(heading="正文", body=body))
    
    return sections


def _build_prompt_for_qwen(research: ResearchResult) -> str:
    """根据搜研结果构造发给 Qwen-plus 的提示词（B站硬核风格）。"""
    
    try:
        prompt_template = load_prompt_user("step2_writer.yaml")
    except (FileNotFoundError, ImportError):
        # 回退到硬编码提示词（兼容性）
        parts: List[str] = []
        parts.append(
            "你是一位 B 站政经史领域的【金牌编剧】，擅长制作硬核、有梗、节奏感强的长视频。"
            "现在要根据「硬核事实清单」写一份完整的视频脚本。"
        )
        
        parts.append("\n【核心要求】")
        parts.append("1. **黄金前 3 秒（Cold Open）**：")
        parts.append("   必须用最炸裂的事实或暴论开场，直接抓住观众，不要慢吞吞的「各位好」。")
        parts.append("   例如：「你有没有想过，一个靠煤矿起家的家族，是怎么一步步登上日本政坛C位的？」")
        
        parts.append("\n2. **分轨思维（画面 + 解说）**：")
        parts.append("   脚本要包含【画面描述】和【解说词】的对应关系。")
        parts.append("   格式示例：")
        parts.append("   [画面：麻生太郎歪着嘴的特写照片，黑白处理，配上红眼特效]")
        parts.append("   旁白：别看现在他一副「我累了，人类」的模样，他家的发家史，可一点都不佛系。")
        
        parts.append("\n3. **B站味调教**：")
        parts.append("   - 多用短句，节奏紧凑（避免长难句）")
        parts.append("   - 适当使用语气词：'甚至'、'好家伙'、'离大谱'、'这公平吗？不。但这很现实。'")
        parts.append("   - 梗+干货+反转+升华的节奏：先用梗吸引，再给硬核事实，然后反转预期，最后升华主题")
        parts.append("   - 适度阴阳怪气，但观点要清晰")
        
        parts.append("\n4. **画面指导（B-Roll）**：")
        parts.append("   不要只写 [素材占位]，要写具体的画面需求：")
        parts.append("   - [画面：黑白老照片缓缓浮现，日本战时工业区航拍镜头]")
        parts.append("   - [画面：泛黄档案文件特写，标注「强制劳働者」字样]")
        parts.append("   - [画面：动漫《进击的巨人》片段，墙破瞬间 + 「ALERT」音效]")
        parts.append("   让剪辑师知道这里该放什么类型的素材（鬼畜、严肃、数据图等）")
        
        parts.append("\n5. **结构要求**：")
        parts.append("   - 必须包含明确的章节标题，使用 ## 标记（例如：## 第一章：标题）")
        parts.append("   - 章节之间用 --- 分隔")
        parts.append("   - 包含：Cold Open → 正文（3-5个章节）→ 结尾升华")
        
        parts.append("\n6. **内容要求】：")
        parts.append("   - 不要胡编硬造，严格基于给定事实展开")
        parts.append("   - 可以适度类比、吐槽、玩梗")
        parts.append("   - 每个章节都要有画面指导，不要纯文字")
        
        parts.append(f"\n【主题】{research.topic}\n")
        
        parts.append("【硬核事实清单】")
        for idx, fact in enumerate(research.facts, start=1):
            parts.append(f"{idx}. {fact.title} —— {fact.summary}")
        
        parts.append("\n【输出格式】")
        parts.append("请直接输出完整脚本，格式如下：")
        parts.append("```")
        parts.append("【Cold Open：黑屏白字+低沉BGM】")
        parts.append("旁白：最炸裂的开场句...")
        parts.append("[画面：具体画面描述]")
        parts.append("")
        parts.append("---")
        parts.append("")
        parts.append("## 第一章：标题")
        parts.append("旁白：正文内容...")
        parts.append("[画面：具体画面描述]")
        parts.append("")
        parts.append("---")
        parts.append("")
        parts.append("## 结尾：升华")
        parts.append("旁白：结尾升华...")
        parts.append("```")
        parts.append("\n不要输出「分析」「思路」等说明性文字，直接写脚本正文。")
        
        prompt_template = "\n".join(parts)
    
    # 构建事实清单
    facts_list = "\n".join([f"{idx}. {fact.title} —— {fact.summary}" 
                            for idx, fact in enumerate(research.facts, start=1)])
    
    prompt = format_prompt(prompt_template, topic=research.topic, facts_list=facts_list)
    return prompt


def _write_script_with_qwen(research: ResearchResult, debug: bool = False) -> ScriptDraft | None:
    """调用通义千问 Qwen-plus 生成脚本。

    若未配置 API 或调用失败，则返回 None，让上层回退到本地模板。
    
    Args:
        research: 搜研结果
        debug: 是否输出调试信息
    """

    if not _HAS_OPENAI:
        if debug:
            print("  ⚠️ OpenAI 库未安装，无法调用 Qwen API")
        return None

    api_key = CONFIG.llm.api_key
    if not api_key:
        if debug:
            print("  ⚠️ API Key 未配置，无法调用 Qwen API")
        return None

    prompt = _build_prompt_for_qwen(research)

    try:
        if debug:
            print(f"  📤 调用 Qwen API 生成脚本 (模型: {CONFIG.llm.model_writer})...")
        
        client = OpenAI(  # type: ignore[call-arg]
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        try:
            system_message = load_prompt_system("step2_writer.yaml")
        except (FileNotFoundError, ImportError):
            # 回退到硬编码 system message（兼容性）
            system_message = "你是一位擅长做政经史长视频的 B 站金牌编剧。"
        
        completion = client.chat.completions.create(
            model=CONFIG.llm.model_writer,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
        )
    except Exception as e:
        if debug:
            print(f"  ❌ Qwen API 调用失败: {type(e).__name__}: {str(e)[:200]}")
        return None

    try:
        body = completion.choices[0].message.content or ""  # type: ignore[assignment]
    except Exception as e:
        if debug:
            print(f"  ❌ 解析 API 响应失败: {type(e).__name__}: {str(e)[:200]}")
        return None

    body = body.strip()
    
    if debug:
        print(f"  📥 API 返回内容长度: {len(body)} 字符")
        print(f"  📥 内容预览: {body[:300]}...")
    
    if not body:
        if debug:
            print("  ❌ API 返回内容为空")
        return None

    # 尝试从脚本内容中提取标题
    title = _extract_title_from_body(body, research.topic, debug=debug)
    
    # 解析脚本内容，拆分为多个 sections
    sections = _parse_script_into_sections(body)
    
    if debug:
        print(f"  ✅ 成功解析为 {len(sections)} 个章节")

    placeholders = extract_placeholders(sections)
    
    if debug:
        print(f"  ✅ 提取到 {len(placeholders)} 个素材占位符")

    return ScriptDraft(
        topic=research.topic,
        title=title,
        sections=sections,
        placeholders=placeholders,
    )


def write_script(research: ResearchResult, debug: bool = False) -> ScriptDraft:
    """
    输入 Step 1 的搜研结果，输出带占位符的脚本草稿。

    优先调用 Qwen-plus；若 API 不可用，则使用本地模板规则。
    
    Args:
        research: 搜研结果
        debug: 是否输出调试信息
    """

    script = _write_script_with_qwen(research, debug=debug)
    if script is not None:
        if debug:
            print("  ✅ 使用 Qwen 生成的脚本")
        return script

    # === 回退：使用原有模板逻辑 ===
    if debug:
        print("  ⚠️ Qwen API 调用失败，回退到本地模板")
    
    # 使用智能标题生成（避免硬编码"黑历史"）
    title = _extract_title_from_body("", research.topic, debug=debug)

    sections: List[ScriptSection] = []
    sections.append(_build_intro_section(research))
    sections.extend(_build_body_sections(research))
    sections.append(_build_outro_section(research))

    placeholders = extract_placeholders(sections)

    return ScriptDraft(
        topic=research.topic,
        title=title,
        sections=sections,
        placeholders=placeholders,
    )


