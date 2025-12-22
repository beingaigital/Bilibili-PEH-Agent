import argparse
import os
from datetime import datetime

import gradio as gr
from rich.console import Console
from rich.panel import Panel

from agent_mvp.step1_research import (
    research_topic,
    _gen_baike_keywords,
    _fetch_baike_chunks,
    _distill_facts_with_qwen,
    _gen_outline_with_qwen,
)
from agent_mvp.step2_writer import write_script
from agent_mvp.step3_materials import match_materials
from agent_mvp.step4_compose import compose_final_script, check_sensitive_words

console = Console()


def _save_markdown_output(topic: str, final_script, output_dir: str = "output") -> str:
    """将最终结果保存为标准 Markdown 文件。

    保存内容：
    - 标题（一级标题）
    - 主题说明
    - 成品脚本正文（按 compose_final_script 生成的内容）
    - 素材列表（占位符、标题、理由、网页链接、播放直链）
    """
    os.makedirs(output_dir, exist_ok=True)

    # 生成安全的文件名：时间戳 + 简化后的主题
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 去掉可能导致文件名异常的字符
    safe_topic = "".join(c for c in topic if c not in '\\/:*?"<>|').strip()
    if len(safe_topic) > 40:
        safe_topic = safe_topic[:40]
    filename = f"{ts}_{safe_topic or 'topic'}.md"
    path = os.path.join(output_dir, filename)

    lines: list[str] = []
    lines.append(f"# {final_script.title or topic}")
    lines.append("")
    lines.append(f"> 主题：{topic}")
    lines.append("")
    lines.append("## 成品脚本文本")
    lines.append("")

    # 对 Step 4 的文本做 Markdown 友好的轻量转换：
    # - 去掉内部的【标题】/【正文】行（外面已经有标题）
    # - 将“—— xxx ——”转换为三级标题
    # - 将「旁白：」高亮为加粗前缀
    # - 将「[画面：...]」转成加粗前缀（普通文本，不用引用块）
    # - 删除正文中的「推荐素材 / 理由 / 链接」行，避免与下方素材列表重复
    # - 清理任何代码块标记（```），确保正文是普通 Markdown 文本
    raw_content = final_script.content
    # 移除代码块标记（如果存在）
    raw_content = raw_content.replace("```markdown", "").replace("```", "")
    raw_lines = raw_content.splitlines()
    body_lines: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        # 跳过内部标题说明
        if stripped.startswith("【标题】"):
            continue
        if stripped == "【正文】":
            continue

        # 将装饰性分隔标题“—— xxx ——”转成 Markdown 三级标题
        if stripped.startswith("——") and stripped.endswith("——") and len(stripped) > 4:
            heading_text = stripped.strip("—").strip()
            # 只在前面有内容且不是空行时才添加空行
            if body_lines and body_lines[-1].strip() != "":
                body_lines.append("")
            body_lines.append(f"### {heading_text}")
            # 标题后添加一个空行（后续合并逻辑会处理连续空行）
            body_lines.append("")
            continue

        # 旁白行：加粗前缀，保留正文（不添加空行）
        if stripped.startswith("旁白："):
            content = stripped[len("旁白：") :].lstrip()
            body_lines.append(f"**旁白：**{content}")
            continue

        # 画面描述行：[画面：xxx] -> 加粗前缀（不添加空行）
        if stripped.startswith("[画面：") and stripped.endswith("]"):
            inner = stripped[1:-1]  # 去掉两侧中括号
            # inner 形如 "画面：xxx"
            if "：" in inner:
                label, desc = inner.split("：", 1)
            else:
                label, desc = "画面", inner
            body_lines.append(f"**{label}：**{desc.strip()}")
            continue

        # 删除正文中的推荐素材块（统一放在下方「素材列表」章节）
        if stripped.startswith("推荐素材：") or stripped.startswith("- 推荐素材：") or stripped.startswith("· 推荐素材："):
            continue
        if stripped.startswith("理由：") or stripped.startswith("链接："):
            continue

        # 跳过原始内容中的空行（只在章节标题前后保留空行）
        if stripped == "":
            continue

        body_lines.append(line)

    # 合并连续的空行（最多保留一个空行）
    cleaned_lines: list[str] = []
    prev_empty = False
    for line in body_lines:
        is_empty = line.strip() == ""
        if is_empty:
            if not prev_empty:  # 只在连续空行的第一个位置保留一个空行
                cleaned_lines.append("")
            prev_empty = True
        else:
            cleaned_lines.append(line)
            prev_empty = False

    lines.extend(cleaned_lines)
    lines.append("")
    lines.append("## 素材列表")
    lines.append("")

    if not final_script.materials:
        lines.append("_（未匹配到任何素材）_")
    else:
        for idx, m in enumerate(final_script.materials, 1):
            lines.append(f"### 素材 {idx}")
            lines.append("")
            lines.append(f"- **占位符**：{m.placeholder}")
            lines.append(f"- **关键词**：{m.keyword}")
            lines.append(f"- **标题**：{m.title}")
            lines.append(f"- **理由**：{m.reason}")
            if m.url:
                lines.append(f"- **网页链接**：{m.url}")
            if m.play_url:
                lines.append(f"- **播放直链**：`{m.play_url}`")
            lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return path


def run_pipeline_cli(topic: str) -> None:
    """命令行模式下运行四步流水线。"""
    console.rule("[bold blue]Step 1 - 搜研专家[/bold blue]")
    console.print(f"[bold]输入选题：[/bold]{topic}\n")

    # Step 1.1: 关键词拆解
    console.print("[yellow]Step 1.1: Qwen 拆解关键词[/yellow]")
    keywords = _gen_baike_keywords(topic)
    if keywords:
        console.print(f"[green]✓[/green] 生成关键词：{', '.join(keywords)}")
    else:
        console.print("[dim]✗[/dim] 关键词生成失败，使用原选题作为关键词")
        keywords = [topic]
    console.print()

    # Step 1.2: 百度百科抓取
    console.print("[yellow]Step 1.2: 百度百科抓取摘要[/yellow]")
    baike_chunks = []
    failed_keywords = []
    
    # 一次性处理所有关键词（只创建一个浏览器实例，避免资源问题）
    console.print("  正在批量抓取所有关键词...")
    all_chunks = _fetch_baike_chunks(keywords, max_pages=len(keywords), debug=True)
    
    # 统计成功和失败的关键词
    for kw in keywords:
        # 检查是否有该关键词的抓取结果
        found = any(kw in chunk for chunk in all_chunks)
        if found:
            console.print(f"  [green]✓[/green] {kw}")
            baike_chunks.extend([chunk for chunk in all_chunks if kw in chunk])
        else:
            console.print(f"  [dim]✗[/dim] {kw}")
            failed_keywords.append(kw)
    
    if baike_chunks:
        console.print(f"\n[green]✓[/green] 共成功抓取 {len(baike_chunks)} 条百科摘要：")
        for idx, chunk in enumerate(baike_chunks, 1):
            preview = chunk[:100].replace("\n", " ") + "..." if len(chunk) > 100 else chunk
            console.print(f"  {idx}. {preview}")
    else:
        console.print(f"\n[dim]✗[/dim] 所有关键词抓取失败")
        if failed_keywords:
            console.print(f"[dim]失败的关键词：{', '.join(failed_keywords)}[/dim]")
            console.print("[dim]\n💡 调试提示：如需查看详细错误信息，可以在 Python 中运行：[/dim]")
            console.print("[dim]  from agent_mvp.step1_research import _fetch_baike_chunks[/dim]")
            console.print(f"[dim]  _fetch_baike_chunks(['{failed_keywords[0]}'], debug=True)[/dim]")
        console.print("[dim]\n可能原因：[/dim]")
        console.print("[dim]  1. 关键词在百度百科中不存在（LLM 生成了修饰性短语而非实体名）[/dim]")
        console.print("[dim]  2. 网络连接问题或超时（请检查网络和代理设置）[/dim]")
        console.print("[dim]  3. 百度百科反爬虫限制（触发安全验证，可能需要浏览器访问验证）[/dim]")
        console.print("[dim]  4. 百度百科页面结构变更（选择器失效）[/dim]")
        console.print("[dim]\n⚠️  注意：由于抓取失败，后续步骤可能无法生成有效内容[/dim]")
    console.print()

    # Step 1.3: Qwen 脱水成事实清单
    console.print("[yellow]Step 1.3: Qwen 脱水成事实清单[/yellow]")
    facts = _distill_facts_with_qwen(topic, baike_chunks, debug=True) if baike_chunks else None
    if facts:
        console.print(f"[green]✓[/green] 生成 {len(facts)} 条硬核事实：")
        for fact in facts:
            console.print(f"  - {fact.title}: {fact.summary[:80]}...")
    else:
        console.print("[dim]✗[/dim] 事实脱水失败")
        if not baike_chunks:
            console.print("[dim]原因：没有可用的百科数据（Step 1.2 抓取失败）[/dim]")
        else:
            console.print("[dim]原因：Qwen API 调用失败或返回格式错误（详见上方调试信息）[/dim]")
        facts = []
        console.print("[dim]⚠️  注意：没有事实数据，后续脚本生成可能为空[/dim]")
    console.print()

    # Step 1.4: 生成动态大纲
    console.print("[yellow]Step 1.4: Qwen 生成动态大纲[/yellow]")
    outline = _gen_outline_with_qwen(topic, facts, debug=True) if facts else None
    if outline:
        console.print(f"[green]✓[/green] 生成动态大纲（{len(outline)} 个章节）：")
        for idx, section in enumerate(outline, 1):
            console.print(f"  {idx}. {section}")
    else:
        console.print("[dim]✗[/dim] 大纲生成失败，使用默认模板")
        outline = [
            "开场与人物/事件引入",
            "发家史或早期发展阶段",
            "关键转折点与黑历史",
            "当代延续与讽刺意味",
        ]
        console.print(f"[dim]使用默认大纲（{len(outline)} 个章节）[/dim]")
    console.print()

    # 汇总输出
    console.rule("[dim]Step 1 汇总输出[/dim]")
    console.print(f"[bold]主题：[/bold]{topic}")
    console.print(f"[bold]大纲：[/bold]" + " / ".join(outline))
    console.print(f"\n[bold]硬核事实清单（共 {len(facts)} 条）[/bold]")
    for fact in facts:
        console.print(f"  - [bold]{fact.title}[/bold]: {fact.summary}")

    # 创建 ResearchResult 对象供后续步骤使用
    from agent_mvp.data_types import ResearchResult
    research = ResearchResult(topic=topic, outline=outline, facts=facts)

    console.rule("[bold blue]Step 2 - 金牌编剧[/bold blue]")
    script = write_script(research, debug=True)
    console.print(f"[bold]标题候选：[/bold]{script.title}")

    console.print("\n[bold]脚本结构预览：[/bold]")
    for sec in script.sections:
        console.print(f"[green]#{sec.heading}[/green]")

    console.print("\n[bold]📝 完整脚本内容：[/bold]")
    console.print()
    for idx, sec in enumerate(script.sections, 1):
        console.print(f"[bold cyan]【{sec.heading}】[/bold cyan]")
        # 显示文案内容，保留换行格式
        body_lines = sec.body.split('\n')
        for line in body_lines:
            if line.strip():
                console.print(f"  {line}")
            else:
                console.print()  # 保留空行
        console.print()  # 章节之间空一行

    console.print("\n[bold]检测到的素材占位符：[/bold]")
    for p in script.placeholders:
        console.print(f"- {p}")

    console.rule("[bold blue]Step 3 - 素材匹配[/bold blue]")
    materials = match_materials(script, debug=True)
    for idx, m in enumerate(materials.materials, 1):
        # 构建显示内容
        content_lines = [
            f"[bold]占位符：[/bold]{m.placeholder}",
            f"[bold]关键词：[/bold]{m.keyword}",
            f"[bold]标题：[/bold]{m.title}",
            f"[bold]理由：[/bold]{m.reason}",
            f"[bold]网页链接：[/bold]{m.url}",
        ]
        
        # 如果有播放直链，显示它
        if m.play_url:
            content_lines.append(f"[bold green]播放直链：[/bold green]{m.play_url[:80]}...")
            content_lines.append("[dim]（可直接在 <video> 标签中使用）[/dim]")
        elif m.extra.get("mock"):
            content_lines.append("[dim yellow]（模拟数据，无播放直链）[/dim yellow]")
        else:
            content_lines.append("[dim]（未获取到播放直链，请使用网页链接）[/dim]")
        
        console.print(
            Panel.fit(
                "\n".join(content_lines),
                title=f"素材 {idx}",
            )
        )

    console.rule("[bold blue]Step 4 - 最终合成[/bold blue]")
    final_script = compose_final_script(script, materials, enable_safety_filter=True, strict_mode=False)
    
    # 检查是否有敏感词（用于显示警告信息）
    console.print("[yellow]内容安全检查...[/yellow]")
    all_matches = []
    if final_script.title:
        all_matches.extend(check_sensitive_words(final_script.title))
    if final_script.content:
        all_matches.extend(check_sensitive_words(final_script.content))
    for m in final_script.materials:
        if m.title:
            all_matches.extend(check_sensitive_words(m.title))
        if m.reason:
            all_matches.extend(check_sensitive_words(m.reason))
    
    if all_matches:
        unique_words = set(word for word, _ in all_matches)
        console.print(f"[yellow]⚠ 发现 {len(all_matches)} 处敏感内容（{len(unique_words)} 个不同敏感词），已自动替换为 ***[/yellow]")
        console.print(f"  [dim]敏感词示例: {', '.join(list(unique_words)[:5])}{'...' if len(unique_words) > 5 else ''}[/dim]")
    else:
        console.print("[green]✓[/green] 内容安全检查通过")
    console.print()
    
    console.print(Panel(final_script.content, title="成品脚本文本", expand=False))

    # 保存为 Markdown 文件（已包含过滤）
    output_path = _save_markdown_output(topic, final_script, output_dir="output")
    console.print(f"\n[bold green]✓ 已保存为 Markdown 文件：[/bold green]{output_path}\n")


def _generate_markdown_content(topic: str, final_script) -> str:
    """生成 Markdown 格式的内容字符串（不保存文件）。
    
    与 _save_markdown_output 的逻辑一致，但只返回内容字符串。
    """
    lines: list[str] = []
    lines.append(f"# {final_script.title or topic}")
    lines.append("")
    lines.append(f"> 主题：{topic}")
    lines.append("")
    lines.append("## 成品脚本文本")
    lines.append("")

    # 对 Step 4 的文本做 Markdown 友好的轻量转换：
    raw_content = final_script.content
    raw_content = raw_content.replace("```markdown", "").replace("```", "")
    raw_lines = raw_content.splitlines()
    body_lines: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        if stripped.startswith("【标题】"):
            continue
        if stripped == "【正文】":
            continue

        if stripped.startswith("——") and stripped.endswith("——") and len(stripped) > 4:
            heading_text = stripped.strip("—").strip()
            if body_lines and body_lines[-1].strip() != "":
                body_lines.append("")
            body_lines.append(f"### {heading_text}")
            body_lines.append("")
            continue

        if stripped.startswith("旁白："):
            content = stripped[len("旁白：") :].lstrip()
            body_lines.append(f"**旁白：**{content}")
            continue

        if stripped.startswith("[画面：") and stripped.endswith("]"):
            inner = stripped[1:-1]
            if "：" in inner:
                label, desc = inner.split("：", 1)
            else:
                label, desc = "画面", inner
            body_lines.append(f"**{label}：**{desc.strip()}")
            continue

        if stripped.startswith("推荐素材：") or stripped.startswith("- 推荐素材：") or stripped.startswith("· 推荐素材："):
            continue
        if stripped.startswith("理由：") or stripped.startswith("链接："):
            continue

        if stripped == "":
            continue

        body_lines.append(line)

    # 合并连续的空行
    cleaned_lines: list[str] = []
    prev_empty = False
    for line in body_lines:
        is_empty = line.strip() == ""
        if is_empty:
            if not prev_empty:
                cleaned_lines.append("")
            prev_empty = True
        else:
            cleaned_lines.append(line)
            prev_empty = False

    lines.extend(cleaned_lines)
    lines.append("")
    lines.append("## 素材列表")
    lines.append("")

    if not final_script.materials:
        lines.append("_（未匹配到任何素材）_")
    else:
        for idx, m in enumerate(final_script.materials, 1):
            lines.append(f"### 素材 {idx}")
            lines.append("")
            lines.append(f"- **占位符**：{m.placeholder}")
            lines.append(f"- **关键词**：{m.keyword}")
            lines.append(f"- **标题**：{m.title}")
            lines.append(f"- **理由**：{m.reason}")
            if m.url:
                lines.append(f"- **网页链接**：{m.url}")
            if m.play_url:
                lines.append(f"- **播放直链**：`{m.play_url}`")
            lines.append("")

    return "\n".join(lines)


def run_pipeline_ui(topic: str) -> tuple[str, str, str | None]:
    """给 Gradio 用的封装：输入选题，返回成品脚本 + 素材摘要 + Markdown 文件路径。"""
    if not topic.strip():
        return "请先输入一个选题。", "", None

    try:
        research = research_topic(topic.strip())
        script = write_script(research)
        materials = match_materials(script)
        final_script = compose_final_script(script, materials, enable_safety_filter=True, strict_mode=False)
        
        # 检查是否有敏感词（用于显示警告信息）
        all_matches = []
        if final_script.title:
            all_matches.extend(check_sensitive_words(final_script.title))
        if final_script.content:
            all_matches.extend(check_sensitive_words(final_script.content))
        for m in final_script.materials:
            if m.title:
                all_matches.extend(check_sensitive_words(m.title))
            if m.reason:
                all_matches.extend(check_sensitive_words(m.reason))
        
        # 如果有敏感词警告，在内容前添加提示
        warning_prefix = ""
        if all_matches:
            unique_words = set(word for word, _ in all_matches)
            warning_prefix = f"⚠️ 内容安全提示：检测到 {len(all_matches)} 处敏感内容（{len(unique_words)} 个不同敏感词），已自动替换为 ***\n\n"

        content = warning_prefix + final_script.content

        material_lines: list[str] = []
        for m in final_script.materials:
            material_lines.append(f"占位符：{m.placeholder}")
            material_lines.append(f"- 标题：{m.title}")
            material_lines.append(f"- 理由：{m.reason}")
            material_lines.append(f"- 链接：{m.url}")
            material_lines.append("")

        materials_text = "\n".join(material_lines).strip()

        # 生成 Markdown 文件供下载（已包含过滤）
        markdown_content = _generate_markdown_content(topic.strip(), final_script)
        
        # 创建临时文件（使用绝对路径）
        os.makedirs("output", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_topic = "".join(c for c in topic if c not in '\\/:*?"<>|').strip()
        if len(safe_topic) > 40:
            safe_topic = safe_topic[:40]
        filename = f"{ts}_{safe_topic or 'topic'}.md"
        file_path = os.path.abspath(os.path.join("output", filename))
        
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)
        
        return content, materials_text, file_path
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        error_type = type(e).__name__
        error_msg = f"生成过程中出现错误：{error_type}: {str(e)}\n\n提示：\n- 请检查 .env 文件中的 DASHSCOPE_API_KEY 是否正确配置\n- 请检查网络连接是否正常\n- 如果问题持续，请尝试使用命令行模式查看详细错误信息\n\n详细错误信息：\n{error_detail[-500:]}"  # 只显示最后500字符
        return error_msg, error_msg, None


def launch_web():
    """启动 Gradio 网页界面。"""
    # 避免本地 127.0.0.1 请求走代理导致 502
    os.environ.pop("HTTP_PROXY", None)
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "false")

    # 轻量级自定义样式，提升整体观感
    custom_css = """
    body { background: radial-gradient(circle at top left, #1f2933 0, #111827 45%, #020617 100%) !important; }
    .gradio-container { max-width: 1180px !important; margin: 0 auto !important; }
    #title-card { border-radius: 14px !important; background: linear-gradient(135deg,#111827,#020617) !important; border: 1px solid rgba(148, 163, 184, 0.35) !important; padding: 16px 18px !important; }
    #title-card h1 { font-size: 1.8rem !important; margin-bottom: 0.4rem !important; color: #e5e7eb !important; }
    #title-card p { font-size: 0.9rem !important; color: #9ca3af !important; }
    .tool-section { border-radius: 12px !important; border: 1px solid rgba(55, 65, 81, 0.8) !important; background: radial-gradient(circle at top left, rgba(17,24,39,0.96), rgba(15,23,42,0.98)); padding: 14px 16px !important; }
    .flow-section { color: #ffffff !important; }
    .flow-section * { color: #ffffff !important; }
    .preview-box textarea { font-family: "JetBrains Mono", "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace !important; font-size: 13px !important; line-height: 1.5 !important; }
    .preview-box label span { font-weight: 600 !important; }
    .footer-tip { font-size: 0.78rem !important; color: #9ca3af !important; text-align: right; margin-top: 0.5rem; }
    """

    with gr.Blocks(
        title="B站政经史脚本生成 Agent（MVP）",
        theme=gr.themes.Soft(),
        css=custom_css,
    ) as demo:
        # 顶部标题卡片
        with gr.Column(elem_id="title-card"):
            gr.Markdown(
                """
                <h1>📺 B站政经史「硬核」脚本生成 Agent · Demo</h1>
                <p>输入一个选题，从搜研、写稿、素材匹配到成品合成，一键生成适合 B 站风格的视频脚本。</p>
                """,
            )

        with gr.Row():
            with gr.Column(scale=3, elem_classes=["tool-section"]):
                topic_input = gr.Textbox(
                    label="🎯 选题（支持一句话概述）",
                    placeholder="例如：麻生太郎家族发展史：从煤矿财阀到鹰派政客",
                    lines=2,
                )
                gr.Markdown(
                    "小提示：尽量写清「人物 / 时间 / 事件」，可以提高百科命中率和脚本质量。",
                )

                with gr.Row():
                    generate_btn = gr.Button("🚀 生成脚本", variant="primary", scale=3)
                    save_btn = gr.Button("💾 生成并下载 Markdown", variant="secondary", scale=2)

            with gr.Column(scale=2, elem_classes=["tool-section", "flow-section"]):
                gr.Markdown(
                    """
                    ### 🔍 流程一览
                    1. **Step 1：搜研与事实脱水**（Bing + 百度百科 + Qwen）
                    2. **Step 2：脚本撰写**（按章节生成旁白 + 画面）
                    3. **Step 3：B站素材匹配**（关键词 → 搜索 → 播放直链）
                    4. **Step 4：敏感词过滤 & 成品合成**（自动做基础内容合规）
                    """,
                )

        with gr.Row():
            with gr.Column(elem_classes=["tool-section", "preview-box"]):
                script_output = gr.Textbox(
                    label="📝 生成的脚本文本（可直接拷贝到剪映 / PR）",
                    lines=24,
                )
            with gr.Column(elem_classes=["tool-section", "preview-box"]):
                materials_output = gr.Textbox(
                    label="🎬 素材匹配摘要（占位符 → B站视频）",
                    lines=24,
                )

        # 下载文件组件（生成后显示下载链接）
        download_file = gr.File(
            label="📥 下载 Markdown 文件",
            visible=True,
        )

        gr.Markdown(
            '<div class="footer-tip">本 Demo 仅用于个人学习与内容创作辅助，请遵守 B 站及相关平台的使用规范与法律法规。</div>'
        )

        def generate_only(topic: str):
            """仅生成脚本，不提供下载"""
            content, materials, _ = run_pipeline_ui(topic)
            return content, materials, None

        def generate_and_save(topic: str):
            """生成脚本并返回文件路径供下载"""
            content, materials, file_path = run_pipeline_ui(topic)
            return content, materials, file_path

        generate_btn.click(
            fn=generate_only,
            inputs=[topic_input],
            outputs=[script_output, materials_output, download_file],
        )

        save_btn.click(
            fn=generate_and_save,
            inputs=[topic_input],
            outputs=[script_output, materials_output, download_file],
        )

    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        share=False,
        show_error=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="B站政经史脚本生成 Agent（MVP）")
    parser.add_argument(
        "--mode",
        choices=["cli", "web"],
        default="cli",
        help="运行模式：cli（命令行）或 web（网页 UI），默认 cli",
    )
    parser.add_argument(
        "--topic",
        type=str,
        default="麻生太郎家族发展史：从煤矿财阀到鹰派政客",
        help="选题内容，仅在 cli 模式下使用",
    )
    args = parser.parse_args()

    if args.mode == "web":
        launch_web()
    else:
        run_pipeline_cli(args.topic)


