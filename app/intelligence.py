"""
intelligence.py — Scout's Market Intelligence Engine.

This is the layer between raw market data and 0G Compute.
Instead of asking the model to discover structure, we detect it here
and pass FACTS to 0G Compute for explanation.

Architecture:
  Market Data
  ↓
  Structure Engine (structure.py)
  ↓
  Market Intelligence Engine (this file)  ← NEW
  ↓
  0G Compute
  ↓
  Evidence-Based Explanation

The intelligence object gives 0G Compute:
  - Regime (Expansion / Compression / Exhaustion)
  - HTF Bias (Bullish / Bearish / Neutral)
  - Liquidity Sweep (yes/no + which level + description)
  - Displacement (yes/no + quality)
  - BOS level
  - Order Block zone
  - FVG (Fair Value Gap)
  - Volume quality (spike confirmed or not)
  - Funding alignment
  - Nearest target liquidity
  - Session quality (Kill Zone or not)
  - Composite opportunity score (0-100)
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger("scout")


# ── Scoring weights ────────────────────────────────────────────────────────
# Each factor contributes to the opportunity score.
# Total possible = 100.
W_SWEEP          = 15   # liquidity sweep present
W_DISPLACEMENT   = 20   # displacement confirmed after sweep
W_BOS            = 12   # break of structure
W_HTF_ALIGNMENT  = 15   # trade direction matches HTF bias
W_REGIME         = 8    # regime is Expansion (trending)
W_VOLUME         = 8    # volume spike confirming the move
W_FUNDING        = 7    # funding alignment (not fighting)
W_DISTANCE       = 8    # good distance to target (RR)
W_SESSION        = 7    # kill zone (London/NY) active

SCORE_MAX = (W_SWEEP + W_DISPLACEMENT + W_BOS + W_HTF_ALIGNMENT +
             W_REGIME + W_VOLUME + W_FUNDING + W_DISTANCE + W_SESSION)


def _detect_regime(candles_h4: list) -> str:
    """
    Market regime detection using H4 candles.
    Expansion  = strong trending: ATR above 20-period median, directional price
    Compression = ranging: ATR below median, price oscillating
    Exhaustion  = potential reversal: very wide candles after extended trend
    """
    if len(candles_h4) < 20:
        return "Unknown"

    window = candles_h4[-20:]
    ranges = [c["high"] - c["low"] for c in window]
    avg_range = sum(ranges) / len(ranges)
    recent_range = sum(ranges[-5:]) / 5
    atr_ratio = recent_range / avg_range if avg_range > 0 else 1.0

    # Price direction over last 10 candles
    closes = [c["close"] for c in candles_h4[-10:]]
    up_moves   = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
    down_moves = len(closes) - 1 - up_moves
    directional = abs(up_moves - down_moves) >= 5

    if atr_ratio > 1.3 and directional:
        return "Expansion"
    elif atr_ratio < 0.7:
        return "Compression"
    elif atr_ratio > 1.8:
        return "Exhaustion"
    else:
        return "Compression" if not directional else "Expansion"


def _detect_fvg(candles: list, direction: str, lookback: int = 10) -> dict:
    """
    Detect Fair Value Gap (FVG) — 3-candle imbalance.
    Bullish FVG: candle[i].low > candle[i-2].high  (gap between 1st and 3rd)
    Bearish FVG: candle[i].high < candle[i-2].low
    Returns the most recent FVG zone.
    """
    if len(candles) < 3:
        return {}

    search = candles[-lookback:] if len(candles) >= lookback else candles

    # Walk backwards (most recent first)
    for i in range(len(search) - 1, 1, -1):
        c_curr = search[i]
        c_prev = search[i - 2]  # two candles back

        if direction == "bullish":
            # Gap between c_prev high and c_curr low
            if c_curr["low"] > c_prev["high"]:
                gap_low  = c_prev["high"]
                gap_high = c_curr["low"]
                midpoint = (gap_low + gap_high) / 2
                return {
                    "found":       True,
                    "type":        "bullish_fvg",
                    "low":         round(gap_low,  6),
                    "high":        round(gap_high, 6),
                    "midpoint":    round(midpoint, 6),
                    "open_time":   search[i].get("open_time", ""),
                    "description": f"Bullish FVG {gap_low:.4f}–{gap_high:.4f}",
                }
        elif direction == "bearish":
            # Gap between c_curr high and c_prev low
            if c_curr["high"] < c_prev["low"]:
                gap_low  = c_curr["high"]
                gap_high = c_prev["low"]
                midpoint = (gap_low + gap_high) / 2
                return {
                    "found":       True,
                    "type":        "bearish_fvg",
                    "low":         round(gap_low,  6),
                    "high":        round(gap_high, 6),
                    "midpoint":    round(midpoint, 6),
                    "open_time":   search[i].get("open_time", ""),
                    "description": f"Bearish FVG {gap_low:.4f}–{gap_high:.4f}",
                }

    return {"found": False}


def _session_quality() -> dict:
    """
    Is price currently in a kill zone?
    London: 08:00–11:00 UTC
    New York: 13:00–16:00 UTC
    """
    now_utc = datetime.now(timezone.utc)
    hour    = now_utc.hour

    if 8 <= hour < 11:
        return {"in_killzone": True, "session": "London", "description": "London Kill Zone (08:00–11:00 UTC)"}
    elif 13 <= hour < 16:
        return {"in_killzone": True, "session": "New York", "description": "New York Kill Zone (13:00–16:00 UTC)"}
    elif 7 <= hour < 8:
        return {"in_killzone": False, "session": "Pre-London", "description": "Pre-London setup window"}
    elif 22 <= hour or hour < 2:
        return {"in_killzone": False, "session": "Asia", "description": "Asia session (liquidity build)"}
    else:
        return {"in_killzone": False, "session": "Off-Hours", "description": "Between sessions — lower quality"}


def _volume_quality(candles: list, lookback: int = 20) -> dict:
    """
    Is the most recent move backed by above-average volume?
    Returns volume spike ratio and quality label.
    """
    if len(candles) < 5:
        return {"spike": False, "ratio": 1.0, "description": "Insufficient data"}

    window     = candles[-lookback:] if len(candles) >= lookback else candles
    avg_volume = sum(c["volume"] for c in window[:-1]) / max(len(window) - 1, 1)
    last_vol   = candles[-1]["volume"]

    if avg_volume == 0:
        return {"spike": False, "ratio": 1.0, "description": "No volume data"}

    ratio = round(last_vol / avg_volume, 2)

    if ratio >= 3.0:
        label = "Extreme spike"
    elif ratio >= 2.0:
        label = "Strong spike"
    elif ratio >= 1.5:
        label = "Moderate spike"
    elif ratio >= 1.0:
        label = "Normal"
    else:
        label = "Below average"

    return {
        "spike":       ratio >= 1.5,
        "ratio":       ratio,
        "description": f"Volume {ratio:.1f}x average — {label}",
    }


def _find_target_liquidity(candles: list, bias: str, current_price: float) -> dict:
    """
    What's the nearest logical target?
    Bullish: previous day high, recent swing high, equal highs above
    Bearish: previous day low, recent swing low, equal lows below
    """
    if len(candles) < 5 or current_price == 0:
        return {}

    targets = []

    if bias == "bullish":
        # Look for highs above current price
        for i in range(len(candles) - 2, max(0, len(candles) - 30), -1):
            h = candles[i]["high"]
            if h > current_price * 1.001:  # at least 0.1% above
                dist_pct = (h - current_price) / current_price * 100
                targets.append({
                    "price":      round(h, 6),
                    "type":       "swing_high",
                    "distance":   round(dist_pct, 2),
                    "label":      f"Swing High {h:.4f} (+{dist_pct:.1f}%)",
                })
    else:
        # Look for lows below current price
        for i in range(len(candles) - 2, max(0, len(candles) - 30), -1):
            l = candles[i]["low"]
            if l < current_price * 0.999:
                dist_pct = (current_price - l) / current_price * 100
                targets.append({
                    "price":      round(l, 6),
                    "type":       "swing_low",
                    "distance":   round(dist_pct, 2),
                    "label":      f"Swing Low {l:.4f} (-{dist_pct:.1f}%)",
                })

    if not targets:
        return {}

    # Sort by nearest
    targets.sort(key=lambda x: x["distance"])
    nearest = targets[0]

    # RR quality: is there enough distance to make it worthwhile?
    rr_ok = nearest["distance"] >= 0.5  # at least 0.5% to target

    return {
        "price":       nearest["price"],
        "label":       nearest["label"],
        "distance_pct": nearest["distance"],
        "rr_viable":   rr_ok,
        "all_targets": targets[:3],
    }


def _funding_alignment(funding_rate: float, bias: str) -> dict:
    """
    Is funding aligned with the trade direction?
    Positive funding = longs pay = bullish bias but potential reversal risk
    Negative funding = shorts pay = bearish bias but squeeze risk

    For a BULLISH trade:
      - Negative or neutral funding = ALIGNED (shorts being paid, crowd is bearish = smart money bullish)
      - High positive funding = NOT aligned (crowd is long = mean reversion risk)

    For a BEARISH trade:
      - Positive or neutral funding = ALIGNED
      - High negative funding = NOT aligned
    """
    aligned    = False
    note       = ""
    rate_pct   = funding_rate * 100

    if bias == "bullish":
        if funding_rate <= 0.0003:  # neutral or negative
            aligned = True
            note    = f"Funding {rate_pct:+.4f}% — aligned (crowd not overleveraged long)"
        elif funding_rate >= 0.003:
            note    = f"Funding {rate_pct:+.4f}% — caution (crowd heavily long, reversal risk)"
        else:
            aligned = True
            note    = f"Funding {rate_pct:+.4f}% — mild positive, acceptable"
    elif bias == "bearish":
        if funding_rate >= -0.0003:
            aligned = True
            note    = f"Funding {rate_pct:+.4f}% — aligned (crowd not overleveraged short)"
        elif funding_rate <= -0.003:
            note    = f"Funding {rate_pct:+.4f}% — caution (crowd heavily short, squeeze risk)"
        else:
            aligned = True
            note    = f"Funding {rate_pct:+.4f}% — mild negative, acceptable"
    else:
        aligned = True
        note    = f"Funding {rate_pct:+.4f}% — neutral bias, no conflict"

    return {"aligned": aligned, "note": note, "rate_pct": round(rate_pct, 4)}


def build_intelligence(
    symbol: str,
    candles_m15: list,
    candles_h1: list,
    candles_h4: list,
    candles_daily: list,
    funding_rate: float = 0.0,
) -> dict:
    """
    Core function. Builds the full intelligence object for a symbol.

    Takes candles at multiple timeframes, runs all detection logic,
    and returns a structured dict ready to send to 0G Compute.

    This is what was missing before — the layer that extracts FACTS
    so the model explains reality instead of inventing it.
    """
    from app.structure import (
        full_structure_snapshot, get_htf_bias,
        detect_sweeps, detect_bos, find_inducement_zones,
    )

    # ── Use best available candles (h1 preferred, m15 fallback) ───────────
    entry_candles = candles_m15 if candles_m15 else candles_h1
    if not entry_candles:
        return {"symbol": symbol, "error": "No candle data available", "score": 0}

    current_price = entry_candles[-1]["close"] if entry_candles else 0

    # ── Structure snapshot ─────────────────────────────────────────────────
    try:
        snap = full_structure_snapshot(
            symbol,
            candles_h1 or entry_candles,
            candles_h4,
            candles_daily,
        )
    except Exception as e:
        log.debug(f"intelligence build_snapshot {symbol}: {e}")
        snap = {}

    bias            = snap.get("bias", "neutral")
    bias_data       = snap.get("bias_data", {})
    recent_sweep    = snap.get("recent_sweep")
    bos             = snap.get("bos_h1", {})
    displacement    = snap.get("displacement", {})
    order_block     = snap.get("order_block", {})
    inducements     = snap.get("inducement_zones", [])

    # ── Regime ────────────────────────────────────────────────────────────
    regime = _detect_regime(candles_h4) if candles_h4 else "Unknown"

    # ── FVG ───────────────────────────────────────────────────────────────
    fvg = _detect_fvg(candles_h1 or entry_candles, bias)

    # ── Volume quality ────────────────────────────────────────────────────
    vol_quality = _volume_quality(entry_candles)

    # ── Session ───────────────────────────────────────────────────────────
    session = _session_quality()

    # ── Target liquidity ─────────────────────────────────────────────────
    target = _find_target_liquidity(candles_h1 or entry_candles, bias, current_price)

    # ── Funding alignment ─────────────────────────────────────────────────
    funding = _funding_alignment(funding_rate, bias)

    # ── Composite Score ───────────────────────────────────────────────────
    score = 0

    if recent_sweep and recent_sweep.get("age_candles", 99) <= 6:
        score += W_SWEEP

    if displacement.get("confirmed"):
        score += W_DISPLACEMENT

    if bos.get("broken"):
        score += W_BOS

    if bias != "neutral":
        score += W_HTF_ALIGNMENT

    if regime == "Expansion":
        score += W_REGIME
    elif regime == "Compression":
        score += W_REGIME // 2  # partial — compression can still have setups

    if vol_quality.get("spike"):
        score += W_VOLUME

    if funding.get("aligned"):
        score += W_FUNDING

    if target and target.get("rr_viable"):
        score += W_DISTANCE

    if session.get("in_killzone"):
        score += W_SESSION

    # Normalise to 0-100
    score_pct = round(score / SCORE_MAX * 100)

    # ── Sweep details ─────────────────────────────────────────────────────
    sweep_info = {}
    if recent_sweep:
        sweep_info = {
            "detected":     True,
            "level":        recent_sweep.get("level", 0),
            "direction":    recent_sweep.get("direction", ""),
            "description":  recent_sweep.get("description", ""),
            "age_candles":  recent_sweep.get("age_candles", 0),
            "has_rejection": recent_sweep.get("has_rejection", False),
        }

    # ── BOS details ───────────────────────────────────────────────────────
    bos_info = {}
    if bos.get("broken"):
        bos_info = {
            "broken":      True,
            "bias":        bos.get("bias", ""),
            "level":       bos.get("level", 0),
            "description": bos.get("description", ""),
        }

    # ── OB details ───────────────────────────────────────────────────────
    ob_info = {}
    if order_block.get("found"):
        ob_info = {
            "found":       True,
            "type":        order_block.get("type", ""),
            "low":         order_block.get("low", 0),
            "high":        order_block.get("high", 0),
            "description": order_block.get("description", ""),
        }

    # ── Build final intelligence object ───────────────────────────────────
    intel = {
        "symbol":        symbol,
        "price":         round(current_price, 6),
        "regime":        regime,
        "htf_bias":      bias.capitalize(),
        "daily_note":    bias_data.get("daily_note", ""),
        "h4_note":       bias_data.get("h4_note", ""),
        "sweep":         sweep_info,
        "displacement":  {"confirmed": displacement.get("confirmed", False),
                          "description": displacement.get("description", "")},
        "bos":           bos_info,
        "order_block":   ob_info,
        "fvg":           fvg,
        "volume":        vol_quality,
        "session":       session,
        "funding":       funding,
        "target":        target,
        "inducements":   inducements[:2],
        "score":         score_pct,
        "atr_h1":        snap.get("atr_h1", 0),
    }

    return intel


def format_intelligence_for_model(intel: dict) -> str:
    """
    Format the intelligence object as a clean text block for 0G Compute.
    This replaces the old _format_snapshot() in reasoning.py.

    The model receives FACTS. It explains. It doesn't invent.
    """
    if intel.get("error"):
        return f"Error fetching data for {intel.get('symbol')}: {intel['error']}"

    sym   = intel["symbol"].replace("USDT", "")
    lines = [
        f"=== MARKET INTELLIGENCE: {sym} ===",
        f"",
        f"Price:           {intel['price']}",
        f"Regime:          {intel['regime']}",
        f"HTF Bias:        {intel['htf_bias']}",
    ]

    if intel.get("daily_note"):
        lines.append(f"Daily context:   {intel['daily_note']}")
    if intel.get("h4_note"):
        lines.append(f"H4 context:      {intel['h4_note']}")

    lines.append("")

    # Sweep
    sw = intel.get("sweep", {})
    if sw.get("detected"):
        lines.append(f"Liquidity Sweep: YES — {sw['description']}")
        lines.append(f"  Age:           {sw['age_candles']} candles ago")
        lines.append(f"  Rejection:     {'Yes' if sw.get('has_rejection') else 'No'}")
    else:
        lines.append(f"Liquidity Sweep: None detected recently")

    # Displacement
    disp = intel.get("displacement", {})
    if disp.get("confirmed"):
        lines.append(f"Displacement:    CONFIRMED — {disp['description']}")
    else:
        lines.append(f"Displacement:    Not confirmed")

    # BOS
    bos = intel.get("bos", {})
    if bos.get("broken"):
        lines.append(f"Break of Struct: YES — {bos['description']}")
    else:
        lines.append(f"Break of Struct: No BOS detected")

    # Order Block
    ob = intel.get("order_block", {})
    if ob.get("found"):
        lines.append(f"Order Block:     {ob['low']:.4f}–{ob['high']:.4f} ({ob['type']})")

    # FVG
    fvg = intel.get("fvg", {})
    if fvg.get("found"):
        lines.append(f"Fair Value Gap:  {fvg['low']:.4f}–{fvg['high']:.4f} ({fvg['type']})")

    lines.append("")

    # Volume
    vol = intel.get("volume", {})
    lines.append(f"Volume:          {vol.get('description', 'N/A')}")

    # Session
    sess = intel.get("session", {})
    kz   = "✓ ACTIVE" if sess.get("in_killzone") else "Not active"
    lines.append(f"Kill Zone:       {kz} — {sess.get('description', '')}")

    # Funding
    fund = intel.get("funding", {})
    lines.append(f"Funding:         {fund.get('note', 'N/A')}")

    # Target
    tgt = intel.get("target", {})
    if tgt:
        rr_label = "✓ viable" if tgt.get("rr_viable") else "close — tight RR"
        lines.append(f"Nearest Target:  {tgt.get('label', '')} (RR: {rr_label})")

    # Inducements
    indu = intel.get("inducements", [])
    if indu:
        lines.append(f"Inducements:     {', '.join(z['price'].__format__('.4f') + ' (' + z['distance_pct'].__format__('+.1f') + '%)' for z in indu[:2])}")

    lines.append("")
    lines.append(f"Opportunity Score: {intel['score']}/100")
    lines.append(f"ATR (H1):        {intel.get('atr_h1', 0):.6f}")

    return "\n".join(lines)


def rank_opportunities(
    symbols: list,
    candles_map: dict,   # {symbol: {m15, h1, h4, daily}}
    funding_map: dict = None,  # {symbol: rate_float}
    top_n: int = 5,
) -> list:
    """
    Build intelligence for all symbols, rank by score, return top N.
    Used for /opportunities command and proactive scanner ranking.
    """
    funding_map = funding_map or {}
    results     = []

    for sym in symbols:
        try:
            c = candles_map.get(sym, {})
            intel = build_intelligence(
                symbol        = sym,
                candles_m15   = c.get("m15", []),
                candles_h1    = c.get("h1", []),
                candles_h4    = c.get("h4", []),
                candles_daily = c.get("daily", []),
                funding_rate  = funding_map.get(sym, 0.0),
            )
            if not intel.get("error"):
                results.append(intel)
        except Exception as e:
            log.debug(f"rank_opportunities {sym}: {e}")

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]
