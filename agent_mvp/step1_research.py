"""
Step 1: 考据 / 事实脱水

当前实现：
- 使用 Qwen-plus 拆关键词 + 百度百科抓取 + Qwen-plus 脱水（无需梯子）
- 若 API Key 或网络不可用，返回空数据（不提供示例数据，便于测试真实效果）
"""

import json
import random
import time
import os
from typing import Dict, List, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .data_types import ResearchResult, FactItem
from .config import CONFIG
from .prompt_loader import load_prompt, load_prompt_user, load_prompt_system, format_prompt

try:
    from openai import OpenAI  # type: ignore

    _HAS_OPENAI = True
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore
    _HAS_OPENAI = False

# 尝试导入 DrissionPage（备选方案，更强的反爬能力）
try:
    from DrissionPage import ChromiumPage, ChromiumOptions
    _HAS_DRISSION = True
except ImportError:
    _HAS_DRISSION = False


# -------------------------
def _llm_client() -> Optional["OpenAI"]:
    if not _HAS_OPENAI:
        return None
    api_key = CONFIG.llm.api_key
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")


def _proxy_enabled() -> bool:
    """判断是否启用了代理（用于决定是否尝试 Google）。"""
    return bool(
        os.getenv("https_proxy")
        or os.getenv("http_proxy")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
    )


def _get_proxy_dict() -> Optional[Dict[str, str]]:
    proxies: Dict[str, str] = {}
    for scheme in ("http", "https"):
        value = os.getenv(f"{scheme}_proxy") or os.getenv(f"{scheme.upper()}_PROXY")
        if value:
            proxies[scheme] = value
    return proxies or None


def _translate_to_zh(text: str, debug: bool = False) -> tuple[str, bool, str]:
    """将文本翻译成中文（优先使用 LLM，失败则返回原文）。
    
    Returns:
        (translated_text, is_translated, source_lang)
    """
    if not text:
        return "", False, ""
    client = _llm_client()
    if client is None:
        return text, False, ""
    try:
        completion = client.chat.completions.create(
            model=CONFIG.llm.model_research,
            messages=[
                {"role": "system", "content": "你是翻译助手，请将输入翻译成流畅的中文，不要添加解释。"},
                {"role": "user", "content": text[:3000]},
            ],
            max_tokens=800,
            temperature=0.2,
        )
        out = completion.choices[0].message.content or ""
        if out.strip():
            return out.strip(), True, "en"
    except Exception as e:
        if debug:
            print(f"  ⚠️ 翻译失败：{type(e).__name__}: {str(e)[:80]}")
    return text, False, ""


def _fetch_wikipedia_chunks(keywords: List[str], max_pages: int = 3, debug: bool = False) -> List[Dict[str, str]]:
    """优先使用维基百科摘要（支持中英，英文可翻译）。"""
    results: List[Dict[str, str]] = []
    session = requests.Session()
    for kw in keywords[:max_pages]:
        encoded = quote(kw.replace(" ", "_"))
        page_data = None
        lang_used = ""
        for lang in ["zh", "en"]:
            url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{encoded}"
            try:
                resp = session.get(url, timeout=10)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                extract = data.get("extract", "")
                if not extract or len(extract) < 50:
                    continue
                page_data = data
                lang_used = lang
                break
            except Exception as e:
                if debug:
                    print(f"  ⚠️ Wikipedia {lang} 摘要获取失败: {str(e)[:80]}")
                continue

        if not page_data:
            continue

        summary_text = page_data.get("extract", "")
        translated = False
        translated_from = ""
        if lang_used.startswith("en"):
            summary_text, translated, translated_from = _translate_to_zh(summary_text, debug=debug)

        page_url = (
            page_data.get("content_urls", {}).get("desktop", {}).get("page")
            or f"https://{lang_used or 'zh'}.wikipedia.org/wiki/{encoded}"
        )
        results.append(
            {
                "text": f"{kw}：{summary_text[:2000]}",
                "url": page_url,
                "lang": lang_used or "zh",
                "translated_from": translated_from if translated else "",
                "source_site": "wikipedia",
            }
        )
        if debug:
            print(f"  ✓ Wikipedia 捕获：{kw} ({lang_used}) {len(summary_text)}字")
    return results


def _fetch_google_snippets(keywords: List[str], max_pages: int = 3, debug: bool = False) -> List[str]:
    """在启用代理的情况下使用 Google 搜索摘要。"""
    proxies = _get_proxy_dict()
    if not proxies:
        return []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    session = requests.Session()
    session.headers.update(headers)
    results: List[str] = []
    for kw in keywords[:max_pages]:
        try:
            resp = session.get(
                "https://www.google.com/search",
                params={"q": kw, "hl": "zh-CN"},
                timeout=CONFIG.search.google_timeout,
                proxies=proxies,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                if debug:
                    print(f"  ⚠️ Google 搜索状态码: {resp.status_code}")
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            # Google 结果列表
            cards = soup.select("div.g") or soup.select("div.tF2Cxc")
            snippet_text = ""
            for card in cards[:3]:
                title = card.select_one("h3")
                snippet = card.select_one(".VwiC3b") or card.select_one("span.aCOpRe") or card.select_one("div.IsZvec")
                if snippet:
                    text = snippet.get_text(" ", strip=True)
                    if text and len(text) > 30:
                        snippet_text = text
                        break
                if title and kw in title.get_text():
                    snippet_text = title.get_text()
            if snippet_text:
                results.append(f"{kw}：{snippet_text[:2000]}")
                if debug:
                    print(f"  ✅ Google 摘要: {kw} ({len(snippet_text)}字)")
        except Exception as e:
            if debug:
                print(f"  ⚠️ Google 搜索失败: {type(e).__name__}: {str(e)[:80]}")
            continue
        time.sleep(random.uniform(0.5, 1.0))
    return results


def _gen_baike_keywords(topic: str) -> Optional[List[str]]:
    client = _llm_client()
    if client is None:
        return None

    try:
        prompt_template = load_prompt_user("step1_keywords.yaml")
        system_message = load_prompt_system("step1_keywords.yaml")
    except (FileNotFoundError, ImportError):
        # 回退到硬编码提示词（兼容性）
        prompt_template = (
            "请为下面的选题生成 3-5 个适合百度百科检索的【实体名词】关键词，"
            "要求：\n"
            "- 必须是具体的实体名词，不要带修饰语或描述性短语\n"
            "- 例如：用'麻生太郎'而不是'麻生太郎的生平'，用'麻生矿业'而不是'麻生矿业的历史'\n"
            "- 优先选择人名、地名、机构名、事件名等可直接作为百科词条标题的实体\n"
            "- 输出为 JSON 数组（仅数组本身，无多余说明）\n"
            "选题：{topic}"
        )
        system_message = "你是善于拆解历史/政经选题的研究助理，只输出 JSON 数组。"
    
    prompt = format_prompt(prompt_template, topic=topic)
    
    try:
        resp = client.chat.completions.create(
            model=CONFIG.llm.model_research,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
        )
        content = resp.choices[0].message.content or ""
        # 尝试提取 JSON 数组（可能包含 markdown 代码块）
        content = content.strip()
        if content.startswith("```"):
            # 移除 markdown 代码块标记
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
        if content.startswith("```json"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if len(lines) > 2 else content

        # 尝试找到 JSON 数组部分
        start_idx = content.find("[")
        end_idx = content.rfind("]")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            content = content[start_idx : end_idx + 1]

        data = json.loads(content)
        if isinstance(data, list):
            return [str(x) for x in data if str(x).strip()]
    except (json.JSONDecodeError, Exception):
        return None
    return None


# -------------------------
def _fetch_baike_chunks_drission(keywords: List[str], max_pages: int = 3, debug: bool = False) -> List[str]:
    """使用 DrissionPage 抓取（已适配 v4.x 版本语法）
    
    DrissionPage 通过直接控制浏览器协议（CDP）来操作，无需下载 ChromeDriver，
    且天生具备极强的抗指纹检测能力。
    """
    if not _HAS_DRISSION:
        if debug:
            print("  ⚠️ DrissionPage 未安装，跳过此方案")
        return []
        
    chunks = []
    page = None
    
    try:
        if debug:
            print(f"--- 开始抓取（DrissionPage），关键词: {keywords} ---")
        
        # --- 修复点：适配 DrissionPage v4.x 语法 ---
        co = ChromiumOptions()
        # 旧版写法: co.set_headless() 导致报错
        # 新版写法:
        co.headless(True) 
        co.set_argument('--no-sandbox')  # 增加稳定性
        co.set_argument('--disable-gpu')
        
        # 自动查找浏览器路径，初始化页面
        page = ChromiumPage(co)
        
        for kw in keywords[:max_pages]:
            try:
                # 编码 URL
                url = f"https://baike.baidu.com/item/{quote(kw)}"
                if debug:
                    print(f"  正在访问: {kw} -> {url}")
                
                page.get(url)
                
                # 简单的反爬绕过：随机休眠一小会儿
                time.sleep(0.5)

                # 判断是否是有效词条
                if "error.html" in page.url or "search/none" in page.url:
                    if debug:
                        print(f"  ⚠️ 词条不存在或已失效")
                    continue

                # DrissionPage 查找元素
                # 优先找 .lemma-summary (摘要)，其次找 .main-content (正文)
                ele = page.ele('.lemma-summary')
                if not ele:
                    ele = page.ele('.main-content')
                
                if ele:
                    # 提取文本并清洗，增加长度限制到 2000 字符
                    text = ele.text[:2000].replace('\n', ' ').strip()
                    if text:
                        chunks.append(f"{kw}：{text}")
                        if debug:
                            print(f"  ✅ 获取成功 ({len(text)}字)")
                    else:
                        if debug:
                            print("  ❌ 内容为空")
                else:
                    if debug:
                        print("  ❌ 未找到摘要元素")
                    
            except Exception as e:
                if debug:
                    print(f"  ❌ 处理 '{kw}' 出错: {str(e)[:100]}")
                continue
                
    except Exception as e:
        if debug:
            print(f"  ❌ DrissionPage 致命错误: {str(e)[:100]}")
    finally:
        if page:
            try:
                page.quit()
                if debug:
                    print("  ✓ 浏览器已关闭")
            except Exception:
                pass
        
    return chunks


def _fetch_baike_chunks_requests(keywords: List[str], max_pages: int = 3, debug: bool = False) -> List[str]:
    """使用 requests 抓取百度百科摘要（回退方案）。
    """
    chunks: List[str] = []

    # 使用 Session 保持 Cookie
    session = requests.Session()
    
    # 更完整的浏览器 Headers，模拟真实浏览器
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }
    session.headers.update(headers)

    if debug:
        print(f"--- 开始抓取（requests 回退方案），关键词: {keywords} ---")
        print("  正在访问百度百科首页以获取 Cookie...")
    
    # 先访问首页获取 Cookie（模拟正常浏览流程）
    try:
        homepage_resp = session.get("https://baike.baidu.com/", timeout=10)
        if debug:
            print(f"  首页访问状态码: {homepage_resp.status_code}")
    except Exception as e:
        if debug:
            print(f"  ⚠️ 首页访问失败: {str(e)}")

    for kw in keywords[:max_pages]:
        encoded_kw = quote(kw, safe='')
        url = f"https://baike.baidu.com/item/{encoded_kw}"
        
        if debug:
            print(f"正在请求: {kw} -> {url}")

        try:
            resp = session.get(url, timeout=10, allow_redirects=True)
            
            if resp.status_code != 200:
                if debug:
                    print(f"  ❌ 请求失败，状态码: {resp.status_code}")
                continue

            resp.encoding = 'utf-8'
            
            # 检查是否被反爬
            if "wappass.baidu.com" in resp.url or "安全验证" in resp.text[:1000]:
                if debug:
                    print("  ⚠️ 触发百度安全验证")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # 处理重定向到搜索页
            if "search" in resp.url or "/none" in resp.url:
                if debug:
                    print(f"  ⚠️ 精确词条不存在，已重定向到搜索页")
                continue

            # 提取摘要 (尝试多种选择器)
            summary_block = (
                soup.select_one(".lemma-summary") 
                or soup.select_one('[data-module="lemmaSummary"]')
                or soup.select_one("div.lemma-summary")
                or soup.find("div", class_="lemma-summary")
            )

            text = ""
            if summary_block:
                text = " ".join(summary_block.stripped_strings)
            else:
                # 备选：如果没有摘要，提取正文前两段
                content_div = soup.select_one(".main-content")
                if content_div:
                    ps = content_div.find_all("p", limit=3)
                    text = " ".join([p.get_text().strip() for p in ps if p.get_text().strip()])

            # 增加长度限制，获取更完整的内容（从 800 增加到 2000 字符）
            text = text[:2000].strip()
            
            if text and len(text) > 20:
                if debug:
                    print(f"  ✅ 成功抓取摘要，长度: {len(text)} 字符")
                chunks.append(f"{kw}：{text}")
            else:
                if debug:
                    print("  ❌ 内容解析为空")

        except Exception as e:
            if debug:
                print(f"  ❌ 发生异常: {str(e)}")
            continue
    
    return chunks


def _fetch_full_baike_summary(baike_url: str, keyword: str, debug: bool = False) -> str:
    """从百度百科页面获取完整摘要
    
    优化点：
    1. 移除干扰元素（参考资料角标、编辑按钮等）
    2. 优先提取 .lemma-summary 内的完整段落
    3. 如果摘要不够长，自动向下抓取 .main-content 的前 3 个 .para 节点
    
    Args:
        baike_url: 百度百科页面 URL
        keyword: 关键词（用于验证）
        debug: 是否输出调试信息
    
    Returns:
        完整摘要文本，失败返回空字符串
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        
        # 确保 URL 完整
        if not baike_url.startswith("http"):
            baike_url = "https://" + baike_url.lstrip("/")
        
        resp = requests.get(baike_url, headers=headers, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # 1. 移除干扰元素（参考资料角标 [1-2]、编辑按钮等）
        for sup in soup.find_all(['sup', 'a'], class_=['patch-link', 'debug-link', 'lemma-reference']):
            sup.decompose()
        # 移除所有 sup 标签（通常是角标）
        for sup in soup.find_all('sup'):
            sup.decompose()
        
        # 2. 优先提取 .lemma-summary 内的完整段落
        summary_block = (
            soup.select_one(".lemma-summary") 
            or soup.select_one('[data-module="lemmaSummary"]')
            or soup.select_one("div.lemma-summary")
        )
        
        if summary_block:
            # 尝试提取段落结构（.para 节点）
            paragraphs = summary_block.find_all("div", class_="para")
            if paragraphs:
                # 提取每个段落的文本，确保完整
                full_text = "".join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])
            else:
                # 适配另一种结构：直接获取文本
                full_text = summary_block.get_text().strip()
            
            # 如果摘要不够长（少于 200 字），尝试向下抓取正文
            if len(full_text) < 200:
                content_div = soup.select_one(".main-content")
                if content_div:
                    # 提取前 3 个 .para 节点补充
                    para_nodes = content_div.find_all("div", class_="para", limit=3)
                    if para_nodes:
                        additional_text = "".join([p.get_text().strip() for p in para_nodes if p.get_text().strip()])
                        if additional_text:
                            full_text = full_text + additional_text
            
            # 限制长度到 2000 字符，确保内容有意义
            full_text = full_text[:2000].strip()
            if full_text and len(full_text) > 50:
                if debug:
                    print(f"    📖 从百度百科获取完整摘要 ({len(full_text)}字)")
                return full_text
        
        # 3. 如果没有摘要，尝试提取正文前几段
        content_div = soup.select_one(".main-content")
        if content_div:
            # 优先使用 .para 节点
            para_nodes = content_div.find_all("div", class_="para", limit=5)
            if para_nodes:
                text = "".join([p.get_text().strip() for p in para_nodes if p.get_text().strip()])
            else:
                # 回退到 p 标签
                ps = content_div.find_all("p", limit=5)
                text = " ".join([p.get_text().strip() for p in ps if p.get_text().strip()])
            
            text = text[:2000].strip()
            if text and len(text) > 50:
                if debug:
                    print(f"    📖 从正文提取摘要 ({len(text)}字)")
                return text
        
        return ""
    except Exception as e:
        if debug:
            print(f"    ⚠️ 获取完整摘要失败: {str(e)[:50]}")
        return ""


def _fetch_facts_via_bing(keywords: List[str], max_pages: int = 3, debug: bool = False) -> List[str]:
    """替代方案：通过 Bing 搜索抓取摘要（纯 Requests）
    
    优点：
    - 支持 VPN 环境：自动检测并切换 Bing 版本
    - 无需浏览器：直接用 requests 即可，速度极快，更轻量
    - 信息密度高：搜索结果摘要通常就是事实的浓缩
    
    注意：开启 VPN 时会自动使用国际版 Bing (bing.com)，否则使用国内版 (cn.bing.com)
    """
    chunks = []
    
    # 模拟真实浏览器 Header（添加更多头部以绕过检测）
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0"
    }
    
    if debug:
        print(f"--- 开始抓取（Bing 搜索聚合），关键词: {keywords} ---")
    
    # 检测可用的 Bing 域名（优先国内版，VPN 时自动切换国际版）
    bing_domains = ["https://cn.bing.com", "https://www.bing.com"]
    working_domain = None
    
    # 先测试哪个域名可用
    with requests.Session() as test_session:
        test_session.headers.update(headers)
        for domain in bing_domains:
            try:
                test_url = f"{domain}/search?q=test"
                resp = test_session.get(test_url, timeout=5, allow_redirects=False)
                # 如果返回 200 或 302（重定向到登录页也可接受），说明域名可用
                if resp.status_code in [200, 302]:
                    working_domain = domain
                    if debug:
                        print(f"  ✓ 检测到可用域名: {domain}")
                    break
            except Exception:
                continue
    
    # 如果都不可用，默认使用国内版
    if not working_domain:
        working_domain = bing_domains[0]
        if debug:
            print(f"  ⚠️ 无法检测可用域名，使用默认: {working_domain}")
    
    with requests.Session() as session:
        session.headers.update(headers)
        
        for kw in keywords[:max_pages]:
            try:
                # 改进搜索策略：优先搜索"关键词 百度百科"，如果失败再尝试其他方式
                search_queries = [
                    f'"{kw}" 百度百科',  # 精确匹配 + 百度百科（优先）
                    f"{kw} 百度百科",     # 普通搜索 + 百度百科
                    f'"{kw}"',            # 精确匹配（专有名词）
                    f"{kw} 简介",         # 普通搜索 + 简介
                ]
                
                snippets = []
                best_match = None
                
                for query_text in search_queries:
                    query = quote(query_text)
                    url = f"{working_domain}/search?q={query}"
                    
                    if debug:
                        print(f"  正在搜索: {kw} -> {query_text}")
                    
                    try:
                        resp = session.get(url, timeout=10, allow_redirects=True)
                        if resp.status_code != 200:
                            if debug:
                                print(f"  ⚠️ 状态码: {resp.status_code}，尝试下一个查询")
                            continue
                    except requests.exceptions.ProxyError:
                        if debug:
                            print(f"  ⚠️ 代理错误，可能需要配置代理")
                        continue
                    except requests.exceptions.Timeout:
                        if debug:
                            print(f"  ⚠️ 请求超时")
                        continue
                    except Exception as e:
                        if debug:
                            print(f"  ⚠️ 请求异常: {str(e)[:50]}")
                        continue
                    
                    # 检查是否被重定向到登录页或其他页面
                    if "account.microsoft.com" in resp.url or "login.live.com" in resp.url:
                        if debug:
                            print(f"  ⚠️ 被重定向到登录页，可能需要切换域名")
                        # 尝试切换域名
                        if working_domain == "https://cn.bing.com":
                            working_domain = "https://www.bing.com"
                            url = f"{working_domain}/search?q={query}"
                            try:
                                resp = session.get(url, timeout=10, allow_redirects=True)
                            except Exception:
                                continue
                        else:
                            continue
                    
                    soup = BeautifulSoup(resp.text, "html.parser")
                    results = soup.select("li.b_algo")
                    
                    # 如果使用国际版 Bing，选择器可能不同，尝试多个选择器
                    if not results:
                        results = soup.select(".b_algo") or soup.select(".b_searchResult") or soup.select("li")
                    
                    # 检查结果相关性：优先选择标题包含关键词的结果
                    baike_link_found = None
                    for res in results[:5]:  # 增加到前 5 条结果，提高找到百度百科的概率
                        # 检查标题是否包含关键词（提高相关性）
                        title_elem = res.select_one("h2 a") or res.select_one("h2") or res.select_one(".b_title a") or res.select_one("a")
                        title_text = title_elem.get_text().strip() if title_elem else ""
                        title_lower = title_text.lower()
                        kw_lower = kw.lower()
                        
                        # 标题关键词硬过滤：如果不包含关键词且没有"百度百科"字样，直接舍弃
                        is_baike_in_title = "百度百科" in title_text or "baike" in title_lower
                        if not is_baike_in_title and kw_lower not in title_lower:
                            # 跳过明显不相关的结果（如广告、时政新闻等）
                            continue
                        
                        # 优先检查是否是百度百科链接
                        is_baike = "baike.baidu.com" in str(res)
                        if is_baike:
                            for link in res.select("a"):
                                href = link.get("href", "")
                                if "baike.baidu.com/item" in href:
                                    baike_link_found = href
                                    if debug:
                                        print(f"    🔗 发现百度百科链接: {href}")
                                    break
                        
                        # 提取摘要（支持多种选择器）
                        desc = (res.select_one("p") or 
                               res.select_one(".b_caption p") or 
                               res.select_one(".b_caption") or
                               res.select_one(".b_snippet"))
                        
                        if desc:
                            text = desc.get_text().strip()
                            # 过滤掉明显不相关的内容
                            if text and len(text) > 20:
                                text_lower = text.lower()
                                
                                # 过滤掉明显不相关的内容（学生作业、论坛信息等）
                                if any(noise in text_lower for noise in [
                                    "刚好", "老师布置了", "作业", "见笑", "个人感想",
                                    "论坛", "高峰论坛", "会议", "研讨会", "报名",
                                    "点击", "查看更多", "立即下载", "广告"
                                ]):
                                    continue  # 跳过不相关内容
                                
                                # 域名白名单：百度百科内容优先
                                if is_baike:
                                    # 百度百科内容优先，但先不设置 best_match，等待完整抓取
                                    if not best_match:
                                        snippets.append(text)
                                # 如果标题或摘要包含关键词，优先采用
                                elif kw_lower in title_lower or kw_lower in text_lower[:100]:
                                    if best_match is None or kw_lower in title_lower:
                                        best_match = text
                                        snippets = [text]  # 找到最佳匹配，重置列表
                                elif not best_match:  # 如果没有找到精确匹配，保存第一个结果
                                    snippets.append(text)
                    
                    # 逻辑前置：只要看到百科链接，就强制执行一次正文抓取
                    if baike_link_found:
                        try:
                            baike_text = _fetch_full_baike_summary(baike_link_found, kw, debug)
                            if baike_text and len(baike_text) > 50:
                                # 百度百科内容优先，覆盖搜索结果的残缺片段
                                best_match = baike_text
                                snippets = [baike_text]
                                if debug:
                                    print(f"    ✅ 强制执行百度百科抓取，覆盖搜索结果 ({len(baike_text)}字)")
                                # 找到完整百科内容后，直接跳出查询循环
                                break
                        except Exception as e:
                            if debug:
                                print(f"    ⚠️ 获取百度百科内容失败: {str(e)[:50]}")
                            # 如果访问失败，继续使用搜索结果摘要，不跳出循环
                    
                    # 如果找到最佳匹配（包括百度百科完整内容），停止搜索其他查询
                    if best_match:
                        break
                    
                    time.sleep(random.uniform(0.5, 1.0))  # 查询间随机延时，避免被风控
                
                # 去重并合并（在查询循环结束后）
                if snippets:
                    # 如果找到最佳匹配，使用它；否则合并所有结果
                    if best_match:
                        combined_text = best_match
                    else:
                        # 去重：移除相似内容
                        unique_snippets = []
                        for s in snippets:
                            if not any(s[:50] == u[:50] for u in unique_snippets):
                                unique_snippets.append(s)
                        combined_text = "；".join(unique_snippets[:3])  # 最多合并 3 条，增加信息量
                    
                    # 增加长度限制，允许更完整的内容（从 800 增加到 2000 字符）
                    combined_text = combined_text[:2000].strip()
                    chunks.append(f"{kw}：{combined_text}")
                    if debug:
                        print(f"  ✅ 获取成功 ({len(combined_text)}字，{'精确匹配' if best_match else '聚合结果'})")
                else:
                    if debug:
                        print("  ❌ 未找到有效摘要")
                    
                time.sleep(1)  # 关键词间礼貌延时
                
            except Exception as e:
                if debug:
                    print(f"  ❌ Bing 搜索出错: {str(e)[:100]}")
                continue
                
    return chunks


def _fetch_baike_chunks(keywords: List[str], max_pages: int = 3, debug: bool = False) -> List[str]:
    """抓取事实摘要（多种方案自动回退）。
    
    优先级：Bing 搜索聚合 > DrissionPage > requests
    
    推荐使用 Bing 搜索聚合方案：
    - 最快、最轻量（纯 requests，无需浏览器）
    - 无需梯子（Bing 国内版可直连）
    - 信息密度高（搜索结果摘要就是事实浓缩）
    
    Args:
        keywords: 关键词列表
        max_pages: 最多抓取几个关键词
        debug: 是否打印调试信息
    
    Returns:
        摘要文本列表
    """
    # 优先尝试 Bing 搜索聚合方案（最快、最轻量、无需梯子）
    if debug:
        print("  📌 尝试 Bing 搜索聚合方案（推荐，最快最轻量）...")
    chunks = _fetch_facts_via_bing(keywords, max_pages, debug)
    if chunks:
        return chunks
    if debug:
        print("  ⚠️ Bing 搜索方案未获取到数据，尝试浏览器方案...")
    
    # 尝试 DrissionPage 方案（最强的反爬能力）
    if _HAS_DRISSION:
        if debug:
            print("  📌 使用 DrissionPage 方案（最强反爬）")
        chunks = _fetch_baike_chunks_drission(keywords, max_pages, debug)
        if chunks:
            return chunks
        if debug:
            print("  ⚠️ DrissionPage 方案未获取到数据，回退到 requests 方案...")
    
    # 回退到 requests 方案（直接抓取百度百科）
    if debug:
        print("  📌 使用 requests 方案（回退）")
    return _fetch_baike_chunks_requests(keywords, max_pages, debug)


# -------------------------
# Qwen-plus 脱水成事实清单
# -------------------------
def _distill_facts_with_qwen(
    topic: str,
    raw_chunks: List[str],
    source_hint: Optional[Dict[str, str]] = None,
    debug: bool = False,
) -> Optional[List[FactItem]]:
    """使用 Qwen 将百科摘录脱水成硬核事实清单。
    
    Args:
        topic: 主题
        raw_chunks: 原始百科摘录列表
        source_hint: 来源提示（用于为事实补充来源信息）
        debug: 是否输出调试信息
    
    Returns:
        事实清单，失败返回 None
    """
    client = _llm_client()
    if client is None:
        if debug:
            print("  ❌ LLM 客户端未初始化（请检查 API Key）")
        return None
    if not raw_chunks:
        if debug:
            print("  ❌ 没有可用的百科数据")
        return None

    raw_text = "\n\n".join(raw_chunks)
    # 增加长度限制，允许更多内容传递给 LLM（从 6000 增加到 10000 字符）
    raw_text = raw_text[:10000]

    try:
        prompt_template = load_prompt_user("step1_facts.yaml")
        system_message = load_prompt_system("step1_facts.yaml")
    except (FileNotFoundError, ImportError):
        # 回退到硬编码提示词（兼容性）
        prompt_template = (
            "请基于以下中文资料摘录（可能来自维基百科、百度百科、Google/Bing 摘要等），提取与主题相关的硬核事实。\n"
            "输出格式：JSON 数组，每个元素包含："
            '{"title": "事实标题", "summary": "事实摘要", "sources": ["来源"], "source_site": "wikipedia/baike", "source_engine": "google/bing/baidu", "source_link": "原始链接"}\n\n'
            "要求：\n"
            "- 优先使用维基百科的内容作为事实依据，如果只有英文内容请输出中文翻译\n"
            "- 优先提取与主题直接相关的事实（人物、事件、时间、地点、关键数据）\n"
            "- 如果摘录内容与主题不完全匹配，请尝试提取其中可能相关的信息，或基于常识补充基本事实\n"
            "- 摘要应包含时间、地点、关键人物/机构等关键信息\n"
            "- 如果确实无法提取有效事实，返回空数组 []\n"
            "- 至少尝试提取 1-3 条事实，即使信息不完整\n\n"
            "主题：{topic}\n\n"
            "【百科摘录】\n{raw_text}\n\n"
            "只输出 JSON 数组，不要其他说明文字。"
        )
        system_message = "你是历史研究助理，只输出符合要求的 JSON。"
    
    prompt = format_prompt(prompt_template, topic=topic, raw_text=raw_text)

    try:
        if debug:
            print(f"  📤 调用 Qwen API (模型: {CONFIG.llm.model_research})...")
        
        resp = client.chat.completions.create(
            model=CONFIG.llm.model_research,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
        )
        
        if not resp or not resp.choices:
            if debug:
                print("  ❌ API 返回为空")
            return None
        
        content = resp.choices[0].message.content or ""
        
        if debug:
            print(f"  📥 API 返回内容长度: {len(content)} 字符")
            print(f"  📥 原始内容预览: {content[:300]}...")
            # 如果返回空数组，输出输入的百科摘录预览，帮助诊断
            if content.strip() == "[]":
                print(f"  ⚠️ Qwen 返回空数组，输入的百科摘录预览:")
                print(f"     {raw_text[:500]}...")
        
        if not content:
            if debug:
                print("  ❌ API 返回内容为空")
            return None
        
        # 尝试提取 JSON 数组（可能包含 markdown 代码块）
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
        if content.startswith("```json"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
        
        # 尝试找到 JSON 数组部分
        start_idx = content.find("[")
        end_idx = content.rfind("]")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            content = content[start_idx : end_idx + 1]
        else:
            if debug:
                print(f"  ⚠️ 未找到 JSON 数组标记，尝试直接解析...")
        
        if debug:
            print(f"  🔍 提取后的 JSON 内容: {content[:300]}...")
        
        data = json.loads(content)
        facts: List[FactItem] = []
        hint_engine = (source_hint or {}).get("engine", "") if source_hint else ""
        hint_site = (source_hint or {}).get("site", "") if source_hint else ""
        hint_link = (source_hint or {}).get("link", "") if source_hint else ""
        
        # 处理不同的返回格式
        if isinstance(data, list):
            # 标准格式：列表
            for item in data:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "")).strip()
                summary = str(item.get("summary", "")).strip()
                sources_raw = item.get("sources", []) or []
                sources = [str(s).strip() for s in sources_raw if str(s).strip()]
                if title and summary:
                    facts.append(
                        FactItem(
                            title=title,
                            summary=summary,
                            sources=sources or ["WIKI/BAIKE"],
                            source_engine=str(item.get("source_engine", "") or hint_engine),
                            source_site=str(item.get("source_site", "") or hint_site),
                            source_link=str(item.get("source_link", "") or hint_link),
                            translated_from=str(item.get("translated_from", "")),
                        )
                    )
        elif isinstance(data, dict):
            # 如果返回的是单个字典，尝试提取
            if "title" in data and "summary" in data:
                title = str(data.get("title", "")).strip()
                summary = str(data.get("summary", "")).strip()
                sources_raw = data.get("sources", []) or []
                sources = [str(s).strip() for s in sources_raw if str(s).strip()]
                if title and summary:
                    facts.append(
                        FactItem(
                            title=title,
                            summary=summary,
                            sources=sources or ["WIKI/BAIKE"],
                            source_engine=str(data.get("source_engine", "") or hint_engine),
                            source_site=str(data.get("source_site", "") or hint_site),
                            source_link=str(data.get("source_link", "") or hint_link),
                            translated_from=str(data.get("translated_from", "")),
                        )
                    )
            elif debug:
                print(f"  ⚠️ 返回的是字典，但格式不符合预期: {list(data.keys())}")
        
        if facts:
            if debug:
                print(f"  ✅ 成功提取 {len(facts)} 条事实")
            return facts
        else:
            if debug:
                print("  ⚠️ 解析后未找到有效事实（可能是 JSON 格式不匹配或内容不相关）")
                if isinstance(data, list) and len(data) == 0:
                    print("  💡 提示：Qwen 返回了空数组，可能是因为输入的百科摘录与主题不匹配")
            return None
            
    except json.JSONDecodeError as e:
        if debug:
            print(f"  ❌ JSON 解析失败: {str(e)}")
            print(f"  📥 尝试解析的内容: {content[:500] if 'content' in locals() else 'N/A'}")
        return None
    except Exception as e:
        if debug:
            print(f"  ❌ API 调用异常: {type(e).__name__}: {str(e)}")
        return None


# -------------------------
# 动态生成大纲
# -------------------------
def _gen_outline_with_qwen(topic: str, facts: List[FactItem], debug: bool = False) -> Optional[List[str]]:
    """根据选题和事实清单，使用 Qwen 生成脚本大纲。
    
    Args:
        topic: 主题
        facts: 事实清单
        debug: 是否输出调试信息
    
    Returns:
        大纲章节列表，失败返回 None
    """
    client = _llm_client()
    if client is None:
        if debug:
            print("  ❌ LLM 客户端未初始化（请检查 API Key）")
        return None
    
    if not facts:
        if debug:
            print("  ❌ 没有可用的历史事实（Step 1.3 失败）")
        return None
    
    facts_summary = "\n".join([f"- {f.title}: {f.summary}" for f in facts[:5]])
    
    try:
        prompt_template = load_prompt_user("step1_outline.yaml")
        system_message = load_prompt_system("step1_outline.yaml")
    except (FileNotFoundError, ImportError):
        # 回退到硬编码提示词（兼容性）
        prompt_template = (
            "请根据以下选题和硬核事实清单，生成一个适合 B 站视频脚本的 4-6 个章节大纲。\n"
            "要求：\n"
            "- 每个章节标题简洁明了（10字以内）\n"
            "- 符合叙事逻辑：开场引入 → 发展过程 → 关键转折 → 当代影响/反思\n"
            "- 输出为 JSON 数组，例如：[\"开场引入\", \"发家史\", \"关键转折\", \"当代影响\"]\n\n"
            "选题：{topic}\n\n"
            "事实清单：\n{facts_summary}\n\n"
            "只输出 JSON 数组，不要其他说明。"
        )
        system_message = "你是脚本结构规划师，只输出 JSON 数组。"
    
    prompt = format_prompt(prompt_template, topic=topic, facts_summary=facts_summary)
    
    try:
        if debug:
            print(f"  📤 调用 Qwen API (模型: {CONFIG.llm.model_research})...")
        
        resp = client.chat.completions.create(
            model=CONFIG.llm.model_research,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
        )
        
        if not resp or not resp.choices:
            if debug:
                print("  ❌ API 返回为空")
            return None
        
        content = resp.choices[0].message.content or ""
        
        if debug:
            print(f"  📥 API 返回内容长度: {len(content)} 字符")
            print(f"  📥 原始内容预览: {content[:200]}...")
        
        if not content:
            if debug:
                print("  ❌ API 返回内容为空")
            return None
        
        # 提取 JSON 数组
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
        if content.startswith("```json"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if len(lines) > 2 else content
        
        start_idx = content.find("[")
        end_idx = content.rfind("]")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            content = content[start_idx : end_idx + 1]
        else:
            if debug:
                print(f"  ⚠️ 未找到 JSON 数组标记，尝试直接解析...")
        
        if debug:
            print(f"  🔍 提取后的 JSON 内容: {content[:300]}...")
        
        data = json.loads(content)
        if isinstance(data, list) and len(data) >= 3:
            outline = [str(x).strip() for x in data if str(x).strip()]
            if outline:
                if debug:
                    print(f"  ✅ 成功生成 {len(outline)} 个章节大纲")
                return outline
        
        if debug:
            print(f"  ⚠️ 解析后未找到有效大纲（要求至少 3 个章节，实际: {len(data) if isinstance(data, list) else 0}）")
        return None
        
    except json.JSONDecodeError as e:
        if debug:
            print(f"  ❌ JSON 解析失败: {str(e)}")
            print(f"  📥 尝试解析的内容: {content[:500] if 'content' in locals() else 'N/A'}")
        return None
    except Exception as e:
        if debug:
            print(f"  ❌ API 调用异常: {type(e).__name__}: {str(e)}")
        return None


# -------------------------
# 主入口
# -------------------------
def research_topic(topic: str, use_proxy: Optional[bool] = None, debug: bool = False) -> ResearchResult:
    # 默认大纲（如果动态生成失败，使用这个）
    default_outline = [
        "开场与人物/事件引入",
        "发家史或早期发展阶段",
        "关键转折点与黑历史",
        "当代延续与讽刺意味",
    ]

    proxy_on = _proxy_enabled() if use_proxy is None else use_proxy
    keywords = _gen_baike_keywords(topic) or [topic]

    # 1) 维基百科优先
    wiki_blocks = _fetch_wikipedia_chunks(keywords, max_pages=len(keywords), debug=debug) if CONFIG.search.prefer_wikipedia else []
    wiki_chunks = [b["text"] for b in wiki_blocks]

    # 2) 搜索摘要（Google -> Baidu/Bing）
    search_chunks: List[str] = []
    source_hint = {"engine": "google" if proxy_on else "baidu", "site": "", "link": ""}

    if proxy_on:
        search_chunks = _fetch_google_snippets(keywords, max_pages=len(keywords), debug=debug)
        if search_chunks:
            source_hint["engine"] = "google"
    if not search_chunks:
        # 无代理或 Google 失败时，走百度/Bing 聚合 + 百科回退
        search_chunks = _fetch_baike_chunks(keywords, max_pages=len(keywords), debug=debug)
        source_hint.setdefault("engine", "baidu")
        if not source_hint.get("site"):
            source_hint["site"] = "baike"
        source_hint.setdefault("link", "https://baike.baidu.com")

    # 3) 汇总并去重
    baike_chunks = wiki_chunks + search_chunks
    
    # 结果去重优化：去除重复的百科摘录，节省 Token
    # 使用 set 去重，但保留顺序（Python 3.7+ dict 保持插入顺序）
    seen = set()
    unique_chunks = []
    for chunk in baike_chunks:
        if chunk not in seen:
            seen.add(chunk)
            unique_chunks.append(chunk)
    baike_chunks = unique_chunks
    
    # 如果维基百科有命中，补充来源提示
    if wiki_blocks and not source_hint.get("site"):
        source_hint["site"] = "wikipedia"
        source_hint["link"] = wiki_blocks[0].get("url", "")

    facts = _distill_facts_with_qwen(topic, baike_chunks, source_hint=source_hint, debug=debug) or []

    # 根据事实清单动态生成大纲（如果没有事实，使用默认大纲）
    if facts:
        outline = _gen_outline_with_qwen(topic, facts) or default_outline
    else:
        outline = default_outline

    return ResearchResult(topic=topic, outline=outline, facts=facts)
