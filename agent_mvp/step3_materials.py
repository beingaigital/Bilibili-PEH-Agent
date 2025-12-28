"""
Step 3: 素材匹配

真实环境：
- 基于脚本中的占位符关键词，调用 Bilibili 搜索 API 检索高热度素材

当前实现：

- 完全使用 bilibili-api-python 库（内置 WBI 算法和风控对抗，稳定可靠）
- 获取可播放的直链 URL（mp4/flv）
- 三重兜底机制：重试、限频、降级
"""

import os
import random
import re
import time
from typing import List, Optional

# 优先尝试从 .env 加载环境变量（如果安装了 python-dotenv）
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    # 没装也没关系，只用系统环境变量
    pass

from .data_types import ScriptDraft, MaterialItem, MaterialMatchResult
from .config import CONFIG
from .prompt_loader import load_prompt_user, load_prompt_system, format_prompt

# 可选的 YouTube 搜索爬虫
try:
    from youtubesearchpython import VideosSearch  # type: ignore

    _HAS_YOUTUBE_SCRAPER = True
except Exception:
    VideosSearch = None  # type: ignore
    _HAS_YOUTUBE_SCRAPER = False

# 尝试导入 OpenAI（用于调用 Qwen-plus）
try:
    from openai import OpenAI  # type: ignore
    _HAS_OPENAI = True
except Exception:
    OpenAI = None  # type: ignore
    _HAS_OPENAI = False

# 尝试导入 bilibili-api-python（必须）
try:
    import asyncio
    from bilibili_api import search, video  # type: ignore
    try:
        from bilibili_api import Credential  # type: ignore
    except ImportError:
        from bilibili_api.credential import Credential  # type: ignore
    _HAS_BILIBILI_API = True
    _HAS_ASYNCIO = True
except Exception as e:
    _HAS_BILIBILI_API = False
    _HAS_ASYNCIO = False
    search = None  # type: ignore
    video = None  # type: ignore
    Credential = None  # type: ignore
    asyncio = None  # type: ignore
    print("❌ 错误：bilibili-api-python 库导入失败")
    print(f"   错误类型: {type(e).__name__}")
    print(f"   错误详情: {e}")
    print("   请检查：")
    print("   1. 是否已安装: pip install bilibili-api-python httpx")
    print("   2. Python 环境是否正确（可能有多个 Python 环境）")
    print("   3. 库版本是否兼容（建议 >=6.0.0）")

import requests


def _sync_run(coro):
    """同步运行异步函数的辅助函数"""
    if not _HAS_ASYNCIO:
        return None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ================= 配置区 =================
# 如果你愿意提供 Cookie (SESSDATA)，搜索成功率是 100%。
# 如果留空，库会尝试游客模式，但在高频请求下可能也会 412。
# 获取方法：浏览器登录 B 站 -> F12 -> Application -> Cookies -> SESSDATA
# 也可以通过环境变量 BILIBILI_SESSDATA 和 BILIBILI_BUVID3 设置
SESSDATA = os.getenv("BILIBILI_SESSDATA", "")
SCK = os.getenv("BILIBILI_BUVID3", "")  # 可选，buvid3

# 初始化凭证（如果提供了 SESSDATA）
credential = Credential(sessdata=SESSDATA, buvid3=SCK) if (_HAS_BILIBILI_API and SESSDATA) else None
YOUTUBE_API_KEY = CONFIG.youtube.api_key


# ================= 核心工具函数 =================

def _extract_keywords_with_llm(placeholder: str, debug: bool = False) -> str | None:
    """使用 Qwen-plus 从占位符中提取精准的关键词
    
    Args:
        placeholder: 占位符文本，如 "[画面：历代首相办公桌下伸出一只戴戒指的手，戒指刻"ASO"]"
        debug: 是否输出调试信息
    
    Returns:
        提取的关键词字符串，失败返回 None
    """
    if not _HAS_OPENAI:
        if debug:
            print("    ⚠️ OpenAI 库未安装，无法使用 LLM 提取关键词")
        return None
    
    api_key = CONFIG.llm.api_key
    if not api_key:
        if debug:
            print("    ⚠️ API Key 未配置，无法使用 LLM 提取关键词")
        return None
    
    try:
        # 加载 prompt
        try:
            system_message = load_prompt_system("step3_keywords.yaml")
            prompt_template = load_prompt_user("step3_keywords.yaml")
        except (FileNotFoundError, ImportError):
            # 回退到硬编码 prompt
            system_message = "你是视频素材关键词提取专家，擅长从画面描述中提取精准的搜索关键词。"
            prompt_template = """请从以下画面占位符描述中，提取出最适合在 B 站搜索视频素材的 1-3 个核心关键词。

【要求】
1. 提取核心实体：优先提取人名、地名、机构名、事件名、专有名词等实体
2. 去除修饰词：去掉"缓缓"、"慢慢"、"特写"、"镜头"等画面描述词
3. 保留关键信息：保留能代表画面核心内容的词汇
4. 搜索友好：关键词应该是在 B 站容易搜索到的内容
5. 长度限制：单个关键词不超过 10 字，总长度不超过 15 字
6. 输出格式：直接输出关键词，用空格分隔，不要其他说明

【占位符描述】
{placeholder}

只输出关键词，不要其他文字。"""
        
        prompt = format_prompt(prompt_template, placeholder=placeholder)
        
        if debug:
            print(f"    🤖 使用 Qwen-plus 提取关键词...")
        
        client = OpenAI(  # type: ignore[call-arg]
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        
        completion = client.chat.completions.create(
            model=CONFIG.llm.model_research,  # 使用 research 模型
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,  # 降低温度，提高确定性
            max_tokens=50,  # 关键词很短，不需要太多 token
        )
        
        content = completion.choices[0].message.content or ""
        content = content.strip()
        
        # 清理可能的格式标记
        content = content.replace("关键词：", "").replace("关键词:", "")
        content = content.replace("输出：", "").replace("输出:", "")
        
        # 限制长度
        if len(content) > 15:
            # 如果超过15字，取前几个词
            words = content.split()
            content = " ".join(words[:3])[:15]
        
        if debug:
            print(f"    ✅ LLM 提取关键词: {content}")
        
        return content if content else None
        
    except Exception as e:
        if debug:
            print(f"    ⚠️ LLM 提取关键词失败: {type(e).__name__}: {str(e)[:100]}")
        return None


def _extract_keywords_simple(text: str) -> str:
    """简单的关键词提取（降级方案），从占位符中拿核心词"""
    clean = text.replace("[", "").replace("]", "").replace("画面：", "").replace("素材占位：", "")
    # 简单处理：如果有冒号，取冒号后；否则取全部
    if "：" in clean:
        clean = clean.split("：")[-1]
    elif ":" in clean:
        clean = clean.split(":")[-1]
    
    # 截取前 15 个字作为搜索词，太长搜不到
    return clean.strip()[:15]


def _extract_keywords(placeholder: str, debug: bool = False) -> str:
    """提取关键词（优先使用 LLM，失败则降级到简单提取）
    
    Args:
        placeholder: 占位符文本
        debug: 是否输出调试信息
    
    Returns:
        提取的关键词
    """
    # 优先使用 LLM 提取
    llm_keyword = _extract_keywords_with_llm(placeholder, debug=debug)
    if llm_keyword:
        return llm_keyword
    
    # 降级到简单提取
    if debug:
        print("    ⚠️ 使用简单提取方法（降级）")
    return _extract_keywords_simple(placeholder)


def _get_play_url_safe(bvid: str, debug: bool = False) -> str:
    """获取播放直链（使用库封装的方法）
    
    注意：
    - 此链接有 Referer 防盗链，使用时必须设置 Header: 'Referer: https://www.bilibili.com'
    - 链接通常有有效期（几小时），不可永久存储
    - 优先返回 DURL 格式（包含音频），其次返回 DASH 格式（视频流）
    """
    if not _HAS_BILIBILI_API:
        if debug:
            print("    ⚠️ bilibili-api-python 未安装")
        return ""
    
    if not bvid:
        return ""
    
    try:
        v = video.Video(bvid=bvid, credential=credential)
        # page_index=0 取 P1
        # 获取下载地址信息，自动处理签名（异步转同步）
        download_url_data = _sync_run(v.get_download_url(page_index=0))
        
        if not download_url_data:
            if debug:
                print(f"    ⚠️ 未获取到下载地址信息")
            return ""
        
        # 优先解析 durl 格式 (传统的 flv/mp4 单文件，包含声音)
        # 对于画面素材匹配，完整文件更方便使用
        real_url = ""
        format_type = ""
        
        if 'durl' in download_url_data and len(download_url_data['durl']) > 0:
            durl_stream = download_url_data['durl'][0]
            real_url = durl_stream.get('url', '')
            format_type = "DURL (MP4/FLV, Contains Audio)"
        
        # 其次解析 dash 格式 (通常是 m4s/mp4，现在的标准格式)
        # Dash 包含 video 和 audio 分离流，这里只取视频流
        if not real_url and 'dash' in download_url_data:
            if 'video' in download_url_data['dash'] and len(download_url_data['dash']['video']) > 0:
                video_stream = download_url_data['dash']['video'][0]
                real_url = video_stream.get('baseUrl', '')
                format_type = "DASH (Video Only, Need Audio Merge)"
        
        if real_url:
            if debug:
                print(f"    ✅ 获取成功 ({format_type})")
                print(f"    💡 提示：链接有 Referer 防盗链，使用时需设置 Header: 'Referer: https://www.bilibili.com'")
            return real_url
            
        if debug:
            print(f"    ⚠️ 未找到播放地址")
        return ""
    except Exception as e:
        if debug:
            print(f"    ❌ 获取播放链失败 ({bvid}): {str(e)[:50]}")
        return ""


def _search_bilibili_safe(keyword: str, max_results: int = 1, debug: bool = False) -> List[dict]:
    """使用 bilibili-api 库进行搜索（使用 search_by_type，只搜索视频类型）"""
    if not _HAS_BILIBILI_API:
        if debug:
            print("    ⚠️ bilibili-api-python 未安装")
        return []
    
    if not keyword or not keyword.strip():
        return []
    
    try:
        # 使用 search_by_type 直接搜索视频，过滤掉广告、游戏等干扰项
        # 注意：部分旧版本 bilibili-api 不支持 credential 参数，这里只传必要参数
        # search_type=search.SearchObjectType.VIDEO 指定只搜视频
        # order_type=search.OrderVideo.TOTALRANK 综合排序
        res = _sync_run(
            search.search_by_type(
                keyword,
                search_type=search.SearchObjectType.VIDEO,
                order_type=search.OrderVideo.TOTALRANK,
                page_size=max_results,
            )
        )
        
        if not res or not isinstance(res, dict):
            if debug:
                print(f"    ⚠️ 搜索返回空结果或格式错误")
            return []
        
        # 获取视频结果列表（search_by_type 返回的 result 已经是视频列表）
        video_list = res.get('result', [])
        if not video_list or not isinstance(video_list, list):
            if debug:
                print(f"    ⚠️ 未找到相关视频")
            return []
        
        if debug:
            print(f"    ✓ 找到 {len(video_list)} 个视频结果")
            
        results = []
        for item in video_list[:max_results]:
            # 这里的 item 结构由 API 决定，通常包含 title, bvid, play, author 等
            # 注意：HTML 标签需要清洗
            raw_title = item.get('title', '')
            # 清理 HTML 标签
            raw_title = re.sub(r'<[^>]+>', '', raw_title)
            raw_title = raw_title.replace('<em class="keyword">', '').replace('</em>', '')
            
            # 处理封面图 URL
            pic = item.get('pic', '')
            if pic and pic.startswith('//'):
                pic = 'http:' + pic
            elif pic and not pic.startswith('http'):
                pic = 'https:' + pic if pic.startswith(':') else pic
            
            video_info = {
                "bvid": item.get('bvid', ''),
                "title": raw_title,
                "author": item.get('author', '') or item.get('up_name', ''),
                "play": item.get('play', 0) or item.get('play_count', 0) or 0,
                "description": (item.get('description', '') or item.get('desc', ''))[:50] + "...",
                "pic": pic
            }
            results.append(video_info)
            
        return results

    except Exception as e:
        if debug:
            print(f"    ⚠️ 搜索 API 异常: {type(e).__name__}: {str(e)[:100]}")
        return []


def _search_youtube_api(keyword: str, max_results: int = 1, debug: bool = False) -> List[dict]:
    """使用 YouTube Data API v3 搜索视频（需要 YOUTUBE_API_KEY）。"""
    if not YOUTUBE_API_KEY:
        return []
    if not keyword:
        return []
    try:
        search_resp = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "type": "video",
                "q": keyword,
                "maxResults": max_results,
                "order": "relevance",
                "key": YOUTUBE_API_KEY,
            },
            timeout=10,
        )
        if search_resp.status_code != 200:
            if debug:
                print(f"    ⚠️ YouTube API 状态码: {search_resp.status_code}")
            return []
        data = search_resp.json()
        items = data.get("items", []) or []
        if not items:
            return []

        video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
        stats_map = {}
        if video_ids:
            stats_resp = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "part": "statistics",
                    "id": ",".join(video_ids),
                    "key": YOUTUBE_API_KEY,
                },
                timeout=10,
            )
            if stats_resp.status_code == 200:
                stats_data = stats_resp.json().get("items", []) or []
                for stat_item in stats_data:
                    vid = stat_item.get("id")
                    view_count = int(stat_item.get("statistics", {}).get("viewCount", "0") or 0)
                    stats_map[vid] = view_count

        results = []
        for item in items[:max_results]:
            vid = item.get("id", {}).get("videoId")
            snippet = item.get("snippet", {}) or {}
            if not vid:
                continue
            title = snippet.get("title", "YouTube 视频")
            channel = snippet.get("channelTitle", "")
            url = f"https://www.youtube.com/watch?v={vid}"
            embed_url = f"https://www.youtube.com/embed/{vid}"
            view_count = stats_map.get(vid, 0)
            results.append(
                {
                    "video_id": vid,
                    "title": title,
                    "channel": channel,
                    "view": view_count,
                    "url": url,
                    "embed_url": embed_url,
                }
            )
        if debug and results:
            print(f"    ✓ YouTube API 返回 {len(results)} 条结果")
        return results
    except Exception as e:
        if debug:
            print(f"    ⚠️ YouTube API 搜索异常: {type(e).__name__}: {str(e)[:100]}")
        return []


def _search_youtube_scrape(keyword: str, max_results: int = 1, debug: bool = False) -> List[dict]:
    """无 API Key 的情况下用轻量库抓取 YouTube 搜索结果。"""
    if not _HAS_YOUTUBE_SCRAPER:
        return []
    try:
        searcher = VideosSearch(keyword, limit=max_results)
        res = searcher.result()
        items = res.get("result", []) or []
        results = []
        for item in items[:max_results]:
            vid = item.get("id")
            if not vid:
                continue
            title = item.get("title", "YouTube 视频")
            channel = item.get("channel", {}).get("name", "")
            view_str = item.get("viewCount", {}).get("text", "0")
            # 解析播放量
            try:
                view_count = int(view_str.replace(",", "").replace("次观看", "").replace("views", "").strip())
            except Exception:
                view_count = 0
            url = item.get("link") or f"https://www.youtube.com/watch?v={vid}"
            embed_url = f"https://www.youtube.com/embed/{vid}"
            results.append(
                {
                    "video_id": vid,
                    "title": title,
                    "channel": channel,
                    "view": view_count,
                    "url": url,
                    "embed_url": embed_url,
                }
            )
        if debug and results:
            print(f"    ✓ YouTube 爬虫返回 {len(results)} 条结果")
        return results
    except Exception as e:
        if debug:
            print(f"    ⚠️ YouTube 爬虫异常: {type(e).__name__}: {str(e)[:100]}")
        return []


# ================= 主逻辑 =================

def match_materials(script: ScriptDraft, use_youtube: bool = True, debug: bool = False) -> MaterialMatchResult:
    """重构后的匹配主流程
    
    策略：
    1. 使用 bilibili-api-python 库搜索（内置 WBI 签名，稳定可靠）
    2. 如果没搜到，尝试缩短关键词重试
    3. 获取播放直链（dash 格式优先）
    4. 限频：每次搜索后等待 1.5 秒
    5. 降级：搜索失败时使用模拟数据
    
    Args:
        script: 脚本草稿
        debug: 是否输出调试信息
    6. 可选同时搜索 YouTube 并择优返回
    """
    if not _HAS_BILIBILI_API:
        print("❌ 错误：bilibili-api-python 未安装")
        print("   请运行: pip install bilibili-api-python httpx")
        # 返回空结果，避免程序崩溃
        return MaterialMatchResult(topic=script.topic, materials=[])
    
    materials: List[MaterialItem] = []
    
    if debug:
        print(f"🚀 开始素材匹配，共 {len(script.placeholders)} 个占位符")
        print(f"🔧 运行模式: {'已配置 SESSDATA (稳定)' if credential else '游客模式 (可能限流)'}")

    for idx, placeholder in enumerate(script.placeholders, start=1):
        if debug:
            print(f"\n🔍 [{idx}/{len(script.placeholders)}] 处理: {placeholder}")
        
        # 使用 LLM 提取关键词（优先），失败则降级到简单提取
        keyword = _extract_keywords(placeholder, debug=debug)
        
        bili_candidate: Optional[MaterialItem] = None
        yt_candidate: Optional[MaterialItem] = None

        # 1. 尝试 B 站搜索
        search_res = _search_bilibili_safe(keyword, max_results=1, debug=debug)
        
        # 2. 如果没搜到，尝试拆词（简单拆分前半部分）
        if not search_res and len(keyword) > 4:
            short_keyword = keyword[:4]
            if debug: 
                print(f"    🔄 缩短关键词重试: {short_keyword}")
            search_res = _search_bilibili_safe(short_keyword, max_results=1, debug=debug)
            
        if search_res:
            top_video = search_res[0]
            video_title = top_video.get('title', '未知标题')
            video_author = top_video.get('author', '未知UP主')
            if debug: 
                print(f"    ✅ 命中视频: {video_title[:30]}... (UP: {video_author})")
            
            bvid = top_video.get('bvid', '')
            play_url = ""
            if bvid:
                if debug:
                    print(f"    正在获取播放直链: {bvid}")
                play_url = _get_play_url_safe(bvid, debug=debug)
                if play_url and debug:
                    print(f"    ✅ 成功获取播放直链")
            play_count = top_video.get('play', 0)
            reason_parts = ["匹配度高", "平台：B站"]
            if play_count > 0:
                play_str = f"{play_count:,}" if play_count < 10000 else f"{play_count/10000:.1f}万"
                reason_parts.append(f"播放量 {play_str}")
            if play_url:
                reason_parts.append("已获取播放直链")
            bili_candidate = MaterialItem(
                placeholder=placeholder,
                keyword=keyword,
                title=video_title,
                reason="，".join(reason_parts) + "，可作为画面支撑。",
                platform="bilibili",
                url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                play_url=play_url,
                embed_url="",
                extra={
                    "bvid": bvid,
                    "play": play_count,
                    "author": video_author,
                    "mock": False
                }
            )

        # 2+. YouTube 备用/扩展
        if use_youtube:
            yt_res = _search_youtube_api(keyword, max_results=1, debug=debug) or _search_youtube_scrape(keyword, max_results=1, debug=debug)
            if yt_res:
                yt_top = yt_res[0]
                view_count = yt_top.get("view", 0)
                yt_reason_parts = ["匹配度高", "平台：YouTube"]
                if view_count:
                    view_str = f"{view_count:,}" if view_count < 10000 else f"{view_count/10000:.1f}万"
                    yt_reason_parts.append(f"播放量 {view_str}")
                yt_candidate = MaterialItem(
                    placeholder=placeholder,
                    keyword=keyword,
                    title=yt_top.get("title", "YouTube 视频"),
                    reason="，".join(yt_reason_parts) + "，可作为补充画面。",
                    platform="youtube",
                    url=yt_top.get("url", ""),
                    play_url=yt_top.get("embed_url", ""),
                    embed_url=yt_top.get("embed_url", ""),
                    extra={
                        "video_id": yt_top.get("video_id", ""),
                        "channel": yt_top.get("channel", ""),
                        "view": view_count,
                        "mock": False,
                    }
                )

        # 3. 择优选择
        chosen: Optional[MaterialItem] = None
        if bili_candidate and yt_candidate:
            # 对比播放量，视为简单的热度指标
            bili_play = int(bili_candidate.extra.get("play", 0) or 0)
            yt_play = int(yt_candidate.extra.get("view", 0) or 0)
            chosen = yt_candidate if yt_play > bili_play else bili_candidate
            if debug:
                picked = "YouTube" if chosen == yt_candidate else "B站"
                print(f"    🎯 优选平台：{picked}")
        else:
            chosen = bili_candidate or yt_candidate

        if chosen:
            materials.append(chosen)
        else:
            # 4. 彻底失败 -> 降级 Mock
            if debug: 
                print("    ❌ 搜索无结果，降级为模拟数据")
            
            bv_code = f"BV{idx:06d}"
            materials.append(MaterialItem(
                placeholder=placeholder,
                keyword=keyword,
                title=f"未找到素材: {keyword}",
                reason="搜索无结果或被风控",
                platform="mock",
                url=f"https://www.bilibili.com/video/{bv_code}",
                play_url="",
                embed_url="",
                extra={"mock": True}
            ))

        # 限频：稍微休息一下，虽然库有处理，但慢点更稳
        if idx < len(script.placeholders):
            delay = 1.5
            if debug:
                print(f"  等待 {delay:.2f} 秒（限频）...")
            time.sleep(delay)

    return MaterialMatchResult(topic=script.topic, materials=materials)
