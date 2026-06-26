# 足球数据网站反爬研究与绕过方案

## 目标网站分析

### 1. FBref (fbref.com) ⭐⭐⭐⭐⭐
**数据价值**: 最全面的足球统计数据  
**反爬机制**:
- Cloudflare Turnstile (新版人机验证)
- 速率限制 (rate limiting)
- User-Agent 检测
- TLS 指纹识别

**绕过策略**:
- ✅ Playwright (真实浏览器指纹)
- ✅ Stealth 插件 (隐藏自动化特征)
- ✅ 随机延迟 (3-6秒)
- ✅ 代理轮换 (可选)

---

### 2. Transfermarkt (transfermarkt.com) ⭐⭐⭐⭐
**数据价值**: 球员身价、转会、阵容  
**反爬机制**:
- Cloudflare (基础版)
- Cookie 验证
- JavaScript 渲染

**绕过策略**:
- ✅ Playwright
- ✅ Cookie 保持会话

---

### 3. SofaScore (sofascore.com) ⭐⭐⭐⭐⭐
**数据价值**: 实时比分、阵容、角球、xG  
**反爬机制**:
- API 签名验证 (x-signature header)
- 设备指纹
- 频率限制

**绕过策略**:
- ✅ 逆向 API 签名算法
- ✅ 模拟移动端 App 请求
- ⚠️ 难度高,需要逆向 JS

---

### 4. Understat (understat.com) ⭐⭐⭐
**数据价值**: xG 数据(联赛为主)  
**反爬机制**:
- 基础反爬(User-Agent)
- JSON 数据嵌在 JS 中

**绕过策略**:
- ✅ 正则提取 JSON
- ✅ 简单 requests 即可
- ❌ 国家队数据缺失

---

### 5. WhoScored (whoscored.com) ⭐⭐⭐⭐
**数据价值**: 详细战术数据、评分  
**反爬机制**:
- Cloudflare
- Incapsula WAF
- JavaScript 混淆

**绕过策略**:
- ✅ Playwright + Stealth
- ⚠️ 需要登录(部分数据)

---

### 6. FlashScore (flashscore.com) ⭐⭐⭐⭐
**数据价值**: 实时比分、角球、统计  
**反爬机制**:
- API 加密
- WebSocket 实时数据
- 设备指纹

**绕过策略**:
- ✅ Playwright 监听 WebSocket
- ✅ 解析二进制消息

---

## 通用反反爬技术栈

### 浏览器自动化
```python
# Playwright + Stealth
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="...",
        viewport={"width": 1920, "height": 1080},
        locale="en-US"
    )
    page = context.new_page()
    stealth_sync(page)  # 隐藏自动化特征
```

### 指纹伪装
- Canvas 指纹随机化
- WebGL 指纹伪造
- Audio Context 指纹修改
- 时区/语言/字体列表

### 代理策略
- 住宅代理(Residential Proxies) - 最难检测
- 数据中心代理(Datacenter) - 便宜但易封
- 自建代理池(多地域 VPS)

### 请求模式
- 人类行为模拟(随机滚动、鼠标移动)
- 随机延迟(泊松分布)
- 会话保持(Cookie + localStorage)

---

## 实施优先级

### 第一阶段(当前): FBref
- 最重要的数据源
- Playwright 已就绪
- 预计成功率: 85%+

### 第二阶段: SofaScore API
- 实时数据最准确
- 需要逆向工程
- 预计1-2天研究

### 第三阶段: Transfermarkt
- 补充球员身价数据
- 相对简单
- 预计1天

---

## 备用方案

如果爬取失败:
1. **手动维护 JSON** - corners.json, xg_profiles.json
2. **付费 API** - API-Football (100次/天免费)
3. **众包数据** - 社区维护 GitHub repo
