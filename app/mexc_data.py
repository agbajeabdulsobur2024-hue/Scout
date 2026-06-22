"""
mexc_data.py — MEXC Futures public API client.

Pulls live ticker data and candles from MEXC's public endpoints.
No API key required for these. Used for crime detection — spotting
coordinated pumps/dumps like ASTEROID +85%, HEI +56% before retail notices.

FIX LOG:
  - Fixed duplicate function body for get_all_tickers() (was inside get_funding_rate)
  - Added multi-field fallback for priceChangePercent (MEXC returns different
    field names depending on the endpoint version)
  - Added open24h price change calculation as final fallback
  - Filter: USDT perps only (no stocks, forex, commodities)
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


def _parse_change_pct(t: dict) -> float | None:
    """
    Parse 24h price change % from a MEXC ticker.

    MEXC uses different field names across API versions:
      - priceChangePercent  (most common, value is already %)
      - changeRate          (decimal, e.g. 0.05 = 5%)
      - riseFallRate        (decimal on some pairs)

    If none present, compute from lastPrice + open24h.
    Returns percentage float (5.0 = 5%) or None if unavailable.
    """
    # Try direct percentage fields first
    for field in ("priceChangePercent",):
        val = t.get(field)
        if val is not None:
            try:
                fval = float(val)
                # Sanity check: if it looks like a decimal (0.05), convert
                # We use ±2.0 as the threshold — anything above is definitely %
                if abs(fval) < 2.0 and fval != 0:
                    fval = fval * 100
                return fval
            except (ValueError, TypeError):
                pass

    # Try decimal rate fields
    for field in ("changeRate", "riseFallRate"):
        val = t.get(field)
        if val is not None:
            try:
                return float(val) * 100  # always convert these to %
            except (ValueError, TypeError):
                pass

    # Compute from price fields
    try:
        last  = float(t.get("lastPrice", 0))
        open_ = float(t.get("open24h", t.get("openPrice", t.get("open", 0))))
        if last > 0 and open_ > 0:
            return round((last - open_) / open_ * 100, 4)
    except (ValueError, TypeError):
        pass

    return None


def get_funding_rate(symbol: str) -> dict:
    """
    Fetch current funding rate from MEXC contract API.
    Symbol format: BTC_USDT (MEXC uses underscores)
    Endpoint: GET /api/v1/contract/funding_rate/{symbol}

    Returns {funding_rate, next_settle_time (ms), ok}
    """
    # Normalise: BTCUSDT → BTC_USDT
    if "_" not in symbol:
        sym = symbol.replace("USDT", "_USDT")
    else:
        sym = symbol
    try:
        r = requests.get(f"{BASE}/funding_rate/{sym}", timeout=8)
        r.raise_for_status()
        d = r.json()
        if not d.get("success"):
            return {"funding_rate": 0.0, "next_settle_time": 0, "ok": False}
        data = d.get("data", {})
        return {
            "funding_rate":     float(data.get("fundingRate", 0)),
            "next_settle_time": int(data.get("nextSettleTime", 0)),
            "ok":               True,
        }
    except Exception as e:
        log.debug(f"MEXC get_funding_rate {sym}: {e}")
        return {"funding_rate": 0.0, "next_settle_time": 0, "ok": False}


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
    """Relative volume: current 24h volume vs median of recent daily volumes."""
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
    """How fast is price moving in the last N candles vs the prior period?"""
    if len(candles) < lookback * 2:
        return 1.0
    recent = candles[-lookback:]
    prior  = candles[-(lookback * 2):-lookback]
    recent_move = abs(recent[-1]["close"] - recent[0]["open"]) / max(recent[0]["open"], 1e-10)
    prior_move  = abs(prior[-1]["close"]  - prior[0]["open"])  / max(prior[0]["open"],  1e-10)
    if prior_move == 0:
        return 1.0
    return round(recent_move / prior_move, 2)


def get_all_tickers() -> list:
    """
    Pull the full MEXC futures ticker feed — all USDT perpetuals.
    Returns a list of dicts with symbol, price, change_pct, volume etc.
    Only returns USDT-paired crypto perpetuals (filters out stocks, forex, commodities).
    """
    try:
        resp = requests.get(f"{BASE}/ticker", timeout=15)
        resp.raise_for_status()
        raw = resp.json().get("data", [])

        if raw:
            log.debug(f"MEXC ticker sample keys: {list(raw[0].keys())}")

        out = []
        for t in raw:
            sym = t.get("symbol", "")

            # Only USDT crypto perpetuals
            if not sym.endswith("_USDT"):
                continue

            # Skip obvious non-crypto assets
            name_lower = sym.lower()
            if any(s in name_lower for s in ("stock", "xau", "xag", "usd_", "eur", "gbp",
                                               "jpy", "aud", "chf", "cad", "nzd", "oil",
                                               "gas", "corn", "wheat", "sp500", "nasdaq")):
                continue

            change_pct = _parse_change_pct(t)
            if change_pct is None:
                continue

            try:
                out.append({
                    "symbol":     sym,
                    "price":      float(t.get("lastPrice", 0)),
                    "change_pct": change_pct,
                    "volume_24h": float(t.get("volume24", t.get("vol24", 0))),
                    "high_24h":   float(t.get("high24Price", t.get("high24", 0))),
                    "low_24h":    float(t.get("low24Price",  t.get("low24",  0))),
                    "bid":        float(t.get("bid1", 0)),
                    "ask":        float(t.get("ask1", 0)),
                })
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:
        log.warning(f"MEXC get_all_tickers failed: {e}")
        return []


def get_top_movers(top_n: int = 20) -> dict:
    """Top gainers and losers across all MEXC USDT perpetuals."""
    tickers = get_all_tickers()
    if not tickers:
        return {"gainers": [], "losers": []}

    # Filter: must have a real move
    live = [t for t in tickers if abs(t["change_pct"]) > 0.01]
    if not live:
        live = tickers  # fallback if all are near 0

    sorted_t = sorted(live, key=lambda x: x["change_pct"])
    return {
        "losers":  sorted_t[:top_n],
        "gainers": sorted_t[-top_n:][::-1],
    }


def scan_for_crimes(top_n: int = 20) -> list:
    """
    Core crime detection scan. Runs across all MEXC USDT perps and returns
    the most suspicious movers — ranked by a composite crime score.
    """
    tickers = get_all_tickers()
    if not tickers:
        return []

    results = []
    for t in tickers:
        chg  = t["change_pct"]
        rng  = _range_pct(t["high_24h"], t["low_24h"], t["price"])

        if abs(chg) < CRIME_MOVE_THRESHOLD and rng < CRIME_RANGE_THRESHOLD:
            continue

        candles = get_candles(t["symbol"], "Hour1", 24)
        rvol    = _rvol(t["volume_24h"], candles)
        velocity = _velocity(candles, lookback=3)

        if abs(chg) < 10.0 and rvol < 2.5:
            continue

        crime_score = (
            abs(chg)   * 2.0 +
            rvol       * 5.0 +
            velocity   * 3.0 +
            rng        * 1.0
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
