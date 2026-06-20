"""
reasoning.py — turns raw market_data snapshots into trader-facing answers,
via 0G Compute. This module never invents numbers and never pretends to
predict price; it explains what the data shows and lets the model reason
about *why* it might matter. The interpretation is genuinely produced by
the model on each call — there's no cached/templated answer underneath.
"""

import logging
from app import market_data
from app.zg_compute import ask, ZGComputeError

log = logging.getLogger("scout")

SYSTEM_PROMPT = (
    "You are Scout, an AI market intelligence companion for a professional crypto trader. "
    "You reason using Smart Money Concepts (SMC). Always use this framework:\n\n"

    "KEY CONCEPTS:\n"
    "- Liquidity sweeps: price wicks into a prior swing high/low (stop hunt), then rejects. "
    "Bullish sweep = sweep of lows (hunts buy stops), price closes back above. "
    "Bearish sweep = sweep of highs (hunts sell stops), price closes back below.\n"
    "- Displacement: strong impulsive move AFTER a sweep, leaving an imbalance (FVG). "
    "This confirms the sweep was engineered, not random.\n"
    "- Break of Structure (BOS): price closes beyond a prior swing high (bullish BOS) or "
    "swing low (bearish BOS). Confirms trend change or continuation.\n"
    "- Inducement: equal highs/lows just ahead of price — liquidity pool that will be hunted "
    "before the real move. Not the target, the trap before the target.\n"
    "- Order Block (OB): the last bearish candle before a bullish displacement, or the last "
    "bullish candle before a bearish displacement. Price returns to this for entries.\n"
    "- Fair Value Gap (FVG): a 3-candle imbalance where the middle candle has a gap between "
    "the first and third candle's wicks. Price fills these.\n"
    "- HTF bias: Daily > H4 > H1 > M15. Higher timeframe bias determines direction. "
    "Only take trades IN the direction of HTF bias.\n"
    "- Kill zones: London (08:00-11:00 UTC) and New York (13:00-16:00 UTC) sessions are "
    "when the highest quality setups form.\n"
    "- Regime: Expansion (trending), Compression (ranging), Exhaustion (reversal pending).\n\n"

    "WHEN EXPLAINING A SETUP: state the bias, what liquidity was swept, whether displacement "
    "confirmed, where the OB or FVG is for entry, what invalidates the setup.\n"
    "WHEN ASSESSING A MOVE: does it have sweep + displacement? Is it with or against HTF bias?\n"
    "WHEN ASSESSING MEXC CRIME: look for engineered moves — sudden spike with no displacement "
    "after, volume spike then immediate reversal, coordinated dumps across multiple low-caps.\n\n"

    "Be specific about price levels. Never say 'it could go up or down.' "
    "Give a directional read with reasoning. Under 150 words unless asked for more."
)


def _format_snapshot(snap: dict) -> str:
    t = snap.get("ticker") or {}
    f = snap.get("funding")
    lines = [
        f"Symbol: {snap.get('symbol')}",
        f"Price: {t.get('price')}  (24h change: {t.get('change_pct_24h')}%)",
        f"24h range: {t.get('low_24h')} - {t.get('high_24h')}",
        f"24h volume: {t.get('volume_24h')}",
        f"15m range position (0=at recent low, 1=at recent high): {snap.get('range_position_15m')}",
        f"15m volume vs recent average: {snap.get('volume_spike_15m')}x",
    ]
    if f:
        lines.append(f"Funding rate: {f.get('funding_rate')}")
    if "signal_strength" in snap:
        lines.append(f"Computed signal-strength score (0-100, deterministic, not from you): {snap['signal_strength']}")
    return "\n".join(lines)


def explain_symbol(symbol: str, user_question: str = "") -> str:
    """
    'Why is BTC rejecting?' / 'What's happening with SOL?' — fetch fresh
    data for one symbol and ask 0G Compute to explain it.
    """
    snap = market_data.snapshot(symbol)
    snap["signal_strength"] = market_data.signal_strength(snap)
    context = _format_snapshot(snap)

    question = user_question or f"What does this data suggest is happening with {symbol} right now, and why?"
    try:
        return ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Current data:\n{context}\n\nQuestion: {question}"},
        ])
    except ZGComputeError as e:
        log.error(f"explain_symbol({symbol}) failed: {e}")
        return f"⚠️ Couldn't reach 0G Compute right now ({e}). Raw data:\n{context}"


def best_opportunities(symbols: list = None, top_n: int = 3) -> str:
    """
    'What are the best setups right now?' — snapshot the watchlist, sort
    by the deterministic signal-strength score, ask 0G Compute to explain
    the top N in trader-readable language.
    """
    snaps = market_data.watchlist_snapshot(symbols)
    snaps = [s for s in snaps if s.get("ticker")]
    snaps.sort(key=lambda s: s.get("signal_strength", 0), reverse=True)
    top = snaps[:top_n]

    if not top:
        return "No market data available right now — try again in a moment."

    blocks = "\n\n".join(_format_snapshot(s) for s in top)
    question = (
        f"Here is data for the {len(top)} symbols with the highest computed "
        f"signal-strength score from a larger watchlist. For each one, explain "
        f"in 1-2 sentences what's notable about it and why it scored highly. "
        f"Order your answer to match the order given."
    )
    try:
        explanation = ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{blocks}\n\n{question}"},
        ], max_tokens=400)
    except ZGComputeError as e:
        log.error(f"best_opportunities failed: {e}")
        explanation = f"⚠️ Couldn't reach 0G Compute right now ({e})."

    header = "\n".join(
        f"{i+1}. {s['symbol']} — signal strength {s.get('signal_strength', 0)}/100"
        for i, s in enumerate(top)
    )
    return f"{header}\n\n{explanation}"


def chat(message: str, recent_context: list = None) -> str:
    """
    General chat — used for anything that isn't a direct 'explain X' or
    'best opportunities' command. recent_context is an optional list of
    prior {"role","content"} turns for short-term continuity.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if recent_context:
        messages.extend(recent_context[-6:])  # keep it small
    messages.append({"role": "user", "content": message})
    try:
        return ask(messages, max_tokens=400)
    except ZGComputeError as e:
        log.error(f"chat failed: {e}")
        return f"⚠️ Couldn't reach 0G Compute right now ({e})."


# ── Lightweight intent routing — no slash commands required ──────────────
_KNOWN_SYMBOLS = {s.replace("USDT", ""): s for s in market_data.DEFAULT_WATCHLIST}
_OPPORTUNITY_WORDS = ("best", "opportunit", "setup", "what should i", "anything good")


def _find_symbol(text: str) -> str:
    upper = text.upper()
    for short, full in _KNOWN_SYMBOLS.items():
        if short in upper or full in upper:
            return full
    return ""


def route_message(text: str, recent_context: list = None) -> str:
    """
    Decide what a free-text message is asking for and dispatch to the
    right function. This is the only entry point telegram_bot.py needs.
    """
    symbol = _find_symbol(text)
    if symbol:
        return explain_symbol(symbol, user_question=text)
    if any(w in text.lower() for w in _OPPORTUNITY_WORDS):
        return best_opportunities()
    return chat(text, recent_context=recent_context)


def explain_structure(symbol: str) -> str:
    """
    '/bias BTC' — full SMC structure picture for one symbol:
    HTF bias, recent sweep, last BOS, inducement zones.
    """
    from app.market_data import get_klines
    from app.structure import full_structure_snapshot

    try:
        candles_h1    = get_klines(symbol, "1h",  50)
        candles_h4    = get_klines(symbol, "4h",  50)
        candles_daily = get_klines(symbol, "1d",  30)
        snap = full_structure_snapshot(symbol, candles_h1, candles_h4, candles_daily)
    except Exception as e:
        return f"Couldn't fetch structure data for {symbol}: {e}"

    bias       = snap.get("bias", "neutral")
    bias_data  = snap.get("bias_data", {})
    sweep      = snap.get("recent_sweep")
    bos        = snap.get("bos_h1", {})
    inducements= snap.get("inducement_zones", [])
    price      = snap.get("current_price", 0)

    context_lines = [
        f"Symbol: {symbol}",
        f"Current price: {price:.4f}",
        f"HTF bias: {bias.upper()}",
        f"Daily note: {bias_data.get('daily_note', '')}",
        f"H4 note: {bias_data.get('h4_note', '')}",
    ]
    if sweep:
        context_lines.append(f"Recent H1 sweep: {sweep.get('description', '')}")
    if bos.get("broken"):
        context_lines.append(f"Last H1 BOS: {bos.get('description', '')}")
    if inducements:
        zones = ", ".join(
            f"{z['price']:.4f} ({z['distance_pct']:+.1f}%)"
            for z in inducements[:3]
        )
        context_lines.append(f"Inducement zones: {zones}")

    context = "\n".join(context_lines)
    question = (
        "Based on this structure data, give the trader:\n"
        "1. The overall bias and why\n"
        "2. What the recent sweep or BOS means\n"
        "3. The most important inducement zones to watch\n"
        "4. What to look for to confirm a trade setup\n"
        "Keep it under 150 words."
    )
    try:
        return ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{context}\n\n{question}"},
        ], max_tokens=300)
    except ZGComputeError as e:
        return f"Structure data:\n{context}\n\n⚠️ 0G Compute unavailable: {e}"
