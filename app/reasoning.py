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
    "You are Scout, a market intelligence assistant for crypto traders. "
    "You are given real, current market data (price, 24h change, volume, "
    "recent range position, funding rate, and a computed signal-strength "
    "score). Explain what the data shows in plain language. Be specific "
    "about which numbers support your read. Never claim certainty about "
    "future price — describe conditions and possibilities, not predictions. "
    "If the data is thin or ambiguous, say so plainly instead of inventing "
    "a confident-sounding answer. Keep answers under 120 words unless asked "
    "for more detail."
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
