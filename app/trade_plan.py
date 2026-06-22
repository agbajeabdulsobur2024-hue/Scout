"""
trade_plan.py — Deterministic trade plan generator for Scout.

Generates Entry / Stop / TP1 / TP2 / RR from actual market structure.
No invented levels. Every number derives from detected OB, ATR, swing levels,
and FVG zones present in the intelligence object.

Priority 4 of Scout Master Roadmap.
"""

import logging

log = logging.getLogger("scout")


def generate_trade_plan(intel: dict) -> dict:
    """
    Build a full trade plan from market intelligence.

    Uses (in priority order):
      Entry:  Order Block zone → FVG midpoint → current price ± 0.5 ATR
      Stop:   Below/above OB → below/above sweep wick → current price ± 1.5 ATR
      TP1:    Nearest inducement zone → 1.5 × risk
      TP2:    Next inducement / swing level → 3 × risk (or better)

    Returns dict with direction, entry_low, entry_high, stop, tp1, tp2, rr1, rr2,
    confidence, and a formatted text block.
    """
    if intel.get("error"):
        return {"error": intel["error"]}

    bias     = intel.get("htf_bias", "Neutral").lower()
    price    = intel.get("price", 0)
    atr      = intel.get("atr_h1", 0)
    ob       = intel.get("order_block", {})
    fvg      = intel.get("fvg", {})
    sweep    = intel.get("sweep", {})
    bos      = intel.get("bos", {})
    indu     = intel.get("inducements", [])
    target   = intel.get("target", {})
    score    = intel.get("score", 0)
    disp     = intel.get("displacement", {})

    if not price or not atr:
        return {"error": "Insufficient price/ATR data"}

    if bias not in ("bullish", "bearish"):
        return {"error": "No directional bias established — no trade plan possible"}

    direction = "LONG" if bias == "bullish" else "SHORT"

    # ── Entry zone ────────────────────────────────────────────────────────
    entry_low  = None
    entry_high = None
    entry_src  = ""

    if ob.get("found"):
        entry_low  = ob["low"]
        entry_high = ob["high"]
        entry_src  = "Order Block"
    elif fvg.get("found"):
        mid        = fvg["midpoint"]
        half       = atr * 0.3
        entry_low  = round(mid - half, 6)
        entry_high = round(mid + half, 6)
        entry_src  = "FVG midpoint"
    else:
        # Fallback: price ± 0.5 ATR in direction of bias
        if direction == "LONG":
            entry_low  = round(price - atr * 0.5, 6)
            entry_high = round(price - atr * 0.1, 6)
        else:
            entry_low  = round(price + atr * 0.1, 6)
            entry_high = round(price + atr * 0.5, 6)
        entry_src = "ATR projection"

    # ── Stop loss ─────────────────────────────────────────────────────────
    stop      = None
    stop_src  = ""

    if direction == "LONG":
        # Stop below sweep wick or OB low, minimum 1.2 ATR from entry
        if sweep.get("level") and sweep.get("detected"):
            stop_candidate = sweep["level"] - atr * 0.3
        elif ob.get("found"):
            stop_candidate = ob["low"] - atr * 0.3
        else:
            stop_candidate = entry_low - atr * 1.2
        stop     = round(min(stop_candidate, entry_low - atr * 0.8), 6)
        stop_src = "Below sweep/OB"
    else:
        if sweep.get("level") and sweep.get("detected"):
            stop_candidate = sweep["level"] + atr * 0.3
        elif ob.get("found"):
            stop_candidate = ob["high"] + atr * 0.3
        else:
            stop_candidate = entry_high + atr * 1.2
        stop     = round(max(stop_candidate, entry_high + atr * 0.8), 6)
        stop_src = "Above sweep/OB"

    # ── Risk (from entry midpoint to stop) ───────────────────────────────
    entry_mid = (entry_low + entry_high) / 2
    if direction == "LONG":
        risk = entry_mid - stop
    else:
        risk = stop - entry_mid

    if risk <= 0:
        return {"error": "Invalid risk calculation — stop overlaps entry"}

    # ── TP1: nearest inducement or 1.5R ───────────────────────────────────
    tp1      = None
    tp1_src  = ""

    if indu:
        tp1_candidate = indu[0]["price"]
        tp1_min = entry_mid + risk * 1.5 if direction == "LONG" else entry_mid - risk * 1.5
        if direction == "LONG" and tp1_candidate > entry_mid:
            tp1     = round(tp1_candidate, 6)
            tp1_src = f"Inducement zone ({indu[0].get('note','liquidity')})"
        elif direction == "SHORT" and tp1_candidate < entry_mid:
            tp1     = round(tp1_candidate, 6)
            tp1_src = f"Inducement zone ({indu[0].get('note','liquidity')})"

    if tp1 is None:
        tp1     = round(entry_mid + risk * 1.5 if direction == "LONG" else entry_mid - risk * 1.5, 6)
        tp1_src = "1.5R projection"

    # ── TP2: further inducement or 3R ─────────────────────────────────────
    tp2      = None
    tp2_src  = ""

    if len(indu) > 1:
        tp2_candidate = indu[1]["price"]
        if direction == "LONG" and tp2_candidate > tp1:
            tp2     = round(tp2_candidate, 6)
            tp2_src = f"Inducement zone ({indu[1].get('note','liquidity')})"
        elif direction == "SHORT" and tp2_candidate < tp1:
            tp2     = round(tp2_candidate, 6)
            tp2_src = f"Inducement zone ({indu[1].get('note','liquidity')})"

    if tp2 is None:
        if target and target.get("rr_viable"):
            tp2     = round(target["price"], 6)
            tp2_src = "Swing target"
        else:
            tp2     = round(entry_mid + risk * 3.0 if direction == "LONG" else entry_mid - risk * 3.0, 6)
            tp2_src = "3R projection"

    # ── Risk/Reward ───────────────────────────────────────────────────────
    if direction == "LONG":
        rr1 = round((tp1 - entry_mid) / risk, 1) if risk > 0 else 0
        rr2 = round((tp2 - entry_mid) / risk, 1) if risk > 0 else 0
    else:
        rr1 = round((entry_mid - tp1) / risk, 1) if risk > 0 else 0
        rr2 = round((entry_mid - tp2) / risk, 1) if risk > 0 else 0

    # ── Confluence count ──────────────────────────────────────────────────
    confluence = []
    if bos.get("broken") and bos.get("bias") == bias:
        confluence.append(f"{bos.get('bias','').capitalize()} BOS confirmed")
    if sweep.get("detected"):
        confluence.append(f"Liquidity sweep — {sweep.get('description','')[:50]}")
    if disp.get("confirmed"):
        confluence.append(f"Displacement confirmed ({disp.get('description','')[:40]})")
    if ob.get("found"):
        confluence.append(f"Order Block at {ob['low']:.4f}–{ob['high']:.4f}")
    if fvg.get("found"):
        confluence.append(f"FVG {fvg['low']:.4f}–{fvg['high']:.4f}")
    if intel.get("session", {}).get("in_killzone"):
        confluence.append(f"{intel['session']['session']} Kill Zone active")
    if intel.get("funding", {}).get("aligned"):
        confluence.append(f"Funding aligned")

    # ── Confidence label ─────────────────────────────────────────────────
    conf_score = score
    if conf_score >= 80:
        confidence = "High"
    elif conf_score >= 55:
        confidence = "Medium"
    else:
        confidence = "Low"

    plan = {
        "symbol":      intel["symbol"],
        "direction":   direction,
        "entry_low":   entry_low,
        "entry_high":  entry_high,
        "entry_mid":   round(entry_mid, 6),
        "stop":        stop,
        "tp1":         tp1,
        "tp2":         tp2,
        "rr1":         rr1,
        "rr2":         rr2,
        "risk_atr":    round(risk / atr, 2),
        "entry_src":   entry_src,
        "stop_src":    stop_src,
        "tp1_src":     tp1_src,
        "tp2_src":     tp2_src,
        "confluence":  confluence,
        "confidence":  confidence,
        "score":       conf_score,
        "bias":        bias,
        "regime":      intel.get("regime", "Unknown"),
        "htf_bias":    intel.get("htf_bias", "Neutral"),
    }

    return plan


def format_trade_plan(plan: dict, symbol_override: str = None) -> str:
    """
    Format a trade plan dict into a clean Telegram-ready text block.
    This is the canonical output format for all setup alerts.
    """
    if plan.get("error"):
        return f"⚠️ No trade plan: {plan['error']}"

    sym    = (symbol_override or plan["symbol"]).replace("USDT", "").replace("_USDT", "")
    dirn   = plan["direction"]
    flag   = "🟢" if dirn == "LONG" else "🔴"

    lines = [
        f"{flag} <b>{sym} {dirn}</b>",
        f"",
        f"Bias:    <b>{plan['htf_bias']}</b>  |  Regime: {plan['regime']}",
        f"",
        f"Entry:   {plan['entry_low']:.6g} – {plan['entry_high']:.6g}",
        f"SL:      {plan['stop']:.6g}",
        f"TP1:     {plan['tp1']:.6g}  (RR 1:{plan['rr1']})",
        f"TP2:     {plan['tp2']:.6g}  (RR 1:{plan['rr2']})",
        f"",
    ]

    if plan["confluence"]:
        lines.append("Confluence:")
        for c in plan["confluence"]:
            lines.append(f"  ✓ {c}")
        lines.append("")

    lines.append(f"Confidence: <b>{plan['confidence']}</b>  ({plan['score']}/100)")

    return "\n".join(lines)
