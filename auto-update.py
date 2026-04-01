#!/usr/bin/env python3
"""
AI Builders Digest — 自动更新脚本
每周一和周五早上9点执行（非节假日）：
1. 检查是否为节假日（节假日跳过）
2. 检查 GitHub Token 是否有效（过期则通过企微通知更换）
3. 从 GitHub 拉取最新 feed 数据（tweets, podcasts, blogs）
4. 过滤上次推送以来的新内容（周一取周五~周一，周五取周一~周五）
5. 生成可视化 HTML 页面
6. 推送到 GitHub Pages
7. 发送企微群通知
"""

import json
import urllib.request
import urllib.error
import subprocess
import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

# ============================================================================
# 配置
# ============================================================================

REPO_DIR = Path(__file__).parent
GITHUB_REPO = "https://github.com/eagergogogo/ai-digest.git"
GITHUB_PAGES_URL = "https://eagergogogo.github.io/ai-digest/"

# 企微 Webhook（从环境变量读取，安全起见不硬编码）
WECOM_WEBHOOK = os.environ.get(
    "WECOM_WEBHOOK",
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=786b1746-0c53-4e25-990d-722e62146d84"
)

# 运行环境检测（github-actions / local）
RUN_ENV = os.environ.get("RUN_ENV", "local")

# Feed 数据源（来自 follow-builders 中央仓库）
FEED_X_URL = "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-x.json"
FEED_PODCASTS_URL = "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-podcasts.json"
FEED_BLOGS_URL = "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-blogs.json"

# GitHub Token（从环境变量读取，安全起见不硬编码）
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ============================================================================
# 2026 年法定节假日 & 调休上班日（国务院发布）
# ============================================================================

# 法定假日（这些天不执行）
HOLIDAYS_2026 = {
    # 元旦: 1月1日-3日
    date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
    # 春节: 2月15日-23日
    date(2026, 2, 15), date(2026, 2, 16), date(2026, 2, 17),
    date(2026, 2, 18), date(2026, 2, 19), date(2026, 2, 20),
    date(2026, 2, 21), date(2026, 2, 22), date(2026, 2, 23),
    # 清明节: 4月4日-6日
    date(2026, 4, 4), date(2026, 4, 5), date(2026, 4, 6),
    # 劳动节: 5月1日-5日
    date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3),
    date(2026, 5, 4), date(2026, 5, 5),
    # 端午节: 6月19日-21日
    date(2026, 6, 19), date(2026, 6, 20), date(2026, 6, 21),
    # 中秋节: 9月25日-27日
    date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27),
    # 国庆节: 10月1日-7日
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3),
    date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6),
    date(2026, 10, 7),
}

# 调休上班日（这些周末要上班，正常执行）
WORKDAYS_2026 = {
    date(2026, 1, 4),   # 元旦调休，周日上班
    date(2026, 2, 14),  # 春节调休，周六上班
    date(2026, 2, 28),  # 春节调休，周六上班
    date(2026, 5, 9),   # 劳动节调休，周六上班
    date(2026, 9, 20),  # 国庆调休，周日上班
    date(2026, 10, 10), # 国庆调休，周六上班
}


def is_workday(d=None):
    """判断是否为工作日（考虑法定节假日和调休）"""
    if d is None:
        d = date.today()
    # 调休上班日 → 算工作日
    if d in WORKDAYS_2026:
        return True
    # 法定假日 → 不算工作日
    if d in HOLIDAYS_2026:
        return False
    # 周末 → 不算工作日
    if d.weekday() >= 5:  # 5=周六, 6=周日
        return False
    return True

# ============================================================================
# 工具函数
# ============================================================================

def log(msg):
    """带时间戳的日志"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def fetch_json(url):
    """拉取 JSON 数据"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AI-Digest-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log(f"⚠️  拉取失败 {url}: {e}")
        return None

def send_wecom(content):
    """发送企微 Markdown 消息到【数学小天才】群"""
    payload = json.dumps({
        "msgtype": "markdown",
        "markdown": {"content": content}
    }).encode()
    req = urllib.request.Request(
        WECOM_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            if result.get("errcode") == 0:
                log("✅ 企微消息发送成功")
            else:
                log(f"⚠️  企微发送异常: {result}")
    except Exception as e:
        log(f"❌ 企微发送失败: {e}")

def git_push(commit_msg):
    """推送到 GitHub（兼容本地和 GitHub Actions 环境）"""
    os.chdir(REPO_DIR)
    try:
        subprocess.run(["git", "add", "-A"], check=True, capture_output=True)

        # 检查是否有变更
        result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if not result.stdout.strip():
            log("ℹ️  没有变更，跳过推送")
            return True

        subprocess.run(["git", "commit", "-m", commit_msg], check=True, capture_output=True)

        if RUN_ENV == "github-actions":
            # GitHub Actions 中已通过 checkout action 的 token 配置好认证
            subprocess.run(["git", "push"], check=True, capture_output=True, timeout=60)
        else:
            # 本地环境：设置带 token 的 remote URL
            if GITHUB_TOKEN:
                auth_url = f"https://eagergogogo:{GITHUB_TOKEN}@github.com/eagergogogo/ai-digest.git"
                subprocess.run(["git", "remote", "set-url", "origin", auth_url], check=True, capture_output=True)

            subprocess.run(["git", "push", "origin", "main"], check=True, capture_output=True, timeout=60)

            # 推送后清除 token（安全）
            subprocess.run(["git", "remote", "set-url", "origin", GITHUB_REPO], check=True, capture_output=True)

        log("✅ GitHub 推送成功")
        return True
    except subprocess.CalledProcessError as e:
        log(f"❌ Git 推送失败: {e.stderr.decode() if e.stderr else e}")
        if RUN_ENV != "github-actions":
            # 确保清除 token
            subprocess.run(["git", "remote", "set-url", "origin", GITHUB_REPO], capture_output=True)
        return False

# ============================================================================
# 翻译功能（英文 → 中文）
# ============================================================================

def translate_text(text, max_retries=2):
    """使用 Google Translate 免费 API 将英文翻译为中文"""
    if not text or not text.strip():
        return text

    # 跳过已经是中文为主的文本
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    if chinese_chars > len(text) * 0.3:
        return text

    for attempt in range(max_retries + 1):
        try:
            import urllib.parse
            encoded = urllib.parse.quote(text[:2000])  # 限制长度
            url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-CN&dt=t&q={encoded}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                translated = "".join(part[0] for part in result[0] if part[0])
                return translated
        except Exception as e:
            if attempt == max_retries:
                log(f"⚠️  翻译失败，使用原文: {str(e)[:50]}")
                return text
            import time
            time.sleep(1)
    return text


def translate_feed_data(tweets_data, podcasts_data, blogs_data):
    """批量翻译 feed 数据中的英文内容为中文"""
    log("🌐 正在翻译英文内容为中文...")
    translated_count = 0

    # 翻译推文
    for builder in (tweets_data or []):
        # 翻译 bio
        bio = builder.get("bio", "")
        if bio:
            builder["bio"] = translate_text(bio)
            translated_count += 1

        for tweet in builder.get("tweets", []):
            text = tweet.get("text", "")
            if text:
                tweet["text"] = translate_text(text)
                translated_count += 1

    # 翻译播客
    for pod in (podcasts_data or []):
        title = pod.get("title", "")
        if title:
            pod["title"] = translate_text(title)
            translated_count += 1
        summary = pod.get("summary", "")
        if summary:
            pod["summary"] = translate_text(summary)
            translated_count += 1

    # 翻译博客
    for blog in (blogs_data or []):
        title = blog.get("title", "")
        if title:
            blog["title"] = translate_text(title)
            translated_count += 1
        summary = blog.get("summary", "")
        if summary:
            blog["summary"] = translate_text(summary)
            translated_count += 1

    log(f"✅ 翻译完成，共翻译 {translated_count} 段内容")
    return tweets_data, podcasts_data, blogs_data


# ============================================================================
# 过滤最近24小时的内容
# ============================================================================

def get_lookback_hours():
    """根据今天是周几决定回溯时间（覆盖上次推送到现在的区间）
    - 周一：回溯 72 小时（上周五早9点 → 周一早9点）
    - 周五：回溯 96 小时（周一早9点 → 周五早9点）
    """
    today = date.today()
    if today.weekday() == 0:  # 周一
        return 72   # 覆盖周五、周六、周日
    elif today.weekday() == 4:  # 周五
        return 96   # 覆盖周一、周二、周三、周四
    return 96  # 兜底（正常不会走到）


def filter_recent(feed_data, hours=None):
    """过滤上次推送以来的新内容（周一取72h，周五取96h）"""
    if hours is None:
        hours = get_lookback_hours()

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    cutoff_str = cutoff.isoformat() + "Z"
    cutoff_date = cutoff.strftime("%Y-%m-%d")

    if not feed_data:
        return []

    filtered = []
    for item in feed_data:
        # 检查 tweets 列表中的时间
        if "tweets" in item:
            recent_tweets = [
                t for t in item["tweets"]
                if t.get("created_at", "9999") >= cutoff_str or t.get("date", "9999") >= cutoff_str[:10]
            ]
            if recent_tweets:
                filtered.append({**item, "tweets": recent_tweets})
        # 检查 podcast/blog 的发布时间
        elif item.get("published_at", "9999") >= cutoff_str or item.get("date", "9999") >= cutoff_str[:10]:
            filtered.append(item)
        # 没有时间字段的全部保留
        elif "published_at" not in item and "date" not in item and "tweets" not in item:
            filtered.append(item)

    return filtered

# ============================================================================
# 生成 HTML
# ============================================================================

def generate_html(tweets_data, podcasts_data, blogs_data):
    """根据 feed 数据生成可视化 HTML 页面"""
    today = datetime.now()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    date_str = f"{today.year}年{today.month}月{today.day}日 · {weekdays[today.weekday()]}"
    date_short = today.strftime("%Y.%m.%d")

    # 统计数据
    num_builders = len(tweets_data) if tweets_data else 0
    num_tweets = sum(len(b.get("tweets", [])) for b in (tweets_data or []))
    num_podcasts = len(podcasts_data) if podcasts_data else 0
    num_blogs = len(blogs_data) if blogs_data else 0

    # 生成推文卡片
    tweet_cards = []
    for builder in (tweets_data or []):
        name = builder.get("name", "Unknown")
        handle = builder.get("handle", "")
        bio = builder.get("bio", "")
        initials = "".join(w[0] for w in name.split()[:2]).upper()

        for tweet in builder.get("tweets", []):
            text = tweet.get("text", "").replace("<", "&lt;").replace(">", "&gt;")
            likes = tweet.get("likes", 0)
            retweets = tweet.get("retweets", 0)
            replies = tweet.get("replies", 0)
            url = tweet.get("url", f"https://x.com/{handle}")

            card = f'''    <div class="card">
      <div class="card-author">
        <div class="avatar" style="background: var(--gradient-1);">{initials}</div>
        <div class="author-info">
          <div class="name">{name}</div>
          <div class="role">{bio}</div>
        </div>
      </div>
      <div class="card-body">
        <p>{text}</p>
      </div>
      <div class="engagement">
        <span>❤️ {likes:,}</span>
        <span>🔁 {retweets:,}</span>
        <span>💬 {replies:,}</span>
      </div>
      <a class="link-btn" href="{url}" target="_blank">查看原推 →</a>
    </div>'''
            tweet_cards.append(card)

    # 生成播客卡片
    podcast_cards = []
    for pod in (podcasts_data or []):
        title = pod.get("title", "").replace("<", "&lt;").replace(">", "&gt;")
        show = pod.get("show", "").replace("<", "&lt;").replace(">", "&gt;")
        summary = pod.get("summary", "").replace("<", "&lt;").replace(">", "&gt;")
        url = pod.get("url", "#")

        card = f'''    <div class="card podcast-card">
      <div class="card-author">
        <div class="avatar" style="background: var(--gradient-3);">🎙️</div>
        <div class="author-info">
          <div class="name">{show}</div>
          <div class="role">{title}</div>
        </div>
      </div>
      <div class="card-body">
        <p>{summary}</p>
      </div>
      <a class="link-btn" href="{url}" target="_blank">观看完整节目 →</a>
    </div>'''
        podcast_cards.append(card)

    # 生成博客卡片
    blog_cards = []
    for blog in (blogs_data or []):
        title = blog.get("title", "").replace("<", "&lt;").replace(">", "&gt;")
        source = blog.get("source", "").replace("<", "&lt;").replace(">", "&gt;")
        summary = blog.get("summary", "").replace("<", "&lt;").replace(">", "&gt;")
        url = blog.get("url", "#")

        card = f'''    <div class="card blog-card">
      <span class="tag product">产品发布</span>
      <div class="card-author">
        <div class="avatar" style="background: var(--gradient-1);">📝</div>
        <div class="author-info">
          <div class="name">{source}</div>
          <div class="role">{title}</div>
        </div>
      </div>
      <div class="card-body">
        <p>{summary}</p>
      </div>
      <a class="link-btn" href="{url}" target="_blank">阅读原文 →</a>
    </div>'''
        blog_cards.append(card)

    # 如果没有任何新内容
    if not tweet_cards and not podcast_cards and not blog_cards:
        return None

    # 组装 HTML
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Builders Digest — {date_short}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=Noto+Sans+SC:wght@300;400;500;700&display=swap');

  :root {{
    --bg: #0a0a0f;
    --surface: #13131a;
    --surface2: #1a1a24;
    --border: rgba(255,255,255,0.06);
    --text: #e8e8ed;
    --text-dim: #8888a0;
    --accent-blue: #4f8fff;
    --accent-purple: #a855f7;
    --accent-green: #22d3a7;
    --accent-orange: #f59e0b;
    --accent-pink: #ec4899;
    --accent-red: #ef4444;
    --highlight-bg: rgba(79,143,255,0.1);
    --highlight-border: rgba(79,143,255,0.3);
    --gradient-1: linear-gradient(135deg, #4f8fff 0%, #a855f7 100%);
    --gradient-2: linear-gradient(135deg, #22d3a7 0%, #4f8fff 100%);
    --gradient-3: linear-gradient(135deg, #f59e0b 0%, #ec4899 100%);
  }}

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', 'Noto Sans SC', -apple-system, sans-serif;
    line-height: 1.7;
    min-height: 100vh;
    overflow-x: hidden;
  }}

  .bg-effects {{
    position: fixed; inset: 0; z-index: 0; pointer-events: none;
  }}
  .bg-effects .orb {{
    position: absolute; border-radius: 50%; filter: blur(120px); opacity: 0.15;
    animation: float 20s ease-in-out infinite;
  }}
  .bg-effects .orb:nth-child(1) {{
    width: 600px; height: 600px; background: #4f8fff;
    top: -200px; right: -100px;
  }}
  .bg-effects .orb:nth-child(2) {{
    width: 500px; height: 500px; background: #a855f7;
    bottom: -150px; left: -100px; animation-delay: -7s;
  }}
  .bg-effects .orb:nth-child(3) {{
    width: 400px; height: 400px; background: #22d3a7;
    top: 50%; left: 50%; transform: translate(-50%, -50%);
    animation-delay: -14s;
  }}
  @keyframes float {{
    0%, 100% {{ transform: translate(0, 0) scale(1); }}
    25% {{ transform: translate(30px, -40px) scale(1.05); }}
    50% {{ transform: translate(-20px, 20px) scale(0.95); }}
    75% {{ transform: translate(40px, 30px) scale(1.02); }}
  }}

  .container {{
    position: relative; z-index: 1;
    max-width: 900px; margin: 0 auto;
    padding: 40px 24px 80px;
  }}

  .header {{
    text-align: center; margin-bottom: 56px;
    animation: fadeUp 0.8s ease;
  }}
  .header .date-badge {{
    display: inline-flex; align-items: center; gap: 8px;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 100px; padding: 8px 20px;
    font-size: 13px; color: var(--text-dim); margin-bottom: 24px;
    letter-spacing: 1px; text-transform: uppercase;
  }}
  .header .date-badge .dot {{
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--accent-green);
    animation: pulse 2s ease-in-out infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50% {{ opacity: 0.5; transform: scale(0.8); }}
  }}
  .header h1 {{
    font-size: 48px; font-weight: 900; line-height: 1.1;
    background: var(--gradient-1);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text; margin-bottom: 16px;
    letter-spacing: -1px;
  }}
  .header .subtitle {{
    font-size: 17px; color: var(--text-dim); font-weight: 400;
    max-width: 500px; margin: 0 auto;
  }}

  .stats-bar {{
    display: flex; gap: 12px; justify-content: center;
    margin-bottom: 48px; flex-wrap: wrap;
    animation: fadeUp 0.8s ease 0.1s both;
  }}
  .stat-chip {{
    display: flex; align-items: center; gap: 8px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 12px 20px;
  }}
  .stat-chip .stat-icon {{ font-size: 20px; }}
  .stat-chip .stat-num {{
    font-size: 22px; font-weight: 800;
    background: var(--gradient-1);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }}
  .stat-chip .stat-label {{ font-size: 13px; color: var(--text-dim); }}

  .section {{
    margin-bottom: 48px;
    animation: fadeUp 0.8s ease both;
  }}
  .section-header {{
    display: flex; align-items: center; gap: 14px;
    margin-bottom: 24px; padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }}
  .section-icon {{
    width: 44px; height: 44px; border-radius: 14px;
    display: flex; align-items: center; justify-content: center;
    font-size: 22px; flex-shrink: 0;
  }}
  .section-icon.blog {{ background: linear-gradient(135deg, rgba(79,143,255,0.2), rgba(168,85,247,0.2)); }}
  .section-icon.twitter {{ background: linear-gradient(135deg, rgba(34,211,167,0.2), rgba(79,143,255,0.2)); }}
  .section-icon.podcast {{ background: linear-gradient(135deg, rgba(245,158,11,0.2), rgba(236,72,153,0.2)); }}
  .section-title {{ font-size: 22px; font-weight: 800; letter-spacing: -0.5px; }}
  .section-count {{
    font-size: 12px; color: var(--text-dim);
    background: var(--surface2); padding: 4px 12px;
    border-radius: 100px; margin-left: auto;
  }}

  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 28px;
    margin-bottom: 16px;
    transition: all 0.3s ease;
    position: relative;
    overflow: hidden;
  }}
  .card:hover {{
    border-color: rgba(79,143,255,0.2);
    transform: translateY(-2px);
    box-shadow: 0 8px 32px rgba(0,0,0,0.3);
  }}
  .card-author {{
    display: flex; align-items: center; gap: 14px;
    margin-bottom: 16px;
  }}
  .avatar {{
    width: 44px; height: 44px; border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px; font-weight: 700; flex-shrink: 0;
    color: white;
  }}
  .author-info .name {{ font-size: 16px; font-weight: 700; }}
  .author-info .role {{ font-size: 13px; color: var(--text-dim); }}

  .card-body {{ font-size: 15px; line-height: 1.8; color: #c8c8d8; }}
  .card-body p {{ margin-bottom: 12px; }}
  .card-body p:last-child {{ margin-bottom: 0; }}

  .highlight {{
    background: linear-gradient(90deg, rgba(79,143,255,0.12) 0%, rgba(168,85,247,0.08) 100%);
    border-left: 3px solid var(--accent-blue);
    padding: 14px 18px;
    border-radius: 0 12px 12px 0;
    margin: 14px 0;
    font-weight: 500;
    color: #e0e0f0;
    font-size: 15px;
    line-height: 1.7;
  }}

  .tag {{
    display: inline-block;
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 1px; padding: 4px 10px;
    border-radius: 6px; margin-bottom: 12px;
  }}
  .tag.hot {{ background: rgba(239,68,68,0.15); color: #f87171; }}
  .tag.new {{ background: rgba(34,211,167,0.15); color: #34d399; }}
  .tag.insight {{ background: rgba(168,85,247,0.15); color: #c084fc; }}
  .tag.product {{ background: rgba(79,143,255,0.15); color: #93bbff; }}
  .tag.trend {{ background: rgba(245,158,11,0.15); color: #fbbf24; }}

  .engagement {{
    display: flex; gap: 16px; margin-top: 16px; padding-top: 14px;
    border-top: 1px solid var(--border); font-size: 13px; color: var(--text-dim);
  }}
  .engagement span {{ display: flex; align-items: center; gap: 5px; }}

  .link-btn {{
    display: inline-flex; align-items: center; gap: 6px;
    margin-top: 14px; padding: 8px 16px;
    background: rgba(79,143,255,0.1);
    border: 1px solid rgba(79,143,255,0.2);
    border-radius: 10px; font-size: 13px; font-weight: 600;
    color: var(--accent-blue); text-decoration: none;
    transition: all 0.2s;
  }}
  .link-btn:hover {{
    background: rgba(79,143,255,0.2);
    transform: translateX(2px);
  }}

  .podcast-card {{
    background: linear-gradient(135deg, var(--surface) 0%, rgba(245,158,11,0.04) 100%);
    border: 1px solid rgba(245,158,11,0.15);
  }}
  .podcast-card:hover {{ border-color: rgba(245,158,11,0.3); }}

  .blog-card {{
    background: linear-gradient(135deg, var(--surface) 0%, rgba(79,143,255,0.04) 100%);
    border: 1px solid rgba(79,143,255,0.15);
  }}
  .blog-card:hover {{ border-color: rgba(79,143,255,0.3); }}

  .footer {{
    text-align: center; padding-top: 40px;
    border-top: 1px solid var(--border);
    color: var(--text-dim); font-size: 13px;
    animation: fadeUp 0.8s ease 0.5s both;
  }}
  .footer a {{ color: var(--accent-blue); text-decoration: none; }}

  @keyframes fadeUp {{
    from {{ opacity: 0; transform: translateY(20px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}

  @media (max-width: 640px) {{
    .container {{ padding: 24px 16px 60px; }}
    .header h1 {{ font-size: 32px; }}
    .stats-bar {{ gap: 8px; }}
    .stat-chip {{ padding: 10px 14px; }}
    .stat-chip .stat-num {{ font-size: 18px; }}
    .card {{ padding: 20px; }}
    .section-title {{ font-size: 18px; }}
  }}
</style>
</head>
<body>

<div class="bg-effects">
  <div class="orb"></div>
  <div class="orb"></div>
  <div class="orb"></div>
</div>

<div class="container">

  <div class="header">
    <div class="date-badge"><div class="dot"></div> {date_str}</div>
    <h1>AI Builders Digest</h1>
    <p class="subtitle">追踪真正在构建 AI 的人，而不是转述者</p>
  </div>

  <div class="stats-bar">
    <div class="stat-chip">
      <span class="stat-icon">🐦</span>
      <span class="stat-num">{num_builders}</span>
      <span class="stat-label">Builders</span>
    </div>
    <div class="stat-chip">
      <span class="stat-icon">💬</span>
      <span class="stat-num">{num_tweets}</span>
      <span class="stat-label">推文</span>
    </div>
    <div class="stat-chip">
      <span class="stat-icon">🎙️</span>
      <span class="stat-num">{num_podcasts}</span>
      <span class="stat-label">播客</span>
    </div>
    <div class="stat-chip">
      <span class="stat-icon">📝</span>
      <span class="stat-num">{num_blogs}</span>
      <span class="stat-label">博客</span>
    </div>
  </div>

  SECTIONS_PLACEHOLDER

  <div class="footer">
    <p>Generated by <a href="https://github.com/eagergogogo/ai-digest" target="_blank">AI Builders Digest</a></p>
    <p style="margin-top: 8px;">追踪真正在构建 AI 的人 · 数据更新于 UPDATE_TIME_PLACEHOLDER</p>
  </div>

</div>

</body>
</html>'''

    # 构建各 section
    sections_parts = []

    if blog_cards:
        sections_parts.append(f'''<div class="section">
    <div class="section-header">
      <div class="section-icon blog">📝</div>
      <div class="section-title">官方博客</div>
      <div class="section-count">{num_blogs} 篇新文章</div>
    </div>
''' + "\n".join(blog_cards) + "\n  </div>")

    if tweet_cards:
        sections_parts.append(f'''<div class="section">
    <div class="section-header">
      <div class="section-icon twitter">🐦</div>
      <div class="section-title">X / Twitter 动态</div>
      <div class="section-count">{num_builders} 位 Builders</div>
    </div>
''' + "\n".join(tweet_cards) + "\n  </div>")

    if podcast_cards:
        sections_parts.append(f'''<div class="section">
    <div class="section-header">
      <div class="section-icon podcast">🎙️</div>
      <div class="section-title">播客精选</div>
      <div class="section-count">{num_podcasts} 期新节目</div>
    </div>
''' + "\n".join(podcast_cards) + "\n  </div>")

    html = html.replace("SECTIONS_PLACEHOLDER", "\n\n  ".join(sections_parts))
    html = html.replace("UPDATE_TIME_PLACEHOLDER", today.strftime("%Y-%m-%d %H:%M"))

    return html

# ============================================================================
# 主流程
# ============================================================================

def check_github_token():
    """检测 GitHub Token 是否有效，过期则通过企微通知"""
    if not GITHUB_TOKEN:
        log("⚠️  未设置 GITHUB_TOKEN")
        if RUN_ENV == "github-actions":
            send_wecom("## ⚠️ AI Digest Token 异常\n\n未检测到 GitHub Token，请在 GitHub 仓库 Settings → Secrets 中更新 `GH_PAT`。")
        else:
            send_wecom("## ⚠️ AI Digest Token 异常\n\n未检测到 GitHub Token，请更新：\n\n"
                       "1. 打开 [GitHub Token 设置](https://github.com/settings/tokens)\n"
                       "2. 生成新 Token（勾选 `repo` 权限）\n"
                       "3. 编辑 `~/Library/LaunchAgents/com.eagergogogo.ai-digest.plist`\n"
                       "4. 替换 `GITHUB_TOKEN` 的值\n"
                       "5. 执行 `launchctl unload` + `launchctl load` 重载任务\n\n"
                       "或直接联系 CodeBuddy 帮你更新 🤖")
        return False

    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "User-Agent": "AI-Digest-Bot/1.0"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            log("✅ GitHub Token 验证通过")
            return True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            log("❌ GitHub Token 已过期或无效")
            if RUN_ENV == "github-actions":
                send_wecom("## 🔑 AI Digest — GitHub Token 已过期\n\n"
                           "自动更新因 Token 失效暂停，请尽快更换：\n\n"
                           "**操作步骤：**\n"
                           "1. 打开 [GitHub Token 设置](https://github.com/settings/tokens)\n"
                           "2. 生成新 Token（勾选 `repo` + `workflow` 权限）\n"
                           "3. 进入仓库 [Settings → Secrets](https://github.com/eagergogogo/ai-digest/settings/secrets/actions)\n"
                           "4. 更新 `GH_PAT` 的值\n\n"
                           "完成后会自动恢复每周更新 ✅")
            else:
                send_wecom("## 🔑 AI Digest — GitHub Token 已过期\n\n"
                           "自动更新因 Token 失效暂停，请尽快更换：\n\n"
                           "**操作步骤：**\n"
                           "1. 打开 [GitHub Token 设置](https://github.com/settings/tokens)\n"
                           "2. 点击 **Generate new token (classic)**\n"
                           "3. 勾选 `repo` 权限，设置有效期\n"
                           "4. 复制新 Token\n"
                           "5. 编辑文件 `~/Library/LaunchAgents/com.eagergogogo.ai-digest.plist`\n"
                           "6. 将 `<string>ghp_xxx</string>` 中的旧 Token 替换为新的\n"
                           "7. 终端执行：\n"
                           "> `launchctl unload ~/Library/LaunchAgents/com.eagergogogo.ai-digest.plist`\n"
                           "> `launchctl load ~/Library/LaunchAgents/com.eagergogogo.ai-digest.plist`\n\n"
                           "完成后会自动恢复每日更新 ✅")
            return False
        else:
            log(f"⚠️  GitHub API 请求异常: HTTP {e.code}")
            return True  # 非 401 错误可能是网络问题，继续执行
    except Exception as e:
        log(f"⚠️  Token 检测请求失败: {e}")
        return True  # 网络问题不阻塞执行


def get_beijing_now():
    """获取北京时间（兼容 GitHub Actions 的 UTC 环境）"""
    utc_now = datetime.utcnow()
    return utc_now + timedelta(hours=8)


def main():
    # 确保日志目录存在
    log_dir = REPO_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    log("🚀 AI Builders Digest 自动更新开始")
    log(f"📍 运行环境: {RUN_ENV}")

    # 使用北京时间（GitHub Actions 运行在 UTC）
    today = get_beijing_now()
    date_short = today.strftime("%Y.%m.%d")

    # Step 0: 检查今天是否为节假日
    if today.date() in HOLIDAYS_2026:
        log(f"📅 今天是 {today.strftime('%Y-%m-%d')}，法定节假日，跳过更新")
        return

    weekday_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][today.weekday()]
    log(f"📅 今天是{weekday_name}（北京时间 {today.strftime('%Y-%m-%d %H:%M')}），继续执行更新")

    # Step 0.5: 检查 GitHub Token 是否有效
    if not check_github_token():
        log("⛔ Token 无效，已通过企微通知，本次跳过更新")
        return

    # Step 1: 拉取最新 feed 数据
    lookback = get_lookback_hours()
    period_desc = "周末" if today.weekday() == 0 else "昨天"
    log(f"📡 正在拉取最新 feed 数据（回溯 {lookback} 小时，获取{period_desc}的内容）...")
    feed_x = fetch_json(FEED_X_URL)
    feed_podcasts = fetch_json(FEED_PODCASTS_URL)
    feed_blogs = fetch_json(FEED_BLOGS_URL)

    # 先取全量，再过滤时间
    tweets_all = feed_x.get("x", []) if feed_x else []
    podcasts_all = feed_podcasts.get("podcasts", []) if feed_podcasts else []
    blogs_all = feed_blogs.get("blogs", []) if feed_blogs else []

    log(f"📊 全量数据: {len(tweets_all)} builders, {len(podcasts_all)} podcasts, {len(blogs_all)} blogs")

    # 过滤只保留昨天/周末的最新内容
    tweets_data = filter_recent(tweets_all, lookback)
    podcasts_data = filter_recent(podcasts_all, lookback)
    blogs_data = filter_recent(blogs_all, lookback)

    log(f"🔍 过滤后（{period_desc}）: {len(tweets_data)} builders, {len(podcasts_data)} podcasts, {len(blogs_data)} blogs")

    # Step 1.5: 翻译英文内容为中文
    tweets_data, podcasts_data, blogs_data = translate_feed_data(tweets_data, podcasts_data, blogs_data)

    # Step 2: 生成 HTML
    log("🎨 正在生成 HTML 页面...")
    html = generate_html(tweets_data, podcasts_data, blogs_data)

    if html is None:
        log(f"⚠️  {period_desc}没有新内容，跳过更新")
        send_wecom(f"## ℹ️ AI Digest — {date_short}\n\n{period_desc}暂无新内容更新。\n\n📖 [查看往期内容 →]({GITHUB_PAGES_URL})")
        return

    # Step 3: 写入文件
    output_path = REPO_DIR / "index.html"
    output_path.write_text(html, encoding="utf-8")
    log(f"📄 HTML 已写入 {output_path}")

    # Step 4: 推送到 GitHub
    log("📤 正在推送到 GitHub...")
    commit_msg = f"Update: AI Builders Digest {today.strftime('%Y-%m-%d')}"
    push_ok = git_push(commit_msg)

    # Step 5: 发送企微通知
    log("💬 正在发送企微通知...")

    # 构建摘要要点
    num_total_tweets = sum(len(b.get("tweets", [])) for b in tweets_data)
    highlights = []
    for i, builder in enumerate(tweets_data[:3]):
        name = builder.get("name", "")
        if builder.get("tweets"):
            text = builder["tweets"][0].get("text", "")[:50]
            highlights.append(f"{i+1}. **{name}**: {text}...")

    highlights_text = "\n".join(highlights) if highlights else "暂无热门推文"

    wecom_msg = f"""## ⚡ AI Builders Digest — {date_short}

> 追踪真正在构建 AI 的人，而不是转述者

**{period_desc}更新：** {len(tweets_data)} 位 Builders · {num_total_tweets} 条推文 · {len(podcasts_data)} 期播客 · {len(blogs_data)} 篇博客

{highlights_text}

📖 [点击查看完整资讯 →]({GITHUB_PAGES_URL})

---
*由 AI Builders Digest 自动生成 · 每周一、周五上午 9:30 更新*"""

    send_wecom(wecom_msg)

    if push_ok:
        log("🎉 全部完成！")
    else:
        log("⚠️  HTML 已生成，但 GitHub 推送失败，请检查 GITHUB_TOKEN")

if __name__ == "__main__":
    main()
