"""
Trading 212 持仓日报
每日自动拉取持仓 -> 搜索新闻 -> Claude分析 -> 推送 Discord + Telegram
"""

import os
import json
import requests
import base64
from datetime import datetime

# ===== 配置 =====
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

T212_API_KEY = os.environ["T212_API_KEY"]
T212_API_SECRET = os.environ["T212_API_SECRET"]
credentials = base64.b64encode(f"{T212_API_KEY}:{T212_API_SECRET}".encode()).decode()
T212_HEADERS = {"Authorization": f"Basic {credentials}"}
T212_BASE = "https://live.trading212.com/api/v0"

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
SOCIAL_SENTIMENT_API_KEY = os.environ.get("SOCIAL_SENTIMENT_API_KEY", "")


# ===== 1. 拉取 Trading 212 数据 =====

def get_portfolio():
    """获取当前持仓"""
    r = requests.get(f"{T212_BASE}/equity/portfolio", headers=T212_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_account_cash():
    """获取账户现金"""
    r = requests.get(f"{T212_BASE}/equity/account/cash", headers=T212_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


# ===== 2. 数据计算 =====

def calc_portfolio_stats(positions, cash_info):
    """计算持仓统计"""
    total_invested = 0
    total_current = 0
    stocks = []

    for pos in positions:
        ticker = pos.get("ticker", "").replace("_US_EQ", "").replace("_EQ", "")
        quantity = pos.get("quantity", 0)
        avg_price = pos.get("averagePrice", 0)
        current_price = pos.get("currentPrice", 0)
        ppl = pos.get("ppl", 0)

        invested = quantity * avg_price
        current_value = quantity * current_price
        pct_change = ((current_price - avg_price) / avg_price * 100) if avg_price else 0

        total_invested += invested
        total_current += current_value

        stocks.append({
            "ticker": ticker,
            "quantity": quantity,
            "avg_price": avg_price,
            "current_price": current_price,
            "invested": invested,
            "current_value": current_value,
            "ppl": ppl,
            "pct_change": pct_change,
        })

    stocks.sort(key=lambda x: x["pct_change"], reverse=True)

    total_ppl = total_current - total_invested
    total_ppl_pct = (total_ppl / total_invested * 100) if total_invested else 0
    cash = cash_info.get("free", 0)
    total_assets = total_current + cash

    for s in stocks:
        s["weight"] = (s["current_value"] / total_assets * 100) if total_assets else 0

    return {
        "stocks": stocks,
        "total_invested": total_invested,
        "total_current": total_current,
        "total_ppl": total_ppl,
        "total_ppl_pct": total_ppl_pct,
        "cash": cash,
        "total_assets": total_assets,
        "winning": sum(1 for s in stocks if s["ppl"] > 0),
        "losing": sum(1 for s in stocks if s["ppl"] < 0),
    }


def detect_risks(stats):
    """风险检测"""
    warnings = []
    stocks = stats["stocks"]

    for s in stocks:
        if s["pct_change"] <= -10:
            warnings.append(f"⚠️ {s['ticker']} 跌幅已达 {s['pct_change']:.1f}%，注意止损")

    for s in stocks:
        if s["weight"] >= 35:
            warnings.append(f"⚠️ {s['ticker']} 仓位占比 {s['weight']:.1f}%，集中度过高")

    if stats["total_ppl_pct"] <= -15:
        warnings.append(f"🚨 整体持仓亏损已达 {stats['total_ppl_pct']:.1f}%，建议审查策略")

    cash_pct = (stats["cash"] / stats["total_assets"] * 100) if stats["total_assets"] else 0
    if cash_pct < 5 and stats["cash"] < 500:
        warnings.append(f"⚠️ 可用现金仅 ${stats['cash']:.0f}，应对波动能力有限")

    return warnings


# ===== 3. 新闻和情绪数据 =====

def fetch_news(tickers):
    """用 Tavily 搜索每只股票的最新新闻"""
    if not TAVILY_API_KEY:
        print("Tavily 未配置，跳过新闻搜索")
        return {}

    news_by_ticker = {}
    for ticker in tickers:
        try:
            r = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": TAVILY_API_KEY,
                    "query": f"{ticker} stock news today",
                    "search_depth": "basic",
                    "max_results": 3,
                    "days": 1
                },
                timeout=10
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            news_by_ticker[ticker] = [
                f"- {item['title']}: {item.get('content', '')[:150]}"
                for item in results
            ]
            print(f"{ticker} 获取到 {len(results)} 条新闻")
        except Exception as e:
            print(f"{ticker} 新闻获取失败: {e}")
            news_by_ticker[ticker] = []

    return news_by_ticker


def fetch_sentiment(tickers):
    """用 Social Sentiment API 获取美股情绪"""
    if not SOCIAL_SENTIMENT_API_KEY:
        print("Social Sentiment 未配置，跳过情绪分析")
        return {}

    sentiment_by_ticker = {}
    for ticker in tickers:
        try:
            r = requests.get(
                f"https://api.adanos.org/sentiment/{ticker}",
                headers={"Authorization": f"Bearer {SOCIAL_SENTIMENT_API_KEY}"},
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            sentiment_by_ticker[ticker] = data.get("sentiment", "unknown")
            print(f"{ticker} 情绪: {sentiment_by_ticker[ticker]}")
        except Exception as e:
            print(f"{ticker} 情绪获取失败: {e}")

    return sentiment_by_ticker


# ===== 4. Claude AI 分析 =====

def ai_analysis(stats, news_by_ticker={}, sentiment_by_ticker={}):
    """调用 Claude 生成持仓分析"""
    stocks_summary = "\n".join([
        f"- {s['ticker']}: 持仓{s['quantity']:.2f}股, 均价{s['avg_price']:.2f}, "
        f"现价{s['current_price']:.2f}, 盈亏{s['pct_change']:+.1f}%, 占仓{s['weight']:.1f}%"
        for s in stats["stocks"]
    ])

    news_summary = ""
    for ticker, articles in news_by_ticker.items():
        if articles:
            news_summary += f"\n{ticker} 最新消息:\n" + "\n".join(articles) + "\n"

    sentiment_summary = ""
    for ticker, sent in sentiment_by_ticker.items():
        sentiment_summary += f"{ticker}: {sent}  "

    prompt = f"""你是一位专业的美股交易员。现在是美股开盘前30分钟，请根据以下持仓数据和最新资讯给出今日操作建议（中文，控制在500字以内）。

## 持仓数据（{datetime.now().strftime('%Y-%m-%d')}）
总资产: ${stats['total_assets']:.0f}
总投入: ${stats['total_invested']:.0f}
当前市值: ${stats['total_current']:.0f}
总盈亏: ${stats['total_ppl']:+.0f} ({stats['total_ppl_pct']:+.1f}%)
持仓胜率: {stats['winning']}/{stats['winning']+stats['losing']}
可用现金: ${stats['cash']:.0f}

各持仓明细:
{stocks_summary}

## 今日相关新闻
{news_summary if news_summary else "暂无新闻数据"}

## 社交媒体情绪
{sentiment_summary if sentiment_summary else "暂无情绪数据"}

请给出以下内容：
1. 📊 整体持仓状态（1句话）
2. 🎯 今日重点关注（哪1-2只最值得注意，结合新闻说明原因）
3. ✅ 今日可以操作的（具体说：哪只，买入/加仓/减仓/止损，理由）
4. ⛔ 今日不要动的（哪几只，为什么）
5. 🛡️ 今日最大风险点

语气直接，给明确结论，不要模棱两可。结尾加一行：仅供参考，不构成投资建议。"""

    try:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}]
        }
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        print("Claude 分析成功")
        return data["content"][0]["text"]
    except Exception as e:
        print(f"Claude 调用失败: {e}")
        return "AI 分析暂时不可用"


# ===== 5. 格式化报告 =====

def format_report(stats, risks, ai_text):
    """格式化完整报告"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    total_ppl = stats["total_ppl"]
    total_ppl_pct = stats["total_ppl_pct"]

    overall_emoji = "📈" if total_ppl >= 0 else "📉"
    ppl_sign = "+" if total_ppl >= 0 else ""

    lines = [
        f"**{overall_emoji} 持仓日报 | {date_str}**",
        "",
        "**💼 账户概览**",
        f"总资产: `${stats['total_assets']:.0f}`  |  可用现金: `${stats['cash']:.0f}`",
        f"总盈亏: `{ppl_sign}${total_ppl:.0f}` (`{ppl_sign}{total_ppl_pct:.1f}%`)",
        f"持仓胜率: `{stats['winning']}/{stats['winning']+stats['losing']}` 盈利",
        "",
        "**📋 持仓明细**",
    ]

    for s in stats["stocks"]:
        if s["pct_change"] >= 5:
            icon = "🟢"
        elif s["pct_change"] >= 0:
            icon = "🔵"
        elif s["pct_change"] >= -5:
            icon = "🟡"
        else:
            icon = "🔴"

        sign = "+" if s["pct_change"] >= 0 else ""
        lines.append(
            f"{icon} **{s['ticker']}**  `{sign}{s['pct_change']:.1f}%`  "
            f"现价${s['current_price']:.2f}  盈亏${s['ppl']:+.0f}  占仓{s['weight']:.0f}%"
        )

    if risks:
        lines.append("")
        lines.append("**🚨 风险预警**")
        lines.extend(risks)

    lines.append("")
    lines.append("**🤖 AI 操作建议**")
    lines.append(ai_text)

    return "\n".join(lines)


# ===== 6. 推送 =====

def send_discord(content):
    """发送到 Discord，自动分块"""
    chunks = []
    current = ""
    for line in content.split("\n"):
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = line
        else:
            current += ("\n" if current else "") + line
    if current:
        chunks.append(current)

    for chunk in chunks:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk}, timeout=10)
        r.raise_for_status()
    print(f"Discord 推送成功，共 {len(chunks)} 条消息")


def send_telegram(content):
    """发送到 Telegram，自动分块"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    text = content.replace("**", "").replace("`", "")

    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "Markdown"
        }, timeout=10)
        r.raise_for_status()
    print(f"Telegram 推送成功，共 {len(chunks)} 条消息")


# ===== 主流程 =====

def main():
    print(f"[{datetime.now()}] 开始获取持仓数据...")

    positions = get_portfolio()
    cash_info = get_account_cash()

    if not positions:
        print("持仓为空，跳过分析")
        return

    print(f"获取到 {len(positions)} 只持仓")

    stats = calc_portfolio_stats(positions, cash_info)
    risks = detect_risks(stats)

    tickers = [s["ticker"] for s in stats["stocks"]]

    print("获取新闻数据...")
    news = fetch_news(tickers)

    print("获取情绪数据...")
    sentiment = fetch_sentiment(tickers)

    print("调用 Claude 分析...")
    ai_text = ai_analysis(stats, news, sentiment)

    report = format_report(stats, risks, ai_text)

    print("推送报告...")
    send_discord(report)
    send_telegram(report)

    print("完成！")


if __name__ == "__main__":
    main()
