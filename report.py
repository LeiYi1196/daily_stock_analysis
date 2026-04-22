"""
Trading 212 持仓日报
每日自动拉取持仓 -> Gemini分析 -> 推送 Discord + Telegram
"""

import os
import json
import requests
import base64
from datetime import datetime

# ===== 配置 =====
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

T212_API_KEY = os.environ["T212_API_KEY"]
T212_API_SECRET = os.environ["T212_API_SECRET"]
credentials = base64.b64encode(f"{T212_API_KEY}:{T212_API_SECRET}".encode()).decode()
T212_HEADERS = {"Authorization": f"Basic {credentials}"}
T212_BASE = "https://live.trading212.com/api/v0"


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


def get_account_info():
    """获取账户基本信息（货币等）"""
    r = requests.get(f"{T212_BASE}/equity/account/info", headers=T212_HEADERS, timeout=15)
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
        ppl = pos.get("ppl", 0)  # profit/loss

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

    # 按盈亏百分比排序
    stocks.sort(key=lambda x: x["pct_change"], reverse=True)

    total_ppl = total_current - total_invested
    total_ppl_pct = (total_ppl / total_invested * 100) if total_invested else 0
    cash = cash_info.get("free", 0)
    total_assets = total_current + cash

    # 计算每只股票占总资产比例
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

    # 单只跌幅超过-10%
    for s in stocks:
        if s["pct_change"] <= -10:
            warnings.append(f"⚠️ {s['ticker']} 跌幅已达 {s['pct_change']:.1f}%，注意止损")

    # 单只仓位超过35%
    for s in stocks:
        if s["weight"] >= 35:
            warnings.append(f"⚠️ {s['ticker']} 仓位占比 {s['weight']:.1f}%，集中度过高")

    # 整体亏损超过15%
    if stats["total_ppl_pct"] <= -15:
        warnings.append(f"🚨 整体持仓亏损已达 {stats['total_ppl_pct']:.1f}%，建议审查策略")

    # 现金占比过低
    cash_pct = (stats["cash"] / stats["total_assets"] * 100) if stats["total_assets"] else 0
    if cash_pct < 5 and stats["cash"] < 500:
        warnings.append(f"⚠️ 可用现金仅 {stats['cash']:.0f}，应对波动能力有限")

    return warnings


# ===== 3. Gemini AI 分析 =====

def ai_analysis(stats):
    """调用 Gemini 生成持仓分析"""
    stocks_summary = "\n".join([
        f"- {s['ticker']}: 持仓{s['quantity']:.2f}股, 均价{s['avg_price']:.2f}, "
        f"现价{s['current_price']:.2f}, 盈亏{s['pct_change']:+.1f}%, 占仓{s['weight']:.1f}%"
        for s in stats["stocks"]
    ])

    prompt = f"""你是一位专业的美股投资顾问。请根据以下持仓数据给出简洁的分析报告（中文，控制在400字以内）。

## 持仓数据（{datetime.now().strftime('%Y-%m-%d')}）
总资产: ${stats['total_assets']:.0f}
总投入: ${stats['total_invested']:.0f}
当前市值: ${stats['total_current']:.0f}
总盈亏: ${stats['total_ppl']:+.0f} ({stats['total_ppl_pct']:+.1f}%)
持仓胜率: {stats['winning']}/{stats['winning']+stats['losing']}
可用现金: ${stats['cash']:.0f}

各持仓明细:
{stocks_summary}

请从以下四个维度分析（每个维度2-3句话）：
1. 📊 整体持仓健康度评估
2. 🎯 表现最好/最差的持仓点评
3. 💡 操作建议（加仓/减仓/持有/止损）
4. 🛡️ 主要风险提示

语气专业简洁，不要废话，直接给结论。"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


# ===== 4. 格式化报告 =====

def format_report(stats, risks, ai_text):
    """格式化完整报告"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    total_ppl = stats["total_ppl"]
    total_ppl_pct = stats["total_ppl_pct"]

    # 整体盈亏 emoji
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

    # 风险预警
    if risks:
        lines.append("")
        lines.append("**🚨 风险预警**")
        lines.extend(risks)

    # AI 分析
    lines.append("")
    lines.append("**🤖 AI 分析**")
    lines.append(ai_text)

    lines.append("")
    lines.append("---")
    lines.append("*仅供参考，不构成投资建议*")

    return "\n".join(lines)


# ===== 5. 推送 =====

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
    # Telegram 支持 Markdown，但格式略有不同，这里用纯文本
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

    print("调用 Gemini 分析...")
    ai_text = ai_analysis(stats)

    report = format_report(stats, risks, ai_text)

    print("推送报告...")
    send_discord(report)
    send_telegram(report)

    print("完成！")


if __name__ == "__main__":
    main()
