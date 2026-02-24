"""
api.py — Polymarket API client for PolyTrack Bot
=================================================
All HTTP calls to Polymarket's public APIs go through this module.
Uses aiohttp for non-blocking I/O so the bot event loop never stalls.

Public endpoints used:
  • https://data-api.polymarket.com/trades   – trade history per wallet
  • https://gamma-api.polymarket.com/markets – market metadata (title, slug)
"""

import logging
import asyncio
from typing import Optional, Any
import aiohttp

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────
TRADES_BASE_URL  = "https://data-api.polymarket.com/trades"
MARKETS_BASE_URL = "https://gamma-api.polymarket.com/markets"

# Shared client session; created once and reused across all poll cycles
_session: Optional[aiohttp.ClientSession] = None

# Simple in-memory cache for market titles: market_id → title string
_market_cache: dict[str, str] = {}

# Timeout for every API call
_TIMEOUT = aiohttp.ClientTimeout(total=15)


# ─────────────────────────────────────────────────────────────────────────────
#  Session lifecycle
# ─────────────────────────────────────────────────────────────────────────────

async def get_session() -> aiohttp.ClientSession:
    """
    Return (or lazily create) the shared aiohttp session.
    Call close_session() when the bot shuts down.
    """
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=_TIMEOUT,
            headers={"User-Agent": "PolyTrackBot/1.0 (+github.com/polytrack)"},
        )
    return _session


async def close_session() -> None:
    """Gracefully close the shared HTTP session on bot shutdown."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        logger.info("HTTP session closed.")


# ─────────────────────────────────────────────────────────────────────────────
#  Trade fetching
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_trades(wallet: str, limit: int = 15) -> list[dict[str, Any]]:
    """
    Fetch recent trades for *wallet* from Polymarket Data API.

    Returns a list of trade dicts (may be empty on error).
    Always sorted newest-first by the API.

    Each trade dict contains at minimum:
        id, type (BUY/SELL), size, price, timestamp, market (condition_id/slug)
    """
    params = {
        "user":          wallet.lower(),
        "limit":         str(limit),
        "sortBy":        "TIMESTAMP",
        "sortDirection": "DESC",
    }
    session = await get_session()
    try:
        async with session.get(TRADES_BASE_URL, params=params) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                # API may return a list directly or a dict with a data key
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("data", data.get("trades", []))
                return []
            else:
                logger.warning(
                    "Polymarket trades API returned %s for wallet %s",
                    resp.status, wallet[:10],
                )
                return []
    except asyncio.TimeoutError:
        logger.warning("Timeout fetching trades for %s", wallet[:10])
        return []
    except aiohttp.ClientError as exc:
        logger.error("HTTP error fetching trades for %s: %s", wallet[:10], exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Market title fetching (with in-memory cache)
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_market_title(market_id: str) -> Optional[str]:
    """
    Try to resolve a human-readable title for a market.
    Returns None if not found or on error (caller shows market_id instead).
    Results are cached to avoid hammering the API.
    """
    if not market_id:
        return None

    # Return cached value immediately
    if market_id in _market_cache:
        return _market_cache[market_id]

    session = await get_session()
    # Gamma API accepts either condition_id or a numeric id
    url = f"{MARKETS_BASE_URL}/{market_id}"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                # Response may be a single dict or a list
                if isinstance(data, list) and data:
                    data = data[0]
                title = (
                    data.get("question")
                    or data.get("title")
                    or data.get("name")
                )
                if title:
                    _market_cache[market_id] = str(title)
                    return str(title)
            # Non-200 → silently skip, not critical
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not fetch market title for %s: %s", market_id, exc)

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Trade parsing helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_trade_type(trade: dict) -> str:
    """Normalise trade type to 'BUY' or 'SELL'."""
    raw = str(trade.get("side") or trade.get("type") or trade.get("tradeType") or "").upper()
    if "BUY" in raw:
        return "BUY"
    if "SELL" in raw:
        return "SELL"
    return raw or "?"


def parse_trade_size(trade: dict) -> float:
    """Return shares traded as a float (0.0 on parse error)."""
    raw = trade.get("size") or trade.get("amount") or 0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def parse_trade_price(trade: dict) -> float:
    """Return price per share in USD [0,1] as a float."""
    raw = trade.get("price") or trade.get("outcomePrice") or 0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def parse_trade_usd_value(trade: dict) -> float:
    """Compute approximate USD value of the trade."""
    # Some responses include a pre-computed value field
    if "usdcSize" in trade:
        try:
            return float(trade["usdcSize"])
        except (TypeError, ValueError):
            pass
    return parse_trade_size(trade) * parse_trade_price(trade)


def parse_trade_outcome(trade: dict) -> str:
    """Return YES, NO, or empty string."""
    return str(trade.get("outcome") or trade.get("side") or "").upper()


def parse_trade_timestamp(trade: dict) -> int:
    """Return Unix epoch (seconds) for the trade, or 0 on failure."""
    raw = trade.get("timestamp") or trade.get("createdAt") or trade.get("time") or 0
    try:
        ts = int(raw)
        # If microseconds / milliseconds, convert to seconds
        if ts > 1_000_000_000_000:
            ts = ts // 1000
        return ts
    except (TypeError, ValueError):
        return 0


def parse_market_id(trade: dict) -> str:
    """Return the market identifier string (condition_id, slug, etc.)."""
    return str(
        trade.get("conditionId")
        or trade.get("market")
        or trade.get("marketSlug")
        or trade.get("marketId")
        or ""
    )
