"""
bot.py — PolyTrack Telegram Bot — Entry Point
==============================================

╔══════════════════════════════════════════════════════════════════════════════╗
║              WINDOWS DEVELOPER SETUP INSTRUCTIONS                          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  1. Install Python 3.10+ from https://www.python.org/downloads/             ║
║     ✅ Check "Add Python to PATH" during installation                       ║
║                                                                              ║
║  2. Open Command Prompt or PowerShell in this project folder                ║
║     (Right-click in Explorer → "Open in Terminal")                          ║
║                                                                              ║
║  3. Create virtual environment:                                             ║
║       python -m venv venv                                                   ║
║                                                                              ║
║  4. Activate it:                                                            ║
║       venv\\Scripts\\activate         ← CMD                                  ║
║       .\\venv\\Scripts\\Activate.ps1  ← PowerShell                           ║
║     You'll see (venv) in your prompt when active.                           ║
║                                                                              ║
║  5. Install dependencies:                                                   ║
║       pip install -r requirements.txt                                       ║
║                                                                              ║
║  6. Get your BOT_TOKEN:                                                     ║
║     a) Open Telegram → search for @BotFather                                ║
║     b) Send /newbot → follow prompts → copy the token                       ║
║                                                                              ║
║  7. Create your .env file:                                                  ║
║       copy .env.example .env       ← CMD                                    ║
║       cp .env.example .env         ← PowerShell / Git Bash                  ║
║     Then open .env and paste your token after BOT_TOKEN=                   ║
║                                                                              ║
║  8. Run locally:                                                            ║
║       python bot.py                                                         ║
║     The bot will start polling. Open Telegram and send /start               ║
║                                                                              ║
║  ─────────────────────────────────── VPS DEPLOY (Linux) ─────────────────  ║
║                                                                              ║
║  1. SSH into your VPS:                                                      ║
║       ssh user@your-server-ip                                               ║
║                                                                              ║
║  2. Copy project files (from your local machine):                           ║
║       scp -r . user@your-server-ip:/home/user/polytrack/                   ║
║     OR clone from GitHub:                                                   ║
║       git clone https://github.com/you/polytrack.git                       ║
║                                                                              ║
║  3. On VPS — create venv & install:                                         ║
║       cd polytrack                                                           ║
║       python3 -m venv venv                                                  ║
║       source venv/bin/activate                                              ║
║       pip install -r requirements.txt                                       ║
║                                                                              ║
║  4. Create .env on VPS:                                                     ║
║       nano .env                                                             ║
║     Add: BOT_TOKEN=your_token                                               ║
║                                                                              ║
║  5. Create systemd service (/etc/systemd/system/polytrack.service):         ║
║                                                                              ║
║     [Unit]                                                                  ║
║     Description=PolyTrack Telegram Bot                                      ║
║     After=network.target                                                    ║
║                                                                              ║
║     [Service]                                                               ║
║     Type=simple                                                             ║
║     User=ubuntu                                                             ║
║     WorkingDirectory=/home/ubuntu/polytrack                                 ║
║     ExecStart=/home/ubuntu/polytrack/venv/bin/python bot.py                 ║
║     Restart=always                                                          ║
║     RestartSec=10                                                           ║
║     StandardOutput=journal                                                  ║
║     StandardError=journal                                                   ║
║                                                                              ║
║     [Install]                                                               ║
║     WantedBy=multi-user.target                                              ║
║                                                                              ║
║  6. Enable & start:                                                         ║
║       sudo systemctl daemon-reload                                           ║
║       sudo systemctl enable polytrack                                       ║
║       sudo systemctl start  polytrack                                       ║
║       sudo journalctl -fu polytrack   ← live logs                           ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

import db
import api
from handlers import (
    cmd_start,
    cmd_help,
    cmd_my_wallets,
    cmd_history,
    callback_history,
    cmd_remove_wallet,
    callback_remove_wallet,
    handle_menu_text,
    build_add_wallet_conversation,
    format_trade_alert,
)

# ─────────────────────────────────────────────────────────────────────────────
#  Environment & logging setup
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()  # reads .env from project root

LOG_LEVEL    = os.getenv("LOG_LEVEL", "INFO").upper()
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "45"))
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")

# Console + rotating file logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("polytrack.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Background polling job
# ─────────────────────────────────────────────────────────────────────────────

async def poll_trades(context) -> None:
    """
    Called by JobQueue every POLL_INTERVAL seconds.

    Iterates every watched_wallet row across all users:
      1. Fetches recent trades from Polymarket
      2. Filters by timestamp, only_buys, and min_usd_threshold
      3. Sends alert messages to the wallet owner's chat
      4. Updates last_timestamp so we don't re-alert
    """
    bot: Bot = context.bot
    wallets   = db.get_all_wallets()

    if not wallets:
        return  # Nothing to poll

    logger.debug("Poll cycle — checking %d wallet(s)", len(wallets))

    for row in wallets:
        wallet_id   = row["id"]
        address     = row["wallet_address"]
        chat_id     = row["chat_id"]
        nickname    = row["nickname"]
        min_usd     = row["min_usd_threshold"]
        only_buys   = bool(row["only_buys"])
        last_ts     = row["last_timestamp"]

        try:
            trades = await api.fetch_trades(address)
        except Exception as exc:  # noqa: BLE001 — never crash the job loop
            logger.error("Error fetching trades for %s: %s", address[:10], exc)
            continue

        if not trades:
            continue

        # Determine the highest timestamp we'll see this cycle
        new_max_ts = last_ts

        # Trades are newest-first; process oldest-first so alerts arrive in order
        for trade in reversed(trades):
            ts        = api.parse_trade_timestamp(trade)
            trade_type = api.parse_trade_type(trade)
            size      = api.parse_trade_size(trade)
            price     = api.parse_trade_price(trade)
            usd_value = api.parse_trade_usd_value(trade)
            outcome   = api.parse_trade_outcome(trade)
            market_id = api.parse_market_id(trade)

            # ── Skip already-seen trades ────────────────────────────────────
            if ts <= last_ts:
                continue

            # ── Apply user filters ──────────────────────────────────────────
            if only_buys and trade_type != "BUY":
                new_max_ts = max(new_max_ts, ts)  # still advance cursor
                continue

            if usd_value < min_usd:
                new_max_ts = max(new_max_ts, ts)
                continue

            # ── Fetch market title (optional, best-effort) ──────────────────
            market_title = None
            if market_id:
                try:
                    market_title = await api.fetch_market_title(market_id)
                except Exception:  # noqa: BLE001
                    pass

            # ── Build Polymarket profile URL ────────────────────────────────
            poly_url = f"https://polymarket.com/profile/{address}?tab=activity"

            # ── Format & send the alert ─────────────────────────────────────
            msg = format_trade_alert(
                trade_type    = trade_type,
                size          = size,
                price         = price,
                usd_value     = usd_value,
                outcome       = outcome,
                market_title  = market_title,
                wallet_address = address,
                nickname      = nickname,
                timestamp     = ts,
                polymarket_url = poly_url,
            )

            try:
                await bot.send_message(
                    chat_id    = chat_id,
                    text       = msg,
                    parse_mode = ParseMode.MARKDOWN_V2,
                    disable_web_page_preview = True,
                )
                logger.info(
                    "Alert sent → chat %s | wallet %s | %s $%.2f",
                    chat_id, address[:10], trade_type, usd_value,
                )
            except Exception as send_exc:  # noqa: BLE001
                logger.error("Failed to send alert to chat %s: %s", chat_id, send_exc)

            new_max_ts = max(new_max_ts, ts)

        # ── Persist the highest timestamp seen ──────────────────────────────
        if new_max_ts > last_ts:
            db.update_last_timestamp(wallet_id, new_max_ts)

    logger.debug("Poll cycle complete.")


# ─────────────────────────────────────────────────────────────────────────────
#  Application bootstrap
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        logger.critical(
            "❌ BOT_TOKEN is not set!\n"
            "   Copy .env.example → .env and fill in your token from @BotFather."
        )
        raise SystemExit(1)

    logger.info("🚀 PolyTrack Bot starting up…")

    # Initialise SQLite schema (idempotent)
    db.init_db()

    # Build the Application
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    # ── Register handlers (order matters for dispatcher) ───────────────────

    # 1. Multi-step conversation for adding a wallet (highest priority)
    app.add_handler(build_add_wallet_conversation())

    # 2. Simple command handlers
    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("help",           cmd_help))
    app.add_handler(CommandHandler("my_wallets",     cmd_my_wallets))
    app.add_handler(CommandHandler("history",        cmd_history))
    app.add_handler(CommandHandler("remove_wallet",  cmd_remove_wallet))

    # 3. Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_remove_wallet, pattern=r"^remove:"))
    app.add_handler(CallbackQueryHandler(callback_history,       pattern=r"^hist:"))

    # 4. Reply keyboard button text routing (catch-all text messages)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_menu_text,
        )
    )

    # ── Background polling job ─────────────────────────────────────────────
    app.job_queue.run_repeating(
        poll_trades,
        interval = POLL_INTERVAL,
        first    = 10,          # first run 10 seconds after startup
        name     = "poll_trades",
    )

    logger.info(
        "✅ Bot is running. Polling interval: %ds. Press Ctrl+C to stop.",
        POLL_INTERVAL,
    )

    # Start polling (blocks until stopped)
    app.run_polling(
        allowed_updates = ["message", "callback_query"],
        drop_pending_updates = True,   # ignore messages sent while bot was offline
    )

    # Cleanup after stop
    asyncio.get_event_loop().run_until_complete(api.close_session())
    logger.info("👋 Bot shut down gracefully.")


if __name__ == "__main__":
    main()
