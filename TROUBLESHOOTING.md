# 故障排除指南 (Troubleshooting Guide)

## 1. Bilibili SSL 证书错误 ⚠️ 最常见

### 问题描述
```
ClientConnectorCertificateError: Cannot connect to host api.bilibili.com:443 ssl:True
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate
```

### 根本原因
代理服务器（如 Clash）拦截 HTTPS 流量时，会使用自己的证书替换原始证书。Python 的 httpx 库不信任代理的证书，导致 SSL 验证失败。

### 解决方案

#### 方案 A: 使用环境变量禁用 SSL 验证（快速测试）⭐ 推荐

**注意：此方案会降低安全性，仅用于开发/测试环境**

```bash
# 在 .env 文件中添加
DISABLE_SSL_VERIFY_FOR_BILIBILI=1

# 然后运行
python3 main.py --mode cli --topic "你的选题" --use-proxy
```

#### 方案 B: 信任代理证书（生产环境推荐）

**步骤 1: 找到代理的 CA 证书**

对于 Clash：
- macOS: `~/.config/clash/ca.crt` 或在 Clash 设置中导出
- Windows: `%USERPROFILE%\.config\clash\ca.crt`

**步骤 2: 安装证书到 Python certifi**

```bash
# 查看 certifi 证书包位置
python3 -c "import certifi; print(certifi.where())"

# 将代理 CA 证书追加到 certifi 证书包
cat ~/.config/clash/ca.crt >> $(python3 -c "import certifi; print(certifi.where())")
```

**步骤 3: 验证**

```bash
# 测试 SSL 连接
python3 -c "import httpx; print(httpx.get('https://api.bilibili.com').status_code)"
```

#### 方案 C: 不使用代理（国内网络）

如果你在国内且不需要访问 Google/Wikipedia：

```bash
# 不使用 --use-proxy 标志
python3 main.py --mode cli --topic "你的选题"
```

### 测试命令

```bash
# 测试 Bilibili API 是否可访问
export DISABLE_SSL_VERIFY_FOR_BILIBILI=1
python3 -c "
from agent_mvp.step3_materials import _search_bilibili_safe
results = _search_bilibili_safe('日本政治', max_results=1, debug=True)
print(f'找到 {len(results)} 个结果')
"
```

---

## 2. Wikipedia / Google 搜索返回 0 结果

### 问题描述
即使设置了代理，Wikipedia 和 Google 搜索仍然返回 0 结果。

### 可能原因

1. **代理未正确配置**
   - 检查 `.env` 文件中的代理设置
   - 确保代理服务器正在运行

2. **Wikipedia API 区域限制**
   - Wikipedia 对某些地区有访问限制（403 Forbidden）
   - 代码已有 fallback 机制：Wikipedia 失败 → Bing 搜索

3. **关键词翻译问题**
   - 英文人名翻译可能不准确
   - 代码会同时使用中英文关键词搜索

### 解决方案

#### 验证代理是否工作

```bash
# 设置代理环境变量
export http_proxy=http://127.0.0.1:7897
export https_proxy=http://127.0.0.1:7897

# 测试代理连接
curl -I https://www.google.com
curl -I https://en.wikipedia.org
```

#### 检查代理设置

```bash
# 查看 .env 文件
cat .env | grep proxy

# 应该包含：
# http_proxy=http://127.0.0.1:7897
# https_proxy=http://127.0.0.1:7897
```

#### 手动测试搜索

```python
# 测试 Wikipedia 搜索
python3 -c "
import os
os.environ['http_proxy'] = 'http://127.0.0.1:7897'
os.environ['https_proxy'] = 'http://127.0.0.1:7897'
from agent_mvp.step1_research import _search_wikipedia
results = _search_wikipedia('Sanae Takaichi', debug=True)
print(f'找到 {len(results)} 个结果')
"
```

---

## 3. Baidu 403 错误

### 问题描述
```
baidu_result count=0, error=403
```

### 根本原因
百度百科有严格的反爬虫机制，会检测并阻止自动化请求。

### 解决方案

**这是预期行为，不是 bug** ✅

代码已经有多重 fallback 机制：
1. 百度百科 → Wikipedia → Bing 搜索
2. 即使百度失败，其他数据源仍然可以提供足够的信息

如果需要百度数据，可以考虑：
- 使用更复杂的反爬虫策略（不推荐）
- 使用百度官方 API（如果有）
- 依赖 Wikipedia 和 Bing 作为主要数据源

---

## 4. 关键词质量问题

### 问题描述
生成的关键词不够"硬核"，深度不足。

### 解决方案

已在 `config/prompt/step1_keywords.yaml` 中改进 prompt：
- 从 3-5 个关键词增加到 5-7 个
- 强调"深挖历史根源"、"追溯深层原因"
- 要求包含家族、师承、关键事件等

**测试结果**：
- 原来：5 个表面关键词
- 现在：13 个硬核关键词（包括 北一辉、昭和维新、大东亚共荣圈 等）

---

## 5. 内容深度问题

### 问题描述
只生成 3 个章节，深度不够。

### 解决方案

已在 `config/prompt/step1_outline.yaml` 中改进 prompt：
- 要求生成 5-7 个章节（不少于 5 个）
- 强调"历史纵深感"、"深层原因"
- 明确叙事逻辑：开场引入 → 历史根源 → 发展脉络 → 关键转折 → 深层影响 → 当代反思

**测试结果**：
- 原来：3 个章节
- 现在：7 个章节（开场、根源、家族、转折、演变、影响、反思）

---

## 常见问题 FAQ

### Q: 为什么 YouTube 视频相关度低？

A: YouTube 搜索使用的是英文关键词，可能翻译不准确。建议：
1. 检查生成的英文关键词是否准确
2. 如果不准确，可以在 `step1_research.py` 中调整翻译 prompt
3. 或者手动修正关键词翻译

### Q: 为什么只返回 YouTube 视频，没有 Bilibili？

A: 这是因为 Bilibili API 的 SSL 证书错误。解决方案：
1. 使用 `DISABLE_SSL_VERIFY_FOR_BILIBILI=1` 环境变量
2. 或者信任代理证书（见上文）

### Q: 如何提高搜索成功率？

A:
1. **Bilibili**: 提供 `BILIBILI_SESSDATA` cookie（在 `.env` 中设置）
2. **YouTube**: 提供 `YOUTUBE_API_KEY`（在 `.env` 中设置）
3. **Wikipedia/Google**: 确保代理正确配置并运行

### Q: 代理设置后仍然无法访问 Google/Wikipedia？

A: 可能的原因：
1. 代理服务器未运行或端口错误
2. 代理需要认证（当前代码不支持）
3. 代理本身被封锁

验证方法：
```bash
# 测试代理
curl -x http://127.0.0.1:7897 https://www.google.com
```

---

## 推荐配置

### 最佳实践（生产环境）

```bash
# .env 文件
DASHSCOPE_API_KEY=your_api_key_here
BILIBILI_SESSDATA=your_sessdata_here
YOUTUBE_API_KEY=your_youtube_api_key_here

# 代理设置（仅在需要访问 Google/Wikipedia 时）
http_proxy=http://127.0.0.1:7897
https_proxy=http://127.0.0.1:7897

# 运行命令
python3 main.py --mode cli --topic "你的选题" --use-proxy
```

### 快速测试（开发环境）⭐ 推荐

```bash
# .env 文件
DASHSCOPE_API_KEY=your_api_key_here
DISABLE_SSL_VERIFY_FOR_BILIBILI=1

# 代理设置
http_proxy=http://127.0.0.1:7897
https_proxy=http://127.0.0.1:7897

# 运行命令
python3 main.py --mode cli --topic "你的选题" --use-proxy
```

### 国内网络（无需代理）

```bash
# .env 文件
DASHSCOPE_API_KEY=your_api_key_here
BILIBILI_SESSDATA=your_sessdata_here

# 运行命令（不使用 --use-proxy）
python3 main.py --mode cli --topic "你的选题"
```

---

## 获取帮助

如果问题仍未解决：
1. 查看 `.cursor/debug.log` 获取详细日志
2. 运行测试命令验证各个组件
3. 检查网络连接和代理设置
4. 确保所有依赖库已正确安装

## 关于敏感话题

某些话题可能触发 Qwen API 的内容审核机制，返回错误：
```
BadRequestError: Output data may contain inappropriate content
```

建议：
1. 使用更中性的表述
2. 或选择其他话题进行测试
