"""
mexc_data.py — MEXC Futures public API client.

Pulls live ticker data and candles from MEXC's public endpoints.
No API key required for these. Used for crime detection — spotting
coordinated pumps/dumps like ASTEROID +85%, HEI +56% before retail notices.
"""

import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger("scout")

BASE = "https://contract.mexc.com/api/v1/contract"

# Minimum move % to even consider flagging
CRIME_MOVE_THRESHOLD  = 5.0    # 5%+ move in 24h
CRIME_RVOL_THRESHOLD  = 2.0    # 2x normal volume
CRIME_RANGE_THRESHOLD = 8.0    # 8%+ daily range


def get_all_tickers() -> list:
    """
    Pull the full MEXC futures ticker feed — all USDT perpetuals.
    Returns a list of dicts with symbol, price, change_pct, volume etc.
    """
    try:
        resp = requests.get(f"{BASE}/ticker", timeout=15)
        resp.raise_for_status()
        raw = resp.json().get("data", [])
        out = []
        for t in raw:
            sym = t.get("symbol", "")
            if not sym.endswith("_USDT"):
                continue
            try:
                out.append({
                    "symbol":      sym,
                    "price":       float(t.get("lastPrice", 0)),
                    "change_pct":  float(t.get("priceChangePercent", 0)),
                    "volume_24h":  float(t.get("volume24", 0)),
                    "high_24h":    float(t.get("high24Price", 0)),
                    "low_24h":     float(t.get("low24Price", 0)),
                    "bid":         float(t.get("bid1", 0)),
                    "ask":         float(t.get("ask1", 0)),
                })
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:
        log.warning(f"MEXC ticker fetch failed: {e}")
        return []


def get_candles(symbol: str, interval: str = "Hour1", limit: int = 50) -> list:
    """
    MEXC futures candles.
    interval options: Min1, Min5, Min15, Min30, Hour1, Hour4, Day1
    Returns list of {open_time, open, high, low, close, volume}
    """
    try:
        resp = requests.get(
            f"{BASE}/kline/{symbol}",
            params={"interval": interval, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        times  = data.get("time", [])
        opens  = data.get("open", [])
        highs  = data.get("high", [])
        lows   = data.get("low", [])
        closes = data.get("close", [])
        vols   = data.get("vol", [])
        out = []
        for i in range(len(times)):
            try:
                out.append({
                    "open_time": datetime.fromtimestamp(times[i], tz=timezone.utc).isoformat(),
                    "open":  float(opens[i]),
                    "high":  float(highs[i]),
                    "low":   float(lows[i]),
                    "close": float(closes[i]),
                    "volume": float(vols[i]),
                })
            except (IndexError, ValueError, TypeError):
                continue
        return out
    except Exception as e:
        log.warning(f"MEXC candles {symbol}/{interval} failed: {e}")
        return []


def _rvol(tickers_vol: float, candles: list) -> float:
    """
    Relative volume: current 24h volume vs median of recent daily volumes.
    Simple proxy — uses the candle volumes as a baseline.
    """
    if not candles or len(candles) < 5:
        return 1.0
    vols = [c["volume"] for c in candles if c["volume"] > 0]
    if not vols:
        return 1.0
    median_vol = sorted(vols)[len(vols) // 2]
    if median_vol == 0:
        return 1.0
    return round(tickers_vol / median_vol, 2)


def _range_pct(high: float, low: float, price: float) -> float:
    if price == 0:
        return 0.0
    return round((high - low) / price * 100, 2)


def _velocity(candles: list, lookback: int = 3) -> float:
    """
    How fast is price moving in the last N candles vs the prior period?
    Returns a ratio: >1 means accelerating.
    """
    if len(candles) < lookback * 2:
        return 1.0
    recent = candles[-lookback:]
    prior  = candles[-(lookback * 2):-lookback]
    recent_move = abs(recent[-1]["close"] - recent[0]["open"]) / max(recent[0]["open"], 1e-10)
    prior_move  = abs(prior[-1]["close"]  - prior[0]["open"])  / max(prior[0]["open"],  1e-10)
    if prior_move == 0:
        return 1.0
    return round(recent_move / prior_move, 2)


def scan_for_crimes(top_n: int = 20) -> list:
    """
    Core crime detection scan. Runs across all MEXC USDT perps and returns
    the most suspicious movers — ranked by a composite crime score.

    What makes something suspicious:
      - Large % move (pump or dump)
      - Volume way above normal (coordinated)
      - Price moving faster recently than it was before (acceleration)
      - Wide daily range relative to price (whipsaw manipulation)

    Returns list of dicts sorted by crime_score desc, capped at top_n.
    """
    tickers = get_all_tickers()
    if not tickers:
        return []

    results = []
    for t in tickers:
        chg  = t["change_pct"]
        rng  = _range_pct(t["high_24h"], t["low_24h"], t["price"])

        # First gate: must have meaningful move or range
        if abs(chg) < CRIME_MOVE_THRESHOLD and rng < CRIME_RANGE_THRESHOLD:
            continue

        # Fetch 1H candles to get velocity and RVOL baseline
        candles = get_candles(t["symbol"], "Hour1", 24)
        rvol     = _rvol(t["volume_24h"], candles)
        velocity = _velocity(candles, lookback=3)

        # At small move sizes, require volume confirmation to avoid spam.
        # A 5% move with normal volume is noise. A 5% move with 3x volume is signal.
        if abs(chg) < 10.0 and rvol < 2.5:
            continue

        # Crime score: weighted composite
        crime_score = (
            abs(chg)   * 2.0 +   # size of move is most important
            rvol       * 5.0 +   # unusual volume is very suspicious
            velocity   * 3.0 +   # acceleration pattern
            rng        * 1.0     # wide range = manipulation
        )

        direction = "PUMP" if chg > 0 else "DUMP"
        severity  = (
            "🚨 EXTREME" if abs(chg) > 50 else
            "🔴 HIGH"    if abs(chg) > 20 else
            "🟠 MEDIUM"  if abs(chg) > 10 else
            "🟡 WATCH"   if abs(chg) >  5 else
            "👀 MINOR"
        )

        results.append({
            **t,
            "range_pct":   rng,
            "rvol":        rvol,
            "velocity":    velocity,
            "crime_score": round(crime_score, 1),
            "direction":   direction,
            "severity":    severity,
            "candles_1h":  candles,
        })

    results.sort(key=lambda x: x["crime_score"], reverse=True)
    return results[:top_n]


def get_top_movers(top_n: int = 10) -> dict:
    """
    Simple top gainers/losers — used for the /movers command.
    """
    tickers = get_all_tickers()
    if not tickers:
        return {"gainers": [], "losers": []}
    sorted_t = sorted(tickers, key=lambda x: x["change_pct"])
    return {
        "losers":  sorted_t[:top_n],
        "gainers": sorted_t[-top_n:][::-1],
    }
