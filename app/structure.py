"""
structure.py — SMC structure detection for Scout.

Fresh, original implementation of:
  - Swing high/low detection
  - Liquidity sweep detection (price wicks into a swing level then rejects)
  - Break of Structure (BOS) detection
  - Inducement zone mapping
  - HTF bias determination

Inspired by Smart Money Concepts but written from scratch for Scout.
Uses candle data from market_data.py (Binance) or mexc_data.py (MEXC).
"""

import logging

log = logging.getLogger("scout")


# ── Swing detection ────────────────────────────────────────────────────────

def find_swing_highs(candles: list, strength: int = 3) -> list:
    """
    Find swing highs: candles where the high is higher than N candles
    on each side. Returns list of {index, price, open_time}.
    strength=3 means 3 candles on each side must be lower.
    """
    highs = []
    n = len(candles)
    for i in range(strength, n - strength):
        c_high = candles[i]["high"]
        left   = all(candles[i - j]["high"] < c_high for j in range(1, strength + 1))
        right  = all(candles[i + j]["high"] < c_high for j in range(1, strength + 1))
        if left and right:
            highs.append({
                "index":     i,
                "price":     c_high,
                "open_time": candles[i].get("open_time", ""),
                "type":      "swing_high",
            })
    return highs


def find_swing_lows(candles: list, strength: int = 3) -> list:
    """
    Find swing lows: candles where the low is lower than N candles on each side.
    """
    lows = []
    n = len(candles)
    for i in range(strength, n - strength):
        c_low = candles[i]["low"]
        left  = all(candles[i - j]["low"] > c_low for j in range(1, strength + 1))
        right = all(candles[i + j]["low"] > c_low for j in range(1, strength + 1))
        if left and right:
            lows.append({
                "index":     i,
                "price":     c_low,
                "open_time": candles[i].get("open_time", ""),
                "type":      "swing_low",
            })
    return lows


# ── HTF Bias ───────────────────────────────────────────────────────────────

def get_htf_bias(candles_daily: list, candles_h4: list) -> dict:
    """
    Determine the higher timeframe bias.

    Daily bias: is price closing above or below the midpoint of the last
    20 daily candles? Simple but effective.

    H4 bias: what was the last break of structure direction?

    Returns: {
        daily_bias: "bullish" | "bearish" | "neutral",
        h4_bias:    "bullish" | "bearish" | "neutral",
        bias:       "bullish" | "bearish" | "neutral",  # combined
        note:       str
    }
    """
    # ── Daily bias ───────────────────────────────────────────────────────
    daily_bias = "neutral"
    daily_note = ""
    if len(candles_daily) >= 10:
        last_20  = candles_daily[-20:]
        highs    = [c["high"]  for c in last_20]
        lows     = [c["low"]   for c in last_20]
        midpoint = (max(highs) + min(lows)) / 2
        current  = candles_daily[-1]["close"]
        if current > midpoint * 1.01:
            daily_bias = "bullish"
            daily_note = f"Price {current:.4f} above range midpoint {midpoint:.4f}"
        elif current < midpoint * 0.99:
            daily_bias = "bearish"
            daily_note = f"Price {current:.4f} below range midpoint {midpoint:.4f}"
        else:
            daily_note = f"Price {current:.4f} at range midpoint {midpoint:.4f}"

    # ── H4 BOS direction ─────────────────────────────────────────────────
    h4_bias = "neutral"
    h4_note = ""
    if len(candles_h4) >= 10:
        bos = detect_bos(candles_h4, lookback=20)
        if bos.get("broken"):
            h4_bias = bos["bias"]
            h4_note = f"Last H4 BOS: {h4_bias} at {bos.get('level', 0):.4f}"
        else:
            h4_note = "No clear H4 BOS recently"

    # ── Combined bias ────────────────────────────────────────────────────
    if daily_bias == h4_bias and daily_bias != "neutral":
        combined = daily_bias
    elif daily_bias != "neutral":
        combined = daily_bias   # daily takes precedence
    elif h4_bias != "neutral":
        combined = h4_bias
    else:
        combined = "neutral"

    return {
        "daily_bias": daily_bias,
        "h4_bias":    h4_bias,
        "bias":       combined,
        "daily_note": daily_note,
        "h4_note":    h4_note,
    }


# ── Sweep Detection ────────────────────────────────────────────────────────

def detect_sweeps(candles: list, bias: str = "both",
                  swing_strength: int = 3) -> list:
    """
    Detect liquidity sweeps in the candle data.

    A sweep is when price wicks INTO a prior swing high/low (breaking it
    intracandle) but CLOSES back on the other side — classic stop-hunt.

    bias: "bullish" (look for sweeps of lows), "bearish" (sweeps of highs),
          or "both"

    Returns list of sweep events, most recent first.
    """
    if len(candles) < swing_strength * 2 + 5:
        return []

    sw_highs = find_swing_highs(candles, strength=swing_strength)
    sw_lows  = find_swing_lows(candles,  strength=swing_strength)
    sweeps   = []

    # Check each candle AFTER each swing for a sweep
    for candle_idx in range(len(candles)):
        c = candles[candle_idx]

        # ── Bullish sweep (sweep of lows — hunts buy-side stops) ─────────
        if bias in ("bullish", "both"):
            for sl in sw_lows:
                if candle_idx <= sl["index"]:
                    continue
                level = sl["price"]
                # Wick below the swing low but close above it
                if c["low"] < level and c["close"] > level:
                    depth_pct = (level - c["low"]) / level * 100
                    age       = candle_idx - sl["index"]
                    sweeps.append({
                        "type":       "bullish_sweep",
                        "direction":  "bullish",
                        "level":      level,
                        "sweep_low":  c["low"],
                        "depth_pct":  round(depth_pct, 3),
                        "candle_idx": candle_idx,
                        "swing_idx":  sl["index"],
                        "age_candles": age,
                        "open_time":  c.get("open_time", ""),
                        "close":      c["close"],
                        "description": f"Sweep of swing low {level:.4f} — wick to {c['low']:.4f}, closed {c['close']:.4f}",
                    })

        # ── Bearish sweep (sweep of highs — hunts sell-side stops) ───────
        if bias in ("bearish", "both"):
            for sh in sw_highs:
                if candle_idx <= sh["index"]:
                    continue
                level = sh["price"]
                # Wick above the swing high but close below it
                if c["high"] > level and c["close"] < level:
                    depth_pct = (c["high"] - level) / level * 100
                    age       = candle_idx - sh["index"]
                    sweeps.append({
                        "type":       "bearish_sweep",
                        "direction":  "bearish",
                        "level":      level,
                        "sweep_high": c["high"],
                        "depth_pct":  round(depth_pct, 3),
                        "candle_idx": candle_idx,
                        "swing_idx":  sh["index"],
                        "age_candles": age,
                        "open_time":  c.get("open_time", ""),
                        "close":      c["close"],
                        "description": f"Sweep of swing high {level:.4f} — wick to {c['high']:.4f}, closed {c['close']:.4f}",
                    })

    # Sort by candle_idx descending (most recent first), dedupe by level
    sweeps.sort(key=lambda x: x["candle_idx"], reverse=True)
    seen_levels = set()
    unique = []
    for s in sweeps:
        lvl = round(s["level"], 6)
        if lvl not in seen_levels:
            seen_levels.add(lvl)
            unique.append(s)
    return unique


def detect_bos(candles: list, lookback: int = 30) -> dict:
    """
    Detect the most recent Break of Structure.

    BOS: price closes ABOVE a prior swing high (bullish BOS)
         or CLOSES BELOW a prior swing low (bearish BOS).

    Returns {broken: bool, bias: str, level: float, candle_idx: int,
             open_time: str, description: str}
    """
    if len(candles) < 10:
        return {"broken": False}

    window   = candles[-lookback:] if len(candles) > lookback else candles
    offset   = len(candles) - len(window)
    sw_highs = find_swing_highs(window, strength=2)
    sw_lows  = find_swing_lows(window,  strength=2)

    last_bos = {"broken": False, "candle_idx": -1}

    for i in range(len(window)):
        c = window[i]

        # Bullish BOS: close above a prior swing high
        for sh in sw_highs:
            if sh["index"] >= i:
                continue
            if c["close"] > sh["price"]:
                abs_idx = offset + i
                if abs_idx > last_bos.get("candle_idx", -1):
                    last_bos = {
                        "broken":      True,
                        "bias":        "bullish",
                        "level":       sh["price"],
                        "candle_idx":  abs_idx,
                        "open_time":   c.get("open_time", ""),
                        "description": f"Bullish BOS — closed {c['close']:.4f} above swing high {sh['price']:.4f}",
                    }

        # Bearish BOS: close below a prior swing low
        for sl in sw_lows:
            if sl["index"] >= i:
                continue
            if c["close"] < sl["price"]:
                abs_idx = offset + i
                if abs_idx > last_bos.get("candle_idx", -1):
                    last_bos = {
                        "broken":      True,
                        "bias":        "bearish",
                        "level":       sl["price"],
                        "candle_idx":  abs_idx,
                        "open_time":   c.get("open_time", ""),
                        "description": f"Bearish BOS — closed {c['close']:.4f} below swing low {sl['price']:.4f}",
                    }

    return last_bos


# ── Inducement Zones ───────────────────────────────────────────────────────

def find_inducement_zones(candles: list, bias: str,
                          swing_strength: int = 2) -> list:
    """
    Inducement zones: the swing levels on the path TOWARD a larger target.

    In SMC, after a BOS the market typically retraces to grab liquidity
    sitting at equal highs/lows (inducement) before continuing.

    For bullish bias: inducement zones are clusters of swing HIGHS between
    current price and the last BOS high — these are the short-stop pools
    price will sweep before the real continuation.

    For bearish bias: inducement zones are clusters of swing LOWS.

    Returns list of {price, type, note} sorted by distance from current price.
    """
    if len(candles) < 10:
        return []

    current_price = candles[-1]["close"]
    zones = []

    if bias == "bullish":
        sw_highs = find_swing_highs(candles, strength=swing_strength)
        # Only take swing highs ABOVE current price (buy-side liquidity)
        above = [sh for sh in sw_highs if sh["price"] > current_price]
        # Find equal highs (within 0.3%) — these are especially strong inducements
        for i, sh in enumerate(above):
            equal = [s for s in above if s != sh
                     and abs(s["price"] - sh["price"]) / sh["price"] < 0.003]
            note = "Equal highs — strong inducement" if equal else "Swing high liquidity"
            zones.append({
                "price":    sh["price"],
                "type":     "buy_side_liquidity",
                "note":     note,
                "distance_pct": round((sh["price"] - current_price) / current_price * 100, 2),
            })

    elif bias == "bearish":
        sw_lows = find_swing_lows(candles, strength=swing_strength)
        # Only take swing lows BELOW current price (sell-side liquidity)
        below = [sl for sl in sw_lows if sl["price"] < current_price]
        for sl in below:
            equal = [s for s in below if s != sl
                     and abs(s["price"] - sl["price"]) / sl["price"] < 0.003]
            note = "Equal lows — strong inducement" if equal else "Swing low liquidity"
            zones.append({
                "price":    sl["price"],
                "type":     "sell_side_liquidity",
                "note":     note,
                "distance_pct": round((current_price - sl["price"]) / current_price * 100, 2),
            })

    # Sort by closest first
    zones.sort(key=lambda x: x["distance_pct"])
    return zones[:5]  # top 5 closest


# ── Full structure snapshot ────────────────────────────────────────────────

def full_structure_snapshot(symbol: str, candles_h1: list,
                            candles_h4: list, candles_daily: list) -> dict:
    """
    One call that returns the full SMC picture for a symbol:
    HTF bias, recent sweeps, last BOS, and inducement zones.
    Used by the scanner and by the reasoning layer.
    """
    bias_data   = get_htf_bias(candles_daily, candles_h4)
    bias        = bias_data["bias"]

    # Sweeps on H1 (most actionable)
    h1_sweeps   = detect_sweeps(candles_h1, bias=bias, swing_strength=3)
    recent_sweep = h1_sweeps[0] if h1_sweeps else None

    # BOS on H1
    h1_bos = detect_bos(candles_h1, lookback=48)

    # Inducement zones on H1
    inducement = find_inducement_zones(candles_h1, bias=bias) if bias != "neutral" else []

    return {
        "symbol":         symbol,
        "bias":           bias,
        "bias_data":      bias_data,
        "recent_sweep":   recent_sweep,
        "all_sweeps_h1":  h1_sweeps[:3],
        "bos_h1":         h1_bos,
        "inducement_zones": inducement,
        "current_price":  candles_h1[-1]["close"] if candles_h1 else 0,
    }
