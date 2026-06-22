"""
reasoning.py — Scout's reasoning layer.

Architecture (updated):
  Market Data
  ↓
  Intelligence Engine (intelligence.py) — extracts FACTS
  ↓
  0G Compute — explains facts, evidence-based
  ↓
  Telegram response

The model no longer guesses. It receives structured intelligence and
explains what the data actually shows.

Evidence-Based Prompt Format:
  1. Market Condition
  2. Evidence
  3. Risks
  4. Opportunity
  5. Confidence
"""

import logging
from app import market_data
from app.zg_compute import ask, ZGComputeError
from app.intelligence import (
    build_intelligence,
    format_intelligence_for_model,
    rank_opportunities,
)

log = logging.getLogger("scout")

SYSTEM_PROMPT = (
    "You are Scout, an AI market intelligence companion for a professional crypto trader. "
    "You reason using Smart Money Concepts (SMC).\n\n"

    "CRITICAL RULE: Only use the supplied market intelligence data. "
    "Do not invent price levels, percentages, or conditions not in the data. "
    "Do not make assumptions. If a data point is missing, say so.\n\n"

    "RESPONSE FORMAT — always structure your answer as:\n"
    "1. MARKET CONDITION: What regime and bias the market is in right now\n"
    "2. EVIDENCE: Specific facts from the intelligence data (sweep, displacement, BOS, etc)\n"
    "3. RISKS: What could invalidate this read\n"
    "4. OPPORTUNITY: Is there a trade setup? Entry zone, target, invalidation level\n"
    "5. CONFIDENCE: Low/Medium/High — based on how many confluence factors are present\n\n"

    "KEY SMC CONCEPTS:\n"
    "- Liquidity sweep: price wicks into a prior swing H/L (stop hunt), then rejects. "
    "Bullish sweep = hunts lows then closes back above. "
    "Bearish sweep = hunts highs then closes back below.\n"
    "- Displacement: strong impulsive candle AFTER a sweep (body ≥55% of range). "
    "Confirms the sweep was engineered.\n"
    "- BOS: price closes beyond a prior swing. Confirms structure shift.\n"
    "- Order Block (OB): last opposing candle before displacement. Entry zone.\n"
    "- FVG: 3-candle imbalance. Price tends to fill these.\n"
    "- Inducement: equal H/L just ahead of price — the trap before the real move.\n"
    "- Kill zones: London (08:00–11:00 UTC) and New York (13:00–16:00 UTC).\n"
    "- Regime: Expansion (trending), Compression (ranging), Exhaustion (reversal pending).\n\n"

    "Be specific about price levels from the data. Under 200 words unless asked for more. "
    "Never say 'it could go up or down' without giving a directional lean with reasoning."
)


def explain_symbol(symbol: str, user_question: str = "") -> str:
    """
    Fetch full intelligence for a symbol and ask 0G Compute to explain it.
    This is the primary path for 'What's happening with BTC?'
    """
    from app.market_data import get_klines
    from app.mexc_data import get_funding_rate as mexc_funding

    # Fetch candles at all TFs
    try:
        c_m15   = get_klines(symbol, "15m", 96)
        c_h1    = get_klines(symbol, "1h",  50)
        c_h4    = get_klines(symbol, "4h",  50)
        c_daily = get_klines(symbol, "1d",  30)
    except Exception as e:
        return f"⚠️ Couldn't fetch market data for {symbol}: {e}"

    # Funding
    funding_rate = 0.0
    try:
        fd = mexc_funding(symbol)
        if fd.get("ok"):
            funding_rate = fd["funding_rate"]
        else:
            # Binance fallback
            fd2 = market_data.get_funding_rate(symbol)
            funding_rate = fd2.get("funding_rate", 0.0)
    except Exception:
        pass

    # Build intelligence
    intel = build_intelligence(
        symbol        = symbol,
        candles_m15   = c_m15,
        candles_h1    = c_h1,
        candles_h4    = c_h4,
        candles_daily = c_daily,
        funding_rate  = funding_rate,
    )

    context  = format_intelligence_for_model(intel)
    question = user_question or (
        f"Based on this intelligence data, what is the current market condition "
        f"for {symbol.replace('USDT','')} and is there a trade setup forming?"
    )

    try:
        return ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"{context}\n\nQuestion: {question}"},
        ], max_tokens=400)
    except ZGComputeError as e:
        log.error(f"explain_symbol({symbol}) failed: {e}")
        return f"⚠️ Couldn't reach 0G Compute ({e}).\n\nRaw intelligence:\n{context}"


def explain_structure(symbol: str) -> str:
    """
    '/bias BTC' — full SMC structure picture with intelligence engine.
    """
    from app.market_data import get_klines
    from app.mexc_data import get_funding_rate as mexc_funding

    try:
        c_m15   = get_klines(symbol, "15m", 96)
        c_h1    = get_klines(symbol, "1h",  50)
        c_h4    = get_klines(symbol, "4h",  50)
        c_daily = get_klines(symbol, "1d",  30)
    except Exception as e:
        return f"⚠️ Couldn't fetch data for {symbol}: {e}"

    funding_rate = 0.0
    try:
        fd = mexc_funding(symbol)
        if fd.get("ok"):
            funding_rate = fd["funding_rate"]
    except Exception:
        pass

    intel   = build_intelligence(symbol, c_m15, c_h1, c_h4, c_daily, funding_rate)
    context = format_intelligence_for_model(intel)

    question = (
        "Based on this intelligence data, provide:\n"
        "1. The overall bias and regime — and why\n"
        "2. What the sweep/BOS evidence means for next move\n"
        "3. Key levels to watch (OB, FVG, inducement zones)\n"
        "4. What specifically confirms or invalidates a trade setup\n"
        "Keep it under 200 words."
    )

    try:
        return ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"{context}\n\n{question}"},
        ], max_tokens=400)
    except ZGComputeError as e:
        return f"Structure intelligence:\n{context}\n\n⚠️ 0G Compute unavailable: {e}"


def best_opportunities(symbols: list = None, top_n: int = 5) -> str:
    """
    Rank all watchlist symbols by opportunity score, then ask 0G Compute
    to explain the top setups. Uses the intelligence engine for proper scoring.
    """
    from app.market_data import get_klines, DEFAULT_WATCHLIST
    from app.mexc_data import get_funding_rate as mexc_funding

    symbols = symbols or DEFAULT_WATCHLIST

    # Build candle map for all symbols
    candles_map  = {}
    funding_map  = {}

    for sym in symbols:
        try:
            candles_map[sym] = {
                "m15":   get_klines(sym, "15m", 96),
                "h1":    get_klines(sym, "1h",  50),
                "h4":    get_klines(sym, "4h",  50),
                "daily": get_klines(sym, "1d",  30),
            }
        except Exception as e:
            log.debug(f"best_opportunities candles {sym}: {e}")

        try:
            fd = mexc_funding(sym)
            if fd.get("ok"):
                funding_map[sym] = fd["funding_rate"]
        except Exception:
            pass

    # Rank
    top = rank_opportunities(symbols, candles_map, funding_map, top_n=top_n)

    if not top:
        return (
            "No setups with confirmed structure across the watchlist right now.\n"
            "Scanner checks every 10 minutes — you'll get an alert when one forms."
        )

    # Build ranking header
    header_lines = ["<b>🏆 Opportunity Ranking</b>\n"]
    for i, intel in enumerate(top, 1):
        sym   = intel["symbol"].replace("USDT", "")
        score = intel["score"]
        bias  = intel["htf_bias"]
        regime = intel["regime"]

        # Score indicator
        if score >= 75:
            bar = "🟢"
        elif score >= 50:
            bar = "🟡"
        else:
            bar = "🔴"

        # One-line summary of key signals
        signals = []
        if intel.get("sweep", {}).get("detected"):
            signals.append("sweep")
        if intel.get("displacement", {}).get("confirmed"):
            signals.append("displacement")
        if intel.get("bos", {}).get("broken"):
            signals.append("BOS")
        sig_str = " + ".join(signals) if signals else "no structure yet"

        header_lines.append(
            f"{bar} {i}. <b>{sym}</b>  Score: {score}/100  "
            f"({bias} | {regime})\n"
            f"   <i>{sig_str}</i>"
        )

    header = "\n".join(header_lines)

    # Build context for 0G Compute — give it the intelligence for each
    blocks = []
    for intel in top:
        blocks.append(format_intelligence_for_model(intel))

    context  = "\n\n---\n\n".join(blocks)
    question = (
        "These are the top-ranked SMC opportunities right now, sorted by score. "
        "For each symbol, give a 2-sentence read: "
        "what the evidence shows and whether it's worth watching. "
        "Be specific about levels. Reference the intelligence data — do not guess."
    )

    try:
        explanation = ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"{context}\n\n{question}"},
        ], max_tokens=600)
        return f"{header}\n\n{explanation}"
    except ZGComputeError as e:
        return f"{header}\n\n⚠️ 0G Compute unavailable: {e}"


def explain_crime_move(mover: dict) -> str:
    """Ask 0G Compute to explain a suspicious MEXC mover."""
    try:
        sym   = mover["symbol"].replace("_USDT", "")
        chg   = mover["change_pct"]
        vol   = mover.get("volume_24h", 0)
        rvol  = mover.get("rvol", 1.0)
        vel   = mover.get("velocity", 1.0)
        rng   = mover.get("range_pct", 0)
        score = mover.get("crime_score", 0)

        # Build structured prompt — facts first
        context = (
            f"=== CRIME DETECTION: {sym}/USDT ===\n\n"
            f"24h Move:        {chg:+.1f}% ({mover.get('direction', 'UNKNOWN')})\n"
            f"Volume vs Normal: {rvol:.1f}x\n"
            f"Price Velocity:  {vel:.1f}x (recent vs prior period)\n"
            f"Daily Range:     {rng:.1f}% of price\n"
            f"Crime Score:     {score:.0f}\n"
            f"Volume 24h:      {vol:,.0f}\n"
        )

        prompt = (
            f"{context}\n\n"
            f"Analyze this suspicious move. Answer:\n"
            f"1. Does this look like coordinated manipulation or organic? Why?\n"
            f"2. What pattern does this match (pump-and-dump, stop hunt, whale accumulation)?\n"
            f"3. What should a trader watch for next — continuation, reversal, or trap?\n"
            f"Keep it under 120 words. Be direct."
        )

        return ask([
            {"role": "system", "content":
             "You are Scout, a market intelligence companion. Analyze market crime patterns. "
             "Only use the data provided. Be direct — if it looks like manipulation, say so."},
            {"role": "user", "content": prompt},
        ], max_tokens=180)
    except Exception:
        return ""


def chat(message: str, recent_context: list = None) -> str:
    """
    General chat — free-form questions. Tries to extract relevant intelligence
    if a symbol is mentioned, otherwise uses pure model reasoning.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if recent_context:
        messages.extend(recent_context[-6:])
    messages.append({"role": "user", "content": message})
    try:
        return ask(messages, max_tokens=400)
    except ZGComputeError as e:
        log.error(f"chat failed: {e}")
        return f"⚠️ Couldn't reach 0G Compute right now ({e})."


# ── Intent routing ────────────────────────────────────────────────────────
_KNOWN_SYMBOLS = {s.replace("USDT", ""): s for s in market_data.DEFAULT_WATCHLIST}
_OPPORTUNITY_WORDS = ("best", "opportunit", "setup", "what should i", "anything good",
                      "rank", "top setup", "what's hot", "what is hot")


def _find_symbol(text: str) -> str:
    upper = text.upper()
    for short, full in _KNOWN_SYMBOLS.items():
        if short in upper or full in upper:
            return full
    return ""


def route_message(text: str, recent_context: list = None) -> str:
    """
    Route free-text to the right function.
    """
    symbol = _find_symbol(text)
    if symbol:
        return explain_symbol(symbol, user_question=text)
    if any(w in text.lower() for w in _OPPORTUNITY_WORDS):
        return best_opportunities()
    return chat(text, recent_context=recent_context)
