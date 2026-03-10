import time
from datetime import datetime, timezone

def _esc(text: str) -> str:
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))

def format_trade_alert(
    trade_type: str,
    size: float,
    price: float,
    usd_value: float,
    outcome: str,
    market_title: str | None,
    wallet_address: str,
    nickname: str | None,
    timestamp: int,
    polymarket_url: str,
) -> str:
    emoji       = "🟢" if trade_type == "BUY" else "🔴"
    wallet_disp = nickname or f"{wallet_address[:6]}...{wallet_address[-4:]}"
    dt_str      = (
        datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if timestamp else "Unknown time"
    )
    
    # Do not use `_esc` if it will be placed inside `\\` backticks `\\`! 
    size_str    = f"{size:,.0f}" if size >= 1 else f"{size:.4f}"
    
    outcome_str = f" `{_esc(outcome)}`" if outcome else ""

    market_str = _esc(market_title) if market_title else "Unknown Market"

    import time as _time
    now_ts     = int(_time.time())
    age_secs   = max(0, now_ts - timestamp) if timestamp else 0
    if age_secs < 60:
        age_str = f"{age_secs}s ago"
    elif age_secs < 3600:
        age_str = f"{age_secs // 60}m {age_secs % 60}s ago"
    else:
        age_str = f"{age_secs // 3600}h ago"

    return "\n".join([
        "🚨 *NEW POLYMARKET DETECTED* 🚨\n",
        f"👤 *Wallet:* ` {_esc(wallet_disp)} `",
        f"      ↳ ` {wallet_address[:6]}...{wallet_address[-4:]} `\n",
        f"📊 *Market:* *{market_str}*",
        f"🎯 *Action:* {emoji} *{_esc(trade_type)}*{outcome_str}",
        f"💰 *Size:* `{size_str}` shares",
        f"💲 *Price:* `${price:.3f}`",
        f"💵 *Value:* ≈`${usd_value:,.2f}`\n",
        f"⏱ *Time:* `{_esc(dt_str)}` _({_esc(age_str)})_",
        f"\n🔗 [*View Wallet on Polymarket*]({polymarket_url})",
    ])

print(format_trade_alert(
    "BUY", 1000.5, 0.543, 543.21, "Yes", "Donald Trump vs Joe Biden - 2024",
    "0x1234567890abcdef1234567890abcdef12345678", "Whale #1", int(time.time()),
    "https://polymarket.com/profile/0x...1234"
))
