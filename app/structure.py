"""
structure.py — SMC structure detection for Scout.

Parameters calibrated from BEEM's battle-tested strategy_constants.py:
  - Swing strength: 2 (BEEM uses 2 on 5M/15M, same for H1)
  - Equal level tolerance: 0.003 (0.3%) — BEEM's find_equal_levels default
  - Sweep depth: 0.08 ATR minimum (SWEEP_DEPTH_STANDARD from BEEM)
  - Sweep age: 72 candles (SWEEP_CONTEXT_AGE — 6h on H1 = 3 days)
  - BOS minimum break: 0.15% beyond swing (from BEEM's MIN_BOS_BODY_RATIO logic)
  - Inducement lookback: 15 candles (INDUCEMENT_LOOKBACK from BEEM)
  - Max sweep zones: 4 (MAX_SWEEP_ZONE_DISPLAY — more = chop signal)
"""

import logging

log = logging.getLogger("scout")

# ── Parameters (from BEEM strategy_constants.py) ───────────────────────────
SWING_STRENGTH        = 2       # candles each side for swing detection
EQUAL_LEVEL_TOLERANCE = 0.003   # 0.3% — two levels are "equal" within this
SWEEP_DEPTH_STANDARD  = 0.08    # min penetration depth as fraction of ATR
SWEEP_CONTEXT_AGE     = 72      # max candles back for sweep (72 H1 = 3 days)
SWEEP_ENTRY_AGE       = 60      # candles — older than this = context only
MAX_SWEEP_ZONES       = 4       # more than this = choppy, no clean structure
INDUCEMENT_LOOKBACK   = 15      # candles to look back for equal H/L
MIN_BOS_BREAK_PCT     = 0.0015  # 0.15% minimum close beyond swing for valid BOS
BOS_LOOKBACK          = 48      # H1 candles to look back for BOS


# ── Swing detection ────────────────────────────────────────────────────────

def _swing_highs(candles: list, strength: int = SWING_STRENGTH) -> list:
    """Swing high price levels — candle whose high exceeds N candles each side."""
    if len(candles) < strength * 2 + 1:
        return []
    highs = []
    for i in range(strength, len(candles) - strength):
        pivot = candles[i]["high"]
        if all(pivot > candles[i - j]["high"] for j in range(1, strength + 1)) and \
           all(pivot > candles[i + j]["high"] for j in range(1, strength + 1)):
            highs.append(pivot)
    return highs


def _swing_lows(candles: list, strength: int = SWING_STRENGTH) -> list:
    """Swing low price levels — candle whose low is below N candles each side."""
    if len(candles) < strength * 2 + 1:
        return []
    lows = []
    for i in range(strength, len(candles) - strength):
        pivot = candles[i]["low"]
        if all(pivot < candles[i - j]["low"] for j in range(1, strength + 1)) and \
           all(pivot < candles[i + j]["low"] for j in range(1, strength + 1)):
            lows.append(pivot)
    return lows


def find_equal_levels(candles: list, bias: str,
                      tolerance: float = EQUAL_LEVEL_TOLERANCE) -> list:
    """
    Find equal highs (EQH) or equal lows (EQL).
    bias='bearish' → equal highs (sell-side liquidity above price)
    bias='bullish' → equal lows  (buy-side liquidity below price)
    Two extremes are "equal" when within tolerance (default 0.3%).
    Identical logic to BEEM's find_equal_levels().
    """
    if len(candles) < 3:
        return []
    levels = []
    if bias == "bearish":
        vals = [c["high"] for c in candles]
    else:
        vals = [c["low"] for c in candles]

    for i in range(len(vals)):
        for j in range(i + 1, len(vals)):
            ref = vals[i]
            if ref == 0:
                continue
            if abs(vals[j] - ref) / ref <= tolerance:
                levels.append((vals[i] + vals[j]) / 2)

    if not levels:
        return []
    levels.sort()
    merged = [levels[0]]
    for lvl in levels[1:]:
        if abs(lvl - merged[-1]) / max(merged[-1], 1e-10) > tolerance:
            merged.append(lvl)
    return merged


# ── ATR helper ─────────────────────────────────────────────────────────────

def _atr(candles: list, period: int = 14) -> float:
    """Simple ATR — average of high-low range over period."""
    window = candles[-period:] if len(candles) >= period else candles
    if not window:
        return 0.0
    return sum(c["high"] - c["low"] for c in window) / len(window)


# ── HTF Bias ───────────────────────────────────────────────────────────────

def get_htf_bias(candles_daily: list, candles_h4: list) -> dict:
    """
    Determine HTF bias using:
    - Daily: price position relative to last 20-day range midpoint
    - H4: direction of last confirmed BOS (with 0.15% minimum break)

    Returns {daily_bias, h4_bias, bias, daily_note, h4_note}
    """
    # ── Daily bias ─────────────────────────────────────────────────────────
    daily_bias = "neutral"
    daily_note = ""
    if len(candles_daily) >= 10:
        last_20  = candles_daily[-20:]
        hi       = max(c["high"]  for c in last_20)
        lo       = min(c["low"]   for c in last_20)
        midpoint = (hi + lo) / 2
        current  = candles_daily[-1]["close"]
        if current > midpoint * 1.01:
            daily_bias = "bullish"
            daily_note = f"Price {current:.4f} above 20D range midpoint {midpoint:.4f}"
        elif current < midpoint * 0.99:
            daily_bias = "bearish"
            daily_note = f"Price {current:.4f} below 20D range midpoint {midpoint:.4f}"
        else:
            daily_note = f"Price {current:.4f} at range midpoint {midpoint:.4f} — neutral"

    # ── H4 BOS direction ───────────────────────────────────────────────────
    h4_bias = "neutral"
    h4_note = ""
    if len(candles_h4) >= 10:
        bos = detect_bos(candles_h4, lookback=30)
        if bos.get("broken"):
            h4_bias = bos["bias"]
            h4_note = f"Last H4 BOS: {h4_bias} at {bos.get('level', 0):.4f}"
        else:
            h4_note = "No confirmed H4 BOS recently"

    # ── Combined: daily takes precedence ───────────────────────────────────
    if daily_bias != "neutral":
        combined = daily_bias
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

def detect_sweeps(candles: list, bias: str = "both") -> list:
    """
    Detect liquidity sweeps using BEEM's logic:
    - Sweep = price wicks into a prior swing/equal level then closes back
    - Level hierarchy: equal highs/lows first, then swing highs/lows
    - Depth must exceed SWEEP_DEPTH_STANDARD × ATR
    - Age cap: SWEEP_CONTEXT_AGE candles (older = context only)
    - Returns most recent valid sweeps, deduplicated by level
    """
    if len(candles) < SWING_STRENGTH * 2 + 5:
        return []

    window = candles[-SWEEP_CONTEXT_AGE:] if len(candles) > SWEEP_CONTEXT_AGE else candles
    offset = len(candles) - len(window)

    # Level hierarchy (same as BEEM): equal levels > swing levels
    eq_highs = find_equal_levels(window, "bearish")
    eq_lows  = find_equal_levels(window, "bullish")
    sw_highs = _swing_highs(window)
    sw_lows  = _swing_lows(window)

    # Merge: equal levels take priority (more liquidity = more stops)
    all_bear_levels = sorted(set(eq_highs + sw_highs), reverse=True)
    all_bull_levels = sorted(set(eq_lows  + sw_lows))

    if len(all_bear_levels) > MAX_SWEEP_ZONES and bias == "bearish":
        log.debug(f"detect_sweeps: too many bear zones ({len(all_bear_levels)}) — choppy")
        return []
    if len(all_bull_levels) > MAX_SWEEP_ZONES and bias == "bullish":
        log.debug(f"detect_sweeps: too many bull zones ({len(all_bull_levels)}) — choppy")
        return []

    sweeps = []

    for i in range(len(window)):
        c   = window[i]
        age = len(window) - 1 - i

        # ATR at this point for depth gate
        atr_window = window[max(0, i - 14): i + 1]
        atr = _atr(atr_window)

        # ── Bullish sweep (sweep of lows) ───────────────────────────────
        if bias in ("bullish", "both"):
            for lvl in all_bull_levels:
                if c["low"] < lvl:
                    depth = lvl - c["low"]
                    if atr > 0 and depth < atr * SWEEP_DEPTH_STANDARD:
                        continue  # not deep enough — noise
                    has_rejection = c["close"] > lvl
                    is_eq         = lvl in eq_lows
                    confluence    = int(is_eq) + int(has_rejection)
                    entry_valid   = age <= SWEEP_ENTRY_AGE
                    sweeps.append({
                        "type":          "bullish_sweep",
                        "direction":     "bullish",
                        "level":         lvl,
                        "sweep_low":     c["low"],
                        "depth_pct":     round(depth / lvl * 100, 3),
                        "depth_atr":     round(depth / atr, 2) if atr > 0 else 0,
                        "has_rejection": has_rejection,
                        "is_equal_hl":   is_eq,
                        "confluence":    confluence,
                        "age_candles":   age,
                        "entry_valid":   entry_valid,
                        "candle_idx":    offset + i,
                        "open_time":     c.get("open_time", ""),
                        "close":         c["close"],
                        "description":   (
                            f"Swept {'EQL' if is_eq else 'swing low'} {lvl:.4f} "
                            f"— wick to {c['low']:.4f}, closed {c['close']:.4f}"
                            + (" ✅ rejection" if has_rejection else " ⚠️ no rejection")
                        ),
                    })

        # ── Bearish sweep (sweep of highs) ──────────────────────────────
        if bias in ("bearish", "both"):
            for lvl in all_bear_levels:
                if c["high"] > lvl:
                    depth = c["high"] - lvl
                    if atr > 0 and depth < atr * SWEEP_DEPTH_STANDARD:
                        continue
                    has_rejection = c["close"] < lvl
                    is_eq         = lvl in eq_highs
                    confluence    = int(is_eq) + int(has_rejection)
                    entry_valid   = age <= SWEEP_ENTRY_AGE
                    sweeps.append({
                        "type":          "bearish_sweep",
                        "direction":     "bearish",
                        "level":         lvl,
                        "sweep_high":    c["high"],
                        "depth_pct":     round(depth / lvl * 100, 3),
                        "depth_atr":     round(depth / atr, 2) if atr > 0 else 0,
                        "has_rejection": has_rejection,
                        "is_equal_hl":   is_eq,
                        "confluence":    confluence,
                        "age_candles":   age,
                        "entry_valid":   entry_valid,
                        "candle_idx":    offset + i,
                        "open_time":     c.get("open_time", ""),
                        "close":         c["close"],
                        "description":   (
                            f"Swept {'EQH' if is_eq else 'swing high'} {lvl:.4f} "
                            f"— wick to {c['high']:.4f}, closed {c['close']:.4f}"
                            + (" ✅ rejection" if has_rejection else " ⚠️ no rejection")
                        ),
                    })

    # Sort: valid entries first, then by age (freshest first), then confluence
    sweeps.sort(key=lambda x: (
        not x["entry_valid"],
        x["age_candles"],
        -x["confluence"],
    ))

    # Deduplicate by level (keep best per level)
    seen = set()
    unique = []
    for s in sweeps:
        lvl_key = round(s["level"], 6)
        if lvl_key not in seen:
            seen.add(lvl_key)
            unique.append(s)

    return unique


# ── BOS Detection ──────────────────────────────────────────────────────────

def detect_bos(candles: list, lookback: int = BOS_LOOKBACK) -> dict:
    """
    Detect the most recent Break of Structure.
    BOS requires close beyond swing level by at least MIN_BOS_BREAK_PCT (0.15%).
    This filters the noise that was generating false BOS on 0.01% moves.
    """
    if len(candles) < 10:
        return {"broken": False}

    window = candles[-lookback:] if len(candles) > lookback else candles
    offset = len(candles) - len(window)
    sw_highs = _swing_highs(window)
    sw_lows  = _swing_lows(window)

    last_bos = {"broken": False, "candle_idx": -1}

    for i in range(len(window)):
        c = window[i]
        abs_idx = offset + i

        # Bullish BOS: close above swing high by at least MIN_BOS_BREAK_PCT
        for lvl in sw_highs:
            if c["close"] > lvl * (1 + MIN_BOS_BREAK_PCT):
                if abs_idx > last_bos.get("candle_idx", -1):
                    last_bos = {
                        "broken":      True,
                        "bias":        "bullish",
                        "level":       lvl,
                        "candle_idx":  abs_idx,
                        "open_time":   c.get("open_time", ""),
                        "description": f"Bullish BOS — closed {c['close']:.4f} above swing high {lvl:.4f}",
                    }

        # Bearish BOS: close below swing low by at least MIN_BOS_BREAK_PCT
        for lvl in sw_lows:
            if c["close"] < lvl * (1 - MIN_BOS_BREAK_PCT):
                if abs_idx > last_bos.get("candle_idx", -1):
                    last_bos = {
                        "broken":      True,
                        "bias":        "bearish",
                        "level":       lvl,
                        "candle_idx":  abs_idx,
                        "open_time":   c.get("open_time", ""),
                        "description": f"Bearish BOS — closed {c['close']:.4f} below swing low {lvl:.4f}",
                    }

    return last_bos


# ── Inducement Zones ───────────────────────────────────────────────────────

def find_inducement_zones(candles: list, bias: str) -> list:
    """
    Inducement zones: equal highs/lows and tested swing levels on the path
    toward the next major target. These are the short-stop pools price hunts
    before the real continuation.

    Uses BEEM's INDUCEMENT_LOOKBACK (15 candles) and equal level tolerance.
    Focuses on EQUAL levels first (stronger inducement = more stops clustered).
    """
    if len(candles) < 10:
        return []

    current_price = candles[-1]["close"]
    window        = candles[-INDUCEMENT_LOOKBACK:] if len(candles) > INDUCEMENT_LOOKBACK else candles
    zones = []

    if bias == "bullish":
        # Equal highs above price — buy-side liquidity to be swept
        eq_highs = find_equal_levels(window, "bearish")
        sw_highs = _swing_highs(window)
        all_above = sorted(set(
            lvl for lvl in eq_highs + sw_highs if lvl > current_price
        ))
        for lvl in all_above:
            is_eq = lvl in eq_highs
            zones.append({
                "price":        lvl,
                "type":         "buy_side_liquidity",
                "is_equal":     is_eq,
                "note":         "Equal highs — strong inducement" if is_eq else "Swing high liquidity",
                "distance_pct": round((lvl - current_price) / current_price * 100, 2),
            })

    elif bias == "bearish":
        # Equal lows below price — sell-side liquidity to be swept
        eq_lows = find_equal_levels(window, "bullish")
        sw_lows = _swing_lows(window)
        all_below = sorted(set(
            lvl for lvl in eq_lows + sw_lows if lvl < current_price
        ), reverse=True)
        for lvl in all_below:
            is_eq = lvl in eq_lows
            zones.append({
                "price":        lvl,
                "type":         "sell_side_liquidity",
                "is_equal":     is_eq,
                "note":         "Equal lows — strong inducement" if is_eq else "Swing low liquidity",
                "distance_pct": round((current_price - lvl) / current_price * 100, 2),
            })

    # Sort: equal levels first (stronger), then closest
    zones.sort(key=lambda x: (not x["is_equal"], x["distance_pct"]))
    return zones[:MAX_SWEEP_ZONES]


# ── Displacement & Order Block ─────────────────────────────────────────────
# From BEEM strategy_constants: MIN_DISPLACEMENT_BODY = 0.55
# A displacement candle's body must be >= 55% of its range.
# This separates genuine institutional moves from wicky indecision.
MIN_DISPLACEMENT_BODY = 0.55
DISPLACEMENT_LOOKBACK = 5   # candles after sweep to look for displacement


def detect_displacement(candles: list, sweep_candle_idx: int,
                        direction: str) -> dict:
    """
    After a sweep, did a strong displacement candle follow?

    BEEM threshold: body >= 55% of range (MIN_DISPLACEMENT_BODY).
    Direction: "bullish" = looking for strong green candle after sweep of lows.
               "bearish" = looking for strong red candle after sweep of highs.

    Returns {confirmed, candle_idx, body_ratio, description}
    Scout uses this as CONTEXT for 0G reasoning — not as a hard execution gate.
    """
    if sweep_candle_idx < 0 or sweep_candle_idx >= len(candles):
        return {"confirmed": False, "reason": "Invalid sweep index"}

    # Look at the next DISPLACEMENT_LOOKBACK candles after the sweep
    search_start = sweep_candle_idx + 1
    search_end   = min(search_start + DISPLACEMENT_LOOKBACK, len(candles))

    best = None
    for i in range(search_start, search_end):
        c = candles[i]
        candle_range = c["high"] - c["low"]
        if candle_range == 0:
            continue
        body = abs(c["close"] - c["open"])
        body_ratio = body / candle_range

        is_bullish_candle = c["close"] > c["open"]
        is_bearish_candle = c["close"] < c["open"]

        if direction == "bullish" and is_bullish_candle and body_ratio >= MIN_DISPLACEMENT_BODY:
            if best is None or body_ratio > best["body_ratio"]:
                best = {
                    "confirmed":   True,
                    "candle_idx":  i,
                    "body_ratio":  round(body_ratio, 3),
                    "open_time":   c.get("open_time", ""),
                    "open":        c["open"],
                    "close":       c["close"],
                    "description": (
                        f"Bullish displacement confirmed — "
                        f"body {body_ratio:.0%} of range at {c['close']:.4f}"
                    ),
                }
        elif direction == "bearish" and is_bearish_candle and body_ratio >= MIN_DISPLACEMENT_BODY:
            if best is None or body_ratio > best["body_ratio"]:
                best = {
                    "confirmed":   True,
                    "candle_idx":  i,
                    "body_ratio":  round(body_ratio, 3),
                    "open_time":   c.get("open_time", ""),
                    "open":        c["open"],
                    "close":       c["close"],
                    "description": (
                        f"Bearish displacement confirmed — "
                        f"body {body_ratio:.0%} of range at {c['close']:.4f}"
                    ),
                }

    if best:
        return best

    return {
        "confirmed":   False,
        "reason":      f"No displacement candle (body ≥55%) in next {DISPLACEMENT_LOOKBACK} candles after sweep",
        "description": "No displacement — sweep may be a fake-out",
    }


def find_order_block(candles: list, displacement_idx: int,
                     direction: str) -> dict:
    """
    Find the Order Block: the last OPPOSING candle immediately before
    the displacement move.

    Bullish OB = last RED candle before bullish displacement.
    Bearish OB = last GREEN candle before bearish displacement.

    Price returns to this candle's range for the actual entry.
    Scout surfaces this as the entry zone — 0G Compute explains it.
    """
    if displacement_idx <= 0 or displacement_idx >= len(candles):
        return {"found": False}

    # Walk backwards from the displacement candle
    for i in range(displacement_idx - 1, max(0, displacement_idx - 10), -1):
        c = candles[i]
        is_bullish_candle = c["close"] > c["open"]
        is_bearish_candle = c["close"] < c["open"]

        if direction == "bullish" and is_bearish_candle:
            # Last red candle before bullish displacement = bullish OB
            return {
                "found":       True,
                "type":        "bullish_ob",
                "candle_idx":  i,
                "high":        c["high"],
                "low":         c["low"],
                "open":        c["open"],
                "close":       c["close"],
                "open_time":   c.get("open_time", ""),
                "description": (
                    f"Bullish OB at {c['low']:.4f}–{c['high']:.4f} "
                    f"(last red candle before displacement)"
                ),
            }
        elif direction == "bearish" and is_bullish_candle:
            # Last green candle before bearish displacement = bearish OB
            return {
                "found":       True,
                "type":        "bearish_ob",
                "candle_idx":  i,
                "high":        c["high"],
                "low":         c["low"],
                "open":        c["open"],
                "close":       c["close"],
                "open_time":   c.get("open_time", ""),
                "description": (
                    f"Bearish OB at {c['low']:.4f}–{c['high']:.4f} "
                    f"(last green candle before displacement)"
                ),
            }

    return {"found": False, "reason": "No opposing candle found near displacement"}

def full_structure_snapshot(symbol: str, candles_h1: list,
                            candles_h4: list, candles_daily: list) -> dict:
    """
    Full SMC picture: HTF bias, sweeps, BOS, inducement zones,
    displacement confirmation, and order block.
    """
    bias_data    = get_htf_bias(candles_daily, candles_h4)
    bias         = bias_data["bias"]
    h1_sweeps    = detect_sweeps(candles_h1, bias=bias if bias != "neutral" else "both")
    recent_sweep = next((s for s in h1_sweeps if s.get("entry_valid")), None)
    h1_bos       = detect_bos(candles_h1)
    inducement   = find_inducement_zones(candles_h1, bias) if bias != "neutral" else []

    # ── Displacement + OB (only if there's a recent sweep) ───────────────
    displacement = {"confirmed": False}
    order_block  = {"found": False}

    if recent_sweep:
        sweep_direction = recent_sweep.get("direction", bias)
        sweep_idx       = recent_sweep.get("candle_idx", -1)

        displacement = detect_displacement(
            candles_h1, sweep_idx, sweep_direction
        )
        if displacement.get("confirmed"):
            order_block = find_order_block(
                candles_h1,
                displacement["candle_idx"],
                sweep_direction,
            )

    return {
        "symbol":           symbol,
        "bias":             bias,
        "bias_data":        bias_data,
        "recent_sweep":     recent_sweep,
        "all_sweeps_h1":    h1_sweeps[:3],
        "bos_h1":           h1_bos,
        "inducement_zones": inducement,
        "displacement":     displacement,
        "order_block":      order_block,
        "current_price":    candles_h1[-1]["close"] if candles_h1 else 0,
        "atr_h1":           round(_atr(candles_h1), 6) if candles_h1 else 0,
    }
