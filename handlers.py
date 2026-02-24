"""
handlers.py — Telegram command & conversation handlers for PolyTrack Bot
=========================================================================
Defines every handler that the ApplicationBuilder registers:
  • /start        → main menu
  • /help
  • /my_wallets
  • /history      → last 5 trades for a wallet (for testing / manual check)
  • /remove_wallet
  • ConversationHandler for adding a wallet (multi-step wizard)
  • Inline-button callbacks (remove wallet, history picker)
"""

import re
import logging
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode

import db
import api

logger = logging.getLogger(__name__)

# ─── Conversation states ──────────────────────────────────────────────────────
(
    STATE_WALLET,
    STATE_NICKNAME,
    STATE_MIN_USD,
    STATE_ONLY_BUYS,
) = range(4)

# ─── Wallet address regex (0x + 40-42 hex chars) ─────────────────────────────
WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40,42}$")

# ─── Max wallets per user (prevent abuse) ────────────────────────────────────
MAX_WALLETS = 10

# ─── All menu button labels (used in fallback matching) ──────────────────────
MENU_BUTTONS = frozenset(
    ["➕ Add Wallet", "📋 My Wallets", "🗑️ Remove Wallet", "🕐 History", "❓ Help"]
)


# ─────────────────────────────────────────────────────────────────────────────
#  Shared UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard shown after /start."""
    return ReplyKeyboardMarkup(
        [
            ["➕ Add Wallet",  "📋 My Wallets"],
            ["🕐 History",     "🗑️ Remove Wallet"],
            ["❓ Help"],
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an option…",
    )


def _cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["❌ Cancel"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _esc(text: str) -> str:
    """
    Escape special MarkdownV2 characters.
    NOTE: We intentionally do NOT use ~ in any message templates (it triggers
    strikethrough formatting). Use the ≈ character instead for 'approximately'.
    """
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


def _build_trade_line(
    i: int,
    emoji: str,
    t_type: str,
    outcome_str: str,
    size_str: str,
    price: float,
    usd_value: float,
    dt_str: str,
    market_title: str | None,
) -> str:
    """
    Build one trade line for history display.
    Uses ≈ (U+2248) instead of ~ to show 'approximately', avoiding
    Telegram's strikethrough parser which treats ~ as a formatting marker.
    """
    title_line = ""
    if market_title:
        truncated = market_title[:55] + ("…" if len(market_title) > 55 else "")
        title_line = f"\n    • {_esc(truncated)}"

    return (
        f"*{i}\\.* {emoji} *{_esc(t_type)}*{outcome_str} — "
        f"`{_esc(size_str)}` shares @ `${price:.3f}`{title_line}\n"
        f"    💵 ≈`${usd_value:,.2f}` \\| 📅 {_esc(dt_str)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.upsert_user(user.id, user.username, update.effective_chat.id)

    await update.message.reply_text(
        f"👋 *Welcome to PolyTrack, {_esc(user.first_name)}\\!*\n\n"
        "I'm your personal Polymarket trade monitor\\. "
        "Add any public wallet address and I'll ping you the moment a trade lands\\.\n\n"
        "🔍 *What I can do:*\n"
        "• Track multiple wallets simultaneously\n"
        "• Filter by minimum trade size \\(USD\\)\n"
        "• Alert only on BUY trades if you prefer\n"
        "• Show the last 5 trades of any wallet on demand\n"
        "• Send rich, real\\-time notifications\n\n"
        "Use the menu below to get started\\!",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /help
# ─────────────────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 *PolyTrack Bot — Help*\n\n"
    "*Commands:*\n"
    "  /start — Show main menu\n"
    "  /add\\_wallet — Add a wallet to track\n"
    "  /my\\_wallets — List your tracked wallets\n"
    "  /history \\[address\\] — Show last 5 trades\n"
    "  /remove\\_wallet — Stop tracking a wallet\n"
    "  /help — This message\n\n"
    "*How it works:*\n"
    "Every 45 seconds I query the Polymarket Data API for new trades "
    "on each wallet you're watching\\. "
    "When a trade matches your filters, I send you an alert\\.\n\n"
    "*Testing:*\n"
    "Use 🕐 *History* \\(or `/history 0x…`\\) to instantly see the last "
    "5 trades of any wallet without waiting for a poll cycle\\.\n\n"
    "*Privacy:*\n"
    "Only *public* on\\-chain wallet addresses are used\\. "
    "I never ask for private keys or seed phrases\\.\n\n"
    f"Each user can track up to *{MAX_WALLETS} wallets*\\."
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /my_wallets
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_my_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    rows = db.get_wallets_for_user(user.id)

    if not rows:
        await update.message.reply_text(
            "📭 You're not tracking any wallets yet\\.\n"
            "Tap *➕ Add Wallet* to get started\\!",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )
        return

    lines = ["📋 *Your Tracked Wallets*\n"]
    for row in rows:
        addr     = row["wallet_address"]
        nick     = row["nickname"] or "—"
        min_usd  = row["min_usd_threshold"]
        only_buy = "✅ Yes" if row["only_buys"] else "❌ No"
        short    = f"`{addr[:6]}…{addr[-4:]}`"
        lines.append(
            f"*{_esc(nick)}* \\({short}\\)\n"
            f"  💵 Min USD: `${min_usd:.0f}` \\| Buys only: {only_buy}\n"
            f"  🆔 ID: `{row['id']}`\n"
        )

    lines.append("_Tap 🗑️ Remove Wallet to stop tracking one\\._")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /history — last 5 trades for a wallet (great for testing)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /history [wallet_address]

    If a wallet address is provided → fetch directly.
    If the user has watched wallets → show an inline picker.
    If only one wallet → fetch directly.
    """
    user = update.effective_user

    # ── Argument provided: /history 0x... ──────────────────────────────────
    if context.args:
        wallet = context.args[0].strip()
        if not WALLET_RE.match(wallet):
            await update.message.reply_text(
                "❌ Invalid wallet address\\.\n"
                "Must start with `0x` followed by 40\\-42 hex characters\\.\n\n"
                "Example: `/history 0xAbCd1234`",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=_main_menu_keyboard(),
            )
            return
        await _send_history(update.message, wallet, None)
        return

    # ── No argument — check user's watched wallets ──────────────────────────
    rows = db.get_wallets_for_user(user.id)

    if not rows:
        await update.message.reply_text(
            "📭 You have no tracked wallets yet\\.\n\n"
            "You can also check any wallet directly:\n"
            "`/history 0xYourWalletAddress`",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )
        return

    if len(rows) == 1:
        row = rows[0]
        await _send_history(update.message, row["wallet_address"], row["nickname"])
        return

    # Multiple wallets — let the user pick via inline keyboard
    buttons = []
    for row in rows:
        label    = row["nickname"] or f"{row['wallet_address'][:8]}…"
        nick_val = row["nickname"] or ""
        buttons.append([
            InlineKeyboardButton(
                f"📊 {label}",
                callback_data=f"hist:{row['wallet_address']}:{nick_val}",
            )
        ])

    await update.message.reply_text(
        "🕐 *Trade History*\n\nWhich wallet would you like to check?",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _send_history(message, wallet: str, nickname: str | None) -> None:
    """
    Fetch the last 5 trades for *wallet* and reply with a formatted list.
    Falls back to plain text if MarkdownV2 parsing fails.
    """
    short = f"`{wallet[:6]}…{wallet[-4:]}`"
    sent = await message.reply_text(
        f"🔍 Fetching last 5 trades for {short}\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    trades = await api.fetch_trades(wallet, limit=5)

    if not trades:
        await sent.edit_text(
            f"📭 No trades found for {short}\\.\n\n"
            "_The wallet may have no activity, or the Polymarket API is "
            "temporarily unavailable\\. Try again in a moment\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    wallet_disp = nickname or f"{wallet[:6]}…{wallet[-4:]}"
    count       = min(len(trades), 5)
    lines       = [f"🕐 *Last {count} Trade{'s' if count > 1 else ''} — {_esc(wallet_disp)}*\n"]

    for i, trade in enumerate(trades[:5], 1):
        t_type    = api.parse_trade_type(trade)
        price     = api.parse_trade_price(trade)
        usd_value = api.parse_trade_usd_value(trade)
        size      = api.parse_trade_size(trade)
        outcome   = api.parse_trade_outcome(trade)
        ts        = api.parse_trade_timestamp(trade)
        market_id = api.parse_market_id(trade)

        emoji       = "💰" if t_type == "BUY" else "📉"
        dt_str      = (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %H:%M UTC")
            if ts else "Unknown time"
        )
        outcome_str = f" {_esc(outcome)}" if outcome else ""
        size_str    = f"{size:,.0f}" if size >= 1 else f"{size:.4f}"

        market_title = None
        if market_id:
            try:
                market_title = await api.fetch_market_title(market_id)
            except Exception:
                pass

        lines.append(_build_trade_line(
            i, emoji, t_type, outcome_str, size_str,
            price, usd_value, dt_str, market_title,
        ))

    poly_url = f"https://polymarket.com/profile/{wallet}?tab=activity"
    lines.append(f"\n[🔗 View full activity on Polymarket]({poly_url})")

    text = "\n".join(lines)
    try:
        await sent.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.warning("MarkdownV2 failed, sending plain text: %s", exc)
        # Strip markup and send as plain text fallback
        plain = text.replace("*", "").replace("`", "").replace("\\", "")
        await sent.edit_text(plain, disable_web_page_preview=True)


async def callback_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the inline wallet picker for /history."""
    query = update.callback_query
    await query.answer()

    # callback_data format: "hist:{wallet}:{nickname}"
    parts    = query.data.split(":", 2)
    wallet   = parts[1] if len(parts) > 1 else ""
    nickname = parts[2] if len(parts) > 2 and parts[2] else None

    if not wallet:
        await query.edit_message_text("⚠️ Invalid selection\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    short = f"`{wallet[:6]}…{wallet[-4:]}`"
    await query.edit_message_text(
        f"🔍 Fetching last 5 trades for {short}\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    trades = await api.fetch_trades(wallet, limit=5)

    if not trades:
        await query.edit_message_text(
            f"📭 No trades found for {short}\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    wallet_disp = nickname or f"{wallet[:6]}…{wallet[-4:]}"
    count       = min(len(trades), 5)
    lines       = [f"🕐 *Last {count} Trade{'s' if count > 1 else ''} — {_esc(wallet_disp)}*\n"]

    for i, trade in enumerate(trades[:5], 1):
        t_type    = api.parse_trade_type(trade)
        price     = api.parse_trade_price(trade)
        usd_value = api.parse_trade_usd_value(trade)
        size      = api.parse_trade_size(trade)
        outcome   = api.parse_trade_outcome(trade)
        ts        = api.parse_trade_timestamp(trade)
        market_id = api.parse_market_id(trade)

        emoji       = "💰" if t_type == "BUY" else "📉"
        dt_str      = (
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %H:%M UTC")
            if ts else "Unknown time"
        )
        outcome_str = f" {_esc(outcome)}" if outcome else ""
        size_str    = f"{size:,.0f}" if size >= 1 else f"{size:.4f}"

        market_title = None
        if market_id:
            try:
                market_title = await api.fetch_market_title(market_id)
            except Exception:
                pass

        lines.append(_build_trade_line(
            i, emoji, t_type, outcome_str, size_str,
            price, usd_value, dt_str, market_title,
        ))

    poly_url = f"https://polymarket.com/profile/{wallet}?tab=activity"
    lines.append(f"\n[🔗 View full activity on Polymarket]({poly_url})")

    text = "\n".join(lines)
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.warning("MarkdownV2 failed in callback_history, falling back: %s", exc)
        plain = text.replace("*", "").replace("`", "").replace("\\", "")
        await query.edit_message_text(plain, disable_web_page_preview=True)


# ─────────────────────────────────────────────────────────────────────────────
#  /remove_wallet — inline keyboard approach
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_remove_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    rows = db.get_wallets_for_user(user.id)

    if not rows:
        await update.message.reply_text(
            "📭 You have no wallets to remove\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )
        return

    buttons = []
    for row in rows:
        addr  = row["wallet_address"]
        label = row["nickname"] or f"{addr[:8]}…"
        buttons.append([
            InlineKeyboardButton(
                f"🗑️ {label}",
                callback_data=f"remove:{row['id']}",
            )
        ])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="remove:cancel")])

    await update.message.reply_text(
        "🗑️ *Remove a Wallet*\n\nChoose which wallet to stop tracking:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_remove_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    await query.answer()
    user    = query.from_user
    payload = query.data  # "remove:<id>" or "remove:cancel"

    if payload == "remove:cancel":
        await query.edit_message_text("✅ No changes made\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    try:
        wallet_id = int(payload.split(":")[1])
    except (IndexError, ValueError):
        await query.edit_message_text("⚠️ Invalid action\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    removed = db.remove_wallet(wallet_id, user.id)
    if removed:
        await query.edit_message_text(
            "✅ Wallet removed\\. You'll no longer receive alerts for it\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await query.edit_message_text(
            "⚠️ Wallet not found \\(maybe already removed\\?\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Add-wallet ConversationHandler steps
# ─────────────────────────────────────────────────────────────────────────────

async def conv_start_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: /add_wallet or ➕ Add Wallet button."""
    user  = update.effective_user
    count = db.count_wallets_for_user(user.id)

    if count >= MAX_WALLETS:
        await update.message.reply_text(
            f"⚠️ You've reached the limit of *{MAX_WALLETS} wallets*\\.\n"
            "Please remove one before adding another\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "➕ *Add Wallet — Step 1 of 4*\n\n"
        "Please send me the Polymarket wallet address you want to track\\.\n"
        "_Example: `0xAbCd1234`_\n\n"
        "Tap *❌ Cancel* at any time to abort\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_cancel_keyboard(),
    )
    return STATE_WALLET


async def conv_receive_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    # If user tapped a menu button while conversation was active → cancel & re-route
    if text in MENU_BUTTONS or text == "❌ Cancel":
        await _cancel(update, context)
        if text in MENU_BUTTONS and text != "❌ Cancel":
            await handle_menu_text(update, context)
        return ConversationHandler.END

    if not WALLET_RE.match(text):
        await update.message.reply_text(
            "❌ That doesn't look like a valid wallet address\\.\n"
            "It must start with `0x` followed by 40\\-42 hex characters\\.\n\n"
            "Try again, or tap ❌ Cancel:",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return STATE_WALLET

    # Check for duplicate
    existing = [r["wallet_address"] for r in db.get_wallets_for_user(update.effective_user.id)]
    if text.lower() in existing:
        await update.message.reply_text(
            "⚠️ You're already tracking that wallet\\!\n"
            "Send a different address, or tap ❌ Cancel:",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return STATE_WALLET

    context.user_data["wallet"] = text.lower()

    await update.message.reply_text(
        "✅ *Step 2 of 4 — Nickname*\n\n"
        "Give this wallet a friendly nickname \\(e\\.g\\. `Whale 1`\\)\\.\n"
        "Or send /skip to use the short address\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_cancel_keyboard(),
    )
    return STATE_NICKNAME


async def conv_receive_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text in MENU_BUTTONS or text == "❌ Cancel":
        await _cancel(update, context)
        if text in MENU_BUTTONS and text != "❌ Cancel":
            await handle_menu_text(update, context)
        return ConversationHandler.END

    context.user_data["nickname"] = None if text.lower() in ("/skip", "skip") else text[:32]

    await update.message.reply_text(
        "✅ *Step 3 of 4 — Minimum Trade Size*\n\n"
        "Only alert me when the trade value is at least how many USD?\n"
        "_Send a number like `100` or `0` for all trades\\._\n"
        "Or send /skip for no minimum\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_cancel_keyboard(),
    )
    return STATE_MIN_USD


async def conv_receive_min_usd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text in MENU_BUTTONS or text == "❌ Cancel":
        await _cancel(update, context)
        if text in MENU_BUTTONS and text != "❌ Cancel":
            await handle_menu_text(update, context)
        return ConversationHandler.END

    if text.lower() in ("/skip", "skip"):
        context.user_data["min_usd"] = 0.0
    else:
        try:
            val = float(text.replace("$", "").replace(",", ""))
            if val < 0:
                raise ValueError
            context.user_data["min_usd"] = val
        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid positive number \\(e\\.g\\. `50` or `0`\\):",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return STATE_MIN_USD

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ BUY trades only", callback_data="onlybuys:yes"),
            InlineKeyboardButton("📊 All trades",       callback_data="onlybuys:no"),
        ]
    ])
    await update.message.reply_text(
        "✅ *Step 4 of 4 — Trade Filter*\n\n"
        "Should I alert you only on *BUY* trades, or all trades \\(SELL included\\)?",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
    )
    return STATE_ONLY_BUYS


async def conv_receive_only_buys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    only_buys = query.data == "onlybuys:yes"
    user      = query.from_user
    wallet    = context.user_data.get("wallet", "")
    nick      = context.user_data.get("nickname")
    min_usd   = context.user_data.get("min_usd", 0.0)

    success = db.add_wallet(user.id, wallet, nick, min_usd, only_buys)

    nick_display = _esc(nick) if nick else f"`{wallet[:6]}…{wallet[-4:]}`"
    filter_text  = "BUY trades only" if only_buys else "all trades"
    min_usd_text = f"${min_usd:,.0f}" if min_usd else "no minimum"

    if success:
        await query.edit_message_text(
            f"🎉 *Wallet added successfully\\!*\n\n"
            f"📍 *Name:*    {nick_display}\n"
            f"💵 *Min size:* `{_esc(min_usd_text)}`\n"
            f"🔍 *Filter:*  {_esc(filter_text)}\n\n"
            "_I'll start sending alerts within 45 seconds\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await query.edit_message_text(
            "⚠️ Could not add wallet \\(it may already be tracked\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    # Restore main keyboard
    await context.bot.send_message(
        chat_id      = query.message.chat_id,
        text         = "Use the menu below to manage your wallets\\.",
        parse_mode   = ParseMode.MARKDOWN_V2,
        reply_markup = _main_menu_keyboard(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled\\. No changes were made\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
#  Reply keyboard menu text routing
# ─────────────────────────────────────────────────────────────────────────────

async def handle_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route taps on the persistent reply keyboard to the right handler."""
    text = update.message.text

    if text == "➕ Add Wallet":
        await conv_start_add(update, context)
    elif text == "📋 My Wallets":
        await cmd_my_wallets(update, context)
    elif text == "🗑️ Remove Wallet":
        await cmd_remove_wallet(update, context)
    elif text == "🕐 History":
        await cmd_history(update, context)
    elif text == "❓ Help":
        await cmd_help(update, context)
    else:
        await update.message.reply_text(
            "🤔 I didn't understand that\\. Use the menu below or /help\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Alert formatting (called from the background polling job in bot.py)
# ─────────────────────────────────────────────────────────────────────────────

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
    """
    Build the MarkdownV2 alert message sent to the user on new trades.
    Uses ≈ instead of ~ to show 'approximately' — avoids strikethrough parser.
    """
    emoji       = "💰" if trade_type == "BUY" else "📉"
    wallet_disp = nickname or f"{wallet_address[:6]}…{wallet_address[-4:]}"
    dt_str      = (
        datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if timestamp else "Unknown time"
    )
    size_str    = f"{size:,.0f}" if size >= 1 else f"{size:.4f}"
    outcome_str = f" {_esc(outcome)}" if outcome else ""

    title_part = ""
    if market_title:
        truncated  = market_title[:60] + ("…" if len(market_title) > 60 else "")
        title_part = f"\n    • {_esc(truncated)}"

    return "\n".join([
        "🔔 *New Polymarket Trade\\!*\n",
        f"👤 *Wallet:* {_esc(wallet_disp)}",
        f"    `{wallet_address[:6]}…{wallet_address[-4:]}`\n",
        f"{emoji} *{_esc(trade_type)}*{outcome_str} — "
        f"`{_esc(size_str)}` shares @ `${price:.3f}`{title_part}\n",
        f"💵 *Value:* ≈`${usd_value:,.2f}`",
        f"📅 {_esc(dt_str)}\n",
        f"[🔗 View wallet activity]({polymarket_url})",
    ])


# ─────────────────────────────────────────────────────────────────────────────
#  ConversationHandler factory (imported by bot.py)
# ─────────────────────────────────────────────────────────────────────────────

def build_add_wallet_conversation() -> ConversationHandler:
    """Build and return the multi-step add-wallet ConversationHandler."""

    # Any menu button tap while in a conversation gracefully cancels it
    menu_fallback = MessageHandler(
        filters.Regex(r"^(➕ Add Wallet|📋 My Wallets|🗑️ Remove Wallet|🕐 History|❓ Help|❌ Cancel)$"),
        _cancel,
    )

    return ConversationHandler(
        entry_points=[
            CommandHandler("add_wallet", conv_start_add),
            MessageHandler(filters.Regex(r"^➕ Add Wallet$"), conv_start_add),
        ],
        states={
            STATE_WALLET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_receive_wallet),
            ],
            STATE_NICKNAME: [
                CommandHandler("skip", conv_receive_nickname),
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_receive_nickname),
            ],
            STATE_MIN_USD: [
                CommandHandler("skip", conv_receive_min_usd),
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_receive_min_usd),
            ],
            STATE_ONLY_BUYS: [
                CallbackQueryHandler(conv_receive_only_buys, pattern=r"^onlybuys:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _cancel),
            menu_fallback,
        ],
        allow_reentry=True,
        per_message=False,
        name="add_wallet_conv",
    )
