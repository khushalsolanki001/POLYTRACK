"""
handlers.py — Telegram command & conversation handlers for PolyTrack Bot
=========================================================================
Defines every handler that the ApplicationBuilder registers:
  • /start  → main menu
  • /help
  • /my_wallets
  • ConversationHandler for adding a wallet (multi-step wizard)
  • Inline-button callbacks (remove wallet)
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

# ─────────────────────────────────────────────────────────────────────────────
#  Shared UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard shown after /start."""
    return ReplyKeyboardMarkup(
        [
            ["➕ Add Wallet",  "📋 My Wallets"],
            ["🗑️ Remove Wallet", "❓ Help"],
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


# ─────────────────────────────────────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.upsert_user(user.id, user.username, update.effective_chat.id)

    await update.message.reply_text(
        f"👋 *Welcome to PolyTrack, {user.first_name}\\!*\n\n"
        "I'm your personal Polymarket trade monitor\\. "
        "Add any public wallet address and I'll ping you the moment a trade is made\\.\n\n"
        "🔍 *What I can do:*\n"
        "• Track multiple wallets simultaneously\n"
        "• Filter by minimum trade size \\(USD\\)\n"
        "• Alert only on BUY trades if you prefer\n"
        "• Send rich, real\\-time notifications\n\n"
        "Use the menu below to get started\\!",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /help
# ─────────────────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 *PolyTrack Bot \\— Help*\n\n"
    "*Commands:*\n"
    "  /start — Show main menu\n"
    "  /add\\_wallet — Add a wallet to track\n"
    "  /my\\_wallets — List your tracked wallets\n"
    "  /help — This message\n\n"
    "*How it works:*\n"
    "Every 45 seconds I query the Polymarket Data API for new trades "
    "on each wallet you're watching\\. "
    "When a trade matches your filters, I send you an alert\\.\n\n"
    "*Privacy:*\n"
    "Only *public* on\\-chain wallet addresses are used\\. "
    "I never ask for private keys or seed phrases\\.\n\n"
    "*Limits:*\n"
    f"Each user can track up to {MAX_WALLETS} wallets\\.\n\n"
    "❓ Questions? Open an issue on GitHub\\."
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
    user  = update.effective_user
    rows  = db.get_wallets_for_user(user.id)

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
            f"*{nick}* \\({short}\\)\n"
            f"  💵 Min USD: `${min_usd:.0f}` \\| Buys only: {only_buy}\n"
            f"  🆔 ID: `{row['id']}`\n"
        )

    lines.append(
        "\n_Tap 🗑️ Remove Wallet to stop tracking one\\._"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Remove wallet — inline keyboard approach
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
        label = f"{row['nickname'] or addr[:8]+'…'}"
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
    query = update.callback_query
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
#  Add-wallet ConversationHandler
# ─────────────────────────────────────────────────────────────────────────────

async def conv_start_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: /add_wallet or button tap."""
    user = update.effective_user

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
        "Please send me the Ethereum wallet address you want to track\\.\n"
        "_Example: `0xAbCd…1234`_\n\n"
        "Tap ❌ Cancel at any time to abort\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_cancel_keyboard(),
    )
    return STATE_WALLET


async def conv_receive_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await _cancel(update, context)

    if not WALLET_RE.match(text):
        await update.message.reply_text(
            "❌ That doesn't look like a valid Ethereum address\\.\n"
            "It must start with `0x` followed by 40\\-42 hex characters\\.\n\n"
            "Please try again:",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return STATE_WALLET

    # Check for duplicate
    existing = [r["wallet_address"] for r in db.get_wallets_for_user(update.effective_user.id)]
    if text.lower() in existing:
        await update.message.reply_text(
            "⚠️ You're already tracking that wallet\\!\n"
            "Do you want to track a different one?",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return STATE_WALLET

    context.user_data["wallet"] = text.lower()

    await update.message.reply_text(
        "✅ *Step 2 of 4 — Nickname*\n\n"
        "Give this wallet a friendly nickname \\(e\\.g\\. `Whale #1`\\), "
        "or send /skip to use the address\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_cancel_keyboard(),
    )
    return STATE_NICKNAME


async def conv_receive_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await _cancel(update, context)

    if text.lower() in ("/skip", "skip"):
        context.user_data["nickname"] = None
    else:
        # Truncate very long nicknames
        context.user_data["nickname"] = text[:32]

    await update.message.reply_text(
        "✅ *Step 3 of 4 — Minimum Trade Size*\n\n"
        "Only alert me when the trade value is at least how many USD?\n"
        "_Enter a number like `100` or `0` for all trades\\._\n"
        "Or send /skip for no minimum\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_cancel_keyboard(),
    )
    return STATE_MIN_USD


async def conv_receive_min_usd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if text == "❌ Cancel":
        return await _cancel(update, context)

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

    # Step 4 — only_buys: yes/no inline buttons
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, BUY trades only", callback_data="onlybuys:yes"),
            InlineKeyboardButton("📊 All trades",            callback_data="onlybuys:no"),
        ]
    ])
    await update.message.reply_text(
        "✅ *Step 4 of 4 — Filter*\n\n"
        "Should I alert you only on *BUY* trades, or all trades \\(SELL included\\)?",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
    )
    return STATE_ONLY_BUYS


async def conv_receive_only_buys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    only_buys = query.data == "onlybuys:yes"
    context.user_data["only_buys"] = only_buys

    # Commit to DB
    user    = query.from_user
    wallet  = context.user_data["wallet"]
    nick    = context.user_data.get("nickname")
    min_usd = context.user_data.get("min_usd", 0.0)

    success = db.add_wallet(user.id, wallet, nick, min_usd, only_buys)

    nick_display  = nick or f"`{wallet[:6]}…{wallet[-4:]}`"
    filter_text   = "BUY trades only" if only_buys else "all trades"
    min_usd_text  = f"${min_usd:.0f}" if min_usd else "no minimum"

    if success:
        await query.edit_message_text(
            f"🎉 *Wallet added successfully\\!*\n\n"
            f"📍 *Name:* {_esc(nick_display)}\n"
            f"📏 *Min size:* {_esc(min_usd_text)}\n"
            f"🔍 *Filter:* {_esc(filter_text)}\n\n"
            "_I'll start sending alerts within 45 seconds\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await query.edit_message_text(
            "⚠️ Could not add wallet \\(it may already be tracked\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    # Restore main keyboard via a follow-up message
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Use the menu below to manage your wallets\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_main_menu_keyboard(),
    )
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
#  Keyboard menu text routing (reply keyboard buttons)
# ─────────────────────────────────────────────────────────────────────────────

async def handle_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route taps on the persistent reply keyboard to the right handler."""
    text = update.message.text

    if text == "➕ Add Wallet":
        # Kick off the conversation
        await conv_start_add(update, context)
    elif text == "📋 My Wallets":
        await cmd_my_wallets(update, context)
    elif text == "🗑️ Remove Wallet":
        await cmd_remove_wallet(update, context)
    elif text == "❓ Help":
        await cmd_help(update, context)
    else:
        await update.message.reply_text(
            "🤔 I didn't understand that\\. Use the menu below or /help\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_main_menu_keyboard(),
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Alert formatting (called from the background job in bot.py)
# ─────────────────────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """Escape special MarkdownV2 characters."""
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
    """
    Build the MarkdownV2 alert message sent to the user.

    Example output:
        🔔 New Polymarket Trade!
        Wallet: Whale #1 (0xAbCd…1234)
        💰 BUY  1,200 YES "Will X happen?" @ $0.65
        Value: ~$780 | 📅 2026-02-24 15:45 UTC
        🔗 View on Polymarket
    """
    emoji      = "💰" if trade_type == "BUY" else "📉"
    wallet_disp = nickname or f"{wallet_address[:6]}…{wallet_address[-4:]}"
    dt_str     = (
        datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if timestamp else "Unknown time"
    )
    title_part = f' "{_esc(market_title[:55] + ("…" if len(market_title) > 55 else ""))}"' if market_title else ""
    outcome_str = f" {_esc(outcome)}" if outcome else ""

    lines = [
        "🔔 *New Polymarket Trade\\!*\n",
        f"👤 *Wallet:* {_esc(wallet_disp)}",
        f"    `{_esc(wallet_address[:6])}…{_esc(wallet_address[-4:])}`\n",
        f"{emoji} *{_esc(trade_type)}*{outcome_str}{title_part}",
        f"    {_esc(f'{size:,.0f}' if size >= 1 else f'{size:.4f}')} shares @ `${price:.3f}`\n",
        f"💵 *Value:* ~`${usd_value:,.2f}`",
        f"📅 {_esc(dt_str)}\n",
        f"[🔗 View activity]({polymarket_url})",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  ConversationHandler factory (imported by bot.py)
# ─────────────────────────────────────────────────────────────────────────────

def build_add_wallet_conversation() -> ConversationHandler:
    """Build and return the multi-step add-wallet ConversationHandler."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("add_wallet", conv_start_add),
            # Also triggered by the "➕ Add Wallet" button handled via handle_menu_text
            # but we need a MessageHandler entry for that specific text too:
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
            MessageHandler(filters.Regex(r"^❌ Cancel$"), _cancel),
        ],
        allow_reentry=True,
        name="add_wallet_conv",
    )
