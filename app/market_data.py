"""
market_data.py — fresh, original market-data layer for Scout.

Pulls live data from Binance's public REST API (no key required for these
endpoints). Deliberately simple: this is NOT a port of any proprietary
SMC/liquidity-sweep logic — it's a small, new set of signals written for
this build, sized for an AI model (via zg_compute) to reason over rather
than for a deterministic rules engine to gate trades on.

NOTE: written without the ability to test live network calls in this
environment — verify each endpoint once in Codespaces before relying on
it for the demo. Endpoints used are Binance's long-stable public ones,
but double-check response shapes against current docs if anything looks
off (api.binance.com/api/v3, fapi.binance.com/fapi/v1).
"""

import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger("scout")

SPOT_BASE = "https://api.binance.com/api/v3"
FAPI_BASE = "https://fapi.binance.com/fapi/v1"

DEFAULT_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT",
]


def get_24h_ticker(symbol: str) -> dict:
    """24h price/volume stats for one symbol."""
    resp = requests.get(f"{SPOT_BASE}/ticker/24hr", params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    d = resp.json()
    return {
        "symbol":          d["symbol"],
        "price":           float(d["lastPrice"]),
        "change_pct_24h":  float(d["priceChangePercent"]),
        "high_24h":        float(d["highPrice"]),
        "low_24h":         float(d["lowPrice"]),
        "volume_24h":      float(d["volume"]),
        "quote_volume_24h": float(d["quoteVolume"]),
    }


def get_klines(symbol: str, interval: str = "15m", limit: int = 50) -> list:
    """
    Recent candles. Returns list of dicts: open_time, open, high, low, close, volume.
    interval examples: "5m", "15m", "1h", "4h", "1d"
    """
    resp = requests.get(
        f"{SPOT_BASE}/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    rows = resp.json()
    return [
        {
            "open_time": datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc).isoformat(),
            "open":  float(r[1]),
            "high":  float(r[2]),
            "low":   float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        }
        for r in rows
    ]


def get_funding_rate(symbol: str) -> dict:
    """Current perpetual funding rate + mark price (futures market)."""
    resp = requests.get(f"{FAPI_BASE}/premiumIndex", params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    d = resp.json()
    return {
        "symbol":       d["symbol"],
        "mark_price":   float(d["markPrice"]),
        "funding_rate": float(d["lastFundingRate"]),
    }


def _range_position(candles: list) -> float:
    """Where is price sitting within its recent high/low range? 0 = at low, 1 = at high."""
    if not candles:
        return 0.5
    highs = [c["high"] for c in candles]
    lows  = [c["low"] for c in candles]
    hi, lo = max(highs), min(lows)
    if hi == lo:
        return 0.5
    return round((candles[-1]["close"] - lo) / (hi - lo), 3)


def _volume_spike(candles: list) -> float:
    """Latest candle's volume vs the average of the prior ones. >1 = above average."""
    if len(candles) < 5:
        return 1.0
    avg_prior = sum(c["volume"] for c in candles[:-1]) / max(len(candles) - 1, 1)
    if avg_prior == 0:
        return 1.0
    return round(candles[-1]["volume"] / avg_prior, 2)


def snapshot(symbol: str) -> dict:
    """
    One symbol's full picture: 24h stats + 15m structure signals + funding.
    This is the unit of context handed to the reasoning layer — small and
    legible on purpose, since the model (not a rules engine) does the
    actual interpretation.
    """
    out = {"symbol": symbol, "ts": datetime.now(timezone.utc).isoformat()}
    try:
        out["ticker"] = get_24h_ticker(symbol)
    except Exception as e:
        log.warning(f"{symbol}: ticker fetch failed — {e}")
        out["ticker"] = None
    try:
        candles = get_klines(symbol, "15m", 30)
        out["range_position_15m"] = _range_position(candles)
        out["volume_spike_15m"]   = _volume_spike(candles)
        out["last_close"]         = candles[-1]["close"] if candles else None
    except Exception as e:
        log.warning(f"{symbol}: klines fetch failed — {e}")
    try:
        out["funding"] = get_funding_rate(symbol)
    except Exception as e:
        log.debug(f"{symbol}: funding fetch failed (spot-only symbol?) — {e}")
        out["funding"] = None
    return out


def signal_strength(snap: dict) -> int:
    """
    Deterministic 0-100 score from the snapshot's actual fields. NOT an
    LLM-generated number — this is plain arithmetic so it's inspectable
    and explainable, not a hallucinated confidence figure. The reasoning
    layer asks 0G Compute to explain *this* score, never to invent its
    own — a judge (or a trader) can always trace where the number came
    from.

    Weights are intentionally simple for a first build:
      - volume spike vs recent average (more weight = more conviction)
      - distance from the middle of the recent range (extremes = more interesting)
      - magnitude of 24h move (bigger moves = more to react to)
    """
    score = 0
    vs = snap.get("volume_spike_15m")
    if vs:
        score += min(40, round((vs - 1.0) * 25)) if vs > 1 else 0

    rp = snap.get("range_position_15m")
    if rp is not None:
        score += round(abs(rp - 0.5) * 2 * 35)  # 0 at midrange, 35 at either extreme

    ticker = snap.get("ticker") or {}
    chg = abs(ticker.get("change_pct_24h", 0))
    score += min(25, round(chg * 2.5))

    return max(0, min(100, score))


def watchlist_snapshot(symbols: list = None) -> list:
    """Snapshot every symbol in the watchlist — used for 'best opportunities now'."""
    symbols = symbols or DEFAULT_WATCHLIST
    out = []
    for s in symbols:
        try:
            snap = snapshot(s)
            snap["signal_strength"] = signal_strength(snap)
            out.append(snap)
        except Exception as e:
            log.warning(f"{s}: snapshot failed — {e}")
    return out
