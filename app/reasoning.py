"""
reasoning.py — Scout's reasoning and intent routing layer.

Scout Master Roadmap implementations:
  P2:  Monitoring ≠ Analysis — separate code paths, never conflated
  P3:  Full trade setups — every analysis output includes entry/SL/TP1/TP2/RR/confluence
  P8:  Trend continuation vs trend change classification in text output
  P12: Proactive opportunity discovery in route_message
  P13: Auto setup generation — best_opportunities() returns ranked setups with full plans
  P14: Rich alert quality — structured output, no bare event names

The model only explains what the intelligence engine has already proven.
It does NOT invent price levels, conditions, or directional bias.
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

# ── System prompt ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are Scout, a personal SMC trading assistant for a professional crypto trader. "
    "You reason exclusively using Smart Money Concepts (SMC).\n\n"

    "ABSOLUTE RULE: Only use supplied market intelligence data. "
    "NEVER invent price levels, percentages, regimes, or conditions not in the data. "
    "If data is missing, say so explicitly.\n\n"

    "TRADE SETUP FORMAT — every analysis must include:\n"
    "Bias: [bullish/bearish]\n"
    "Entry: [price zone low – high]\n"
    "SL: [level]\n"
    "TP1: [level]\n"
    "TP2: [level]\n"
    "RR: 1:[ratio]\n"
    "Confluence:\n"
    "  ✓ [specific evidence from data]\n"
    "  ✓ [specific evidence from data]\n"
    "Confidence: [Low/Medium/High]\n\n"

    "If no setup exists: state the bias, what's missing, and what condition "
    "would trigger a valid setup.\n\n"

    "CLASSIFICATION — always label moves as:\n"
    "  Bullish/Bearish Continuation — same direction as bias\n"
    "  Trend Change — bias has shifted\n"
    "  Range — compression, no directional edge\n"
    "  No Trade — conditions not met\n\n"

    "SMC CONCEPTS:\n"
    "- Liquidity sweep: wicks into prior swing H/L then rejects\n"
    "- Displacement: impulsive candle after sweep (body ≥55% of range)\n"
    "- BOS: close beyond prior swing — confirms structure shift\n"
    "- Order Block (OB): last opposing candle before displacement — entry zone\n"
    "- FVG: 3-candle imbalance — price fills these\n"
    "- Inducement: equal H/L ahead of price — the trap before the real move\n"
    "- Kill zones: London 08:00–11:00 UTC, New York 13:00–16:00 UTC\n"
    "- Regime: Expansion (trending), Compression (ranging), Exhaustion (reversal)\n\n"

    "Be direct. Under 200 words unless asked for more. "
    "Never say 'it could go either way' without a directional lean and specific reasoning."
)

# Tighter prompt for setup generation — forces structured output
SETUP_SYSTEM_PROMPT = (
    "You are Scout. Generate structured trade setups from SMC intelligence data.\n\n"
    "For EACH symbol output EXACTLY this format:\n\n"
    "[SYMBOL] [DIRECTION]\n"
    "Bias: [bullish/bearish]\n"
    "Entry: [low] – [high]\n"
    "SL: [level]\n"
    "TP1: [level]  (RR 1:[x])\n"
    "TP2: [level]  (RR 1:[x])\n"
    "Confluence:\n"
    "  ✓ [fact from data]\n"
    "  ✓ [fact from data]\n"
    "Confidence: [Low/Medium/High]\n\n"
    "RULES:\n"
    "- Only use levels from the intelligence data\n"
    "- No invented numbers\n"
    "- If no setup: write 'No Setup — [reason]'\n"
    "- No commentary outside the format\n"
    "- Classify as: Continuation / Trend Change / Range / No Trade"
)


# ── Core intelligence fetch helper ────────────────────────────────────────

def _fetch_intel(symbol: str) -> dict:
    """Fetch full multi-TF intelligence for a symbol."""
    from app.market_data import get_klines
    from app.mexc_data import get_funding_rate as mexc_funding

    c_m15   = get_klines(symbol, "15m", 96)
    c_h1    = get_klines(symbol, "1h",  50)
    c_h4    = get_klines(symbol, "4h",  50)
    c_daily = get_klines(symbol, "1d",  30)

    funding_rate = 0.0
    try:
        fd = mexc_funding(symbol)
        if fd.get("ok"):
            funding_rate = fd["funding_rate"]
        else:
            fd2 = market_data.get_funding_rate(symbol)
            funding_rate = fd2.get("funding_rate", 0.0)
    except Exception:
        pass

    return build_intelligence(
        symbol        = symbol,
        candles_m15   = c_m15,
        candles_h1    = c_h1,
        candles_h4    = c_h4,
        candles_daily = c_daily,
        funding_rate  = funding_rate,
    )


# ── explain_symbol — P3: always includes full trade setup ─────────────────

def explain_symbol(symbol: str, user_question: str = "") -> str:
    """
    Fetch full intelligence for a symbol and produce a complete SMC analysis.
    P3: output always includes entry / SL / TP1 / TP2 / RR / confluence.
    P8: classifies as continuation or trend change, not bare events.
    """
    try:
        intel = _fetch_intel(symbol)
    except Exception as e:
        return f"⚠️ Couldn't fetch market data for {symbol}: {e}"

    # Attach deterministic trade plan levels (P4)
    plan_context = ""
    try:
        from app.trade_plan import generate_trade_plan, format_trade_plan
        plan = generate_trade_plan(intel)
        if not plan.get("error"):
            plan_context = f"\n\nDETERMINISTIC TRADE PLAN (from market structure):\n{format_trade_plan(plan)}"
    except Exception:
        pass

    context  = format_intelligence_for_model(intel) + plan_context
    question = user_question or (
        f"Provide a complete SMC analysis for {symbol.replace('USDT','')}. "
        f"State the bias, classify the move (continuation/trend change/range/no trade), "
        f"and give a full trade setup with entry, SL, TP1, TP2, and RR. "
        f"Use only the intelligence data — no invented levels."
    )

    try:
        return ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"{context}\n\nQuestion: {question}"},
        ], max_tokens=450)
    except ZGComputeError as e:
        log.error(f"explain_symbol({symbol}) failed: {e}")
        return f"⚠️ Couldn't reach 0G Compute ({e}).\n\nRaw intelligence:\n{context}"


# ── explain_structure — /bias command ─────────────────────────────────────

def explain_structure(symbol: str) -> str:
    """
    /bias command — full SMC structure picture.
    P3/P8: includes classification + full trade setup output.
    """
    try:
        intel = _fetch_intel(symbol)
    except Exception as e:
        return f"⚠️ Couldn't fetch data for {symbol}: {e}"

    # Attach deterministic plan
    plan_section = ""
    try:
        from app.trade_plan import generate_trade_plan, format_trade_plan
        plan = generate_trade_plan(intel)
        if not plan.get("error"):
            plan_section = (
                f"\n\nDETERMINISTIC PLAN (derived from structure):\n"
                f"{format_trade_plan(plan)}"
            )
    except Exception:
        pass

    context  = format_intelligence_for_model(intel) + plan_section
    question = (
        "Provide a complete SMC structure analysis:\n"
        "1. Overall bias and regime classification\n"
        "   (Bullish/Bearish Trend, Continuation, Trend Change, Range, No Trade)\n"
        "2. What the sweep/displacement/BOS evidence means for next move\n"
        "3. Key levels: OB zone, FVG, inducement\n"
        "4. Full trade setup (entry zone, SL, TP1, TP2, RR, confluence)\n"
        "5. What specifically confirms or invalidates the setup\n"
        "Use only data provided. Under 250 words."
    )

    try:
        return ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"{context}\n\n{question}"},
        ], max_tokens=500)
    except ZGComputeError as e:
        return f"Structure intelligence:\n{context}\n\n⚠️ 0G Compute unavailable: {e}"


# ── best_opportunities — P13: auto-ranked setups with full plans ──────────

def _get_mexc_mover_symbols(min_change: float = 5.0, max_symbols: int = 20) -> list:
    """
    Pull current MEXC top movers and return their symbols for setup scanning.
    Filters out stocks/forex/commodities. Only returns crypto perps with
    a meaningful move so we focus on active momentum.
    """
    try:
        import requests
        _NON_CRYPTO = {
            "stock", "xau", "xag", "usd_", "eur", "gbp", "jpy", "aud",
            "chf", "cad", "nzd", "oil", "gas", "corn", "wheat", "sp500",
            "nasdaq", "dow", "gold", "silver",
        }
        resp = requests.get(
            "https://contract.mexc.com/api/v1/contract/ticker", timeout=15
        )
        resp.raise_for_status()
        raw = resp.json().get("data", [])

        movers = []
        for t in raw:
            sym = t.get("symbol", "")
            if not sym.endswith("_USDT"):
                continue
            if any(s in sym.lower() for s in _NON_CRYPTO):
                continue
            try:
                last  = float(t.get("lastPrice", 0))
                open_ = float(t.get("open24h", t.get("openPrice", 0)))
                if last > 0 and open_ > 0:
                    chg = abs((last - open_) / open_ * 100)
                else:
                    raw_pct = t.get("priceChangePercent", t.get("changeRate", 0))
                    chg = abs(float(raw_pct or 0))
                    if chg < 2.0:
                        chg *= 100
                if chg >= min_change:
                    movers.append((sym.replace("_USDT", "") + "USDT", chg))
            except (ValueError, TypeError):
                continue

        # Sort by magnitude, take top N
        movers.sort(key=lambda x: x[1], reverse=True)
        return [m[0] for m in movers[:max_symbols]]
    except Exception as e:
        log.debug(f"_get_mexc_mover_symbols failed: {e}")
        return []


# Grade thresholds
GRADE_A_PLUS = 78
GRADE_A      = 62


def best_opportunities(symbols: list = None, top_n: int = 5) -> str:
    """
    P13: Scan majors + MEXC movers for A/A+ setups.
    - A+: score ≥ 78 — full trade plan
    - A:  score ≥ 62 — trade plan
    - Below A: omitted from output

    Always includes MEXC top movers (≥5% move) alongside the default watchlist
    so we catch active momentum names, not just majors.
    """
    from app.market_data import get_klines, DEFAULT_WATCHLIST
    from app.mexc_data import get_funding_rate as mexc_funding
    from app.trade_plan import generate_trade_plan, format_trade_plan

    # Combine default watchlist + live MEXC movers
    base_symbols  = symbols or DEFAULT_WATCHLIST
    mover_symbols = _get_mexc_mover_symbols(min_change=5.0, max_symbols=20)

    # Deduplicate, movers first (they're active momentum names)
    all_symbols = list(dict.fromkeys(mover_symbols + base_symbols))

    candles_map = {}
    funding_map = {}

    for sym in all_symbols:
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

    top = rank_opportunities(all_symbols, candles_map, funding_map, top_n=top_n)

    if not top:
        return (
            "No A/A+ setups found right now across majors + MEXC movers.\n\n"
            "Scout scanned the full market including active movers. "
            "Nothing cleared the minimum confluence threshold.\n"
            "You'll get an alert automatically when a setup forms."
        )

    # ── Grade each result ─────────────────────────────────────────────────
    graded = []
    for intel in top:
        score = intel["score"]
        if score >= GRADE_A_PLUS:
            grade = "A+"
        elif score >= GRADE_A:
            grade = "A"
        else:
            grade = "B"
        graded.append((grade, intel))

    a_plus = [(g, i) for g, i in graded if g == "A+"]
    a_only = [(g, i) for g, i in graded if g == "A"]
    b_only = [(g, i) for g, i in graded if g == "B"]

    # ── Header ────────────────────────────────────────────────────────────
    total_scanned = len(all_symbols)
    header_lines  = [
        f"<b>🏆 Setup Ranking</b>",
        f"<i>Scanned {total_scanned} symbols — majors + MEXC movers</i>\n",
    ]

    for i, (grade, intel) in enumerate(graded, 1):
        sym    = intel["symbol"].replace("USDT", "")
        score  = intel["score"]
        bias   = intel["htf_bias"]
        regime = intel["regime"]
        icon   = "🟢" if grade == "A+" else "🟡" if grade == "A" else "⚪"

        sigs = []
        if intel.get("sweep", {}).get("detected"):    sigs.append("sweep")
        if intel.get("displacement", {}).get("confirmed"): sigs.append("disp")
        if intel.get("bos", {}).get("broken"):        sigs.append("BOS")
        if intel.get("order_block", {}).get("found"): sigs.append("OB")
        if intel.get("fvg", {}).get("found"):         sigs.append("FVG")
        sig_str = " + ".join(sigs) if sigs else "partial"

        header_lines.append(
            f"{icon} {i}. <b>{sym}</b>  [{grade}]  {score}/100\n"
            f"   {bias} | {regime} | {sig_str}"
        )

    header = "\n".join(header_lines)

    # ── Full trade plans — A+ and A only ─────────────────────────────────
    plan_blocks = []
    for grade, intel in (a_plus + a_only):
        sym_short = intel["symbol"].replace("USDT", "")
        plan      = generate_trade_plan(intel)
        if plan.get("error"):
            plan_blocks.append(
                f"<b>{sym_short} [{grade}]</b>: Structure confirmed — "
                f"no clean entry zone yet. {plan['error']}"
            )
        else:
            plan_blocks.append(
                f"— — — — — — — — — —\n"
                f"{format_trade_plan(plan)}"
            )

    if b_only:
        plan_blocks.append(
            f"\n<i>⚪ {len(b_only)} B-grade setup(s) below A threshold — "
            f"monitoring continues.</i>"
        )

    plans_text = "\n\n".join(plan_blocks) if plan_blocks else ""

    # ── 0G Compute explanation — A/A+ only ───────────────────────────────
    top_intel    = [i for _, i in (a_plus + a_only)]
    intel_blocks = [format_intelligence_for_model(i) for i in top_intel]
    context      = "\n\n---\n\n".join(intel_blocks) if intel_blocks else ""

    if context:
        question = (
            "These are A and A+ grade SMC setups from majors and MEXC movers. "
            "For each symbol write ONE sentence: what specific confluence "
            "makes this actionable right now. "
            "Reference only supplied data. No invented levels. No filler."
        )
        try:
            explanation = ask([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": f"{context}\n\n{question}"},
            ], max_tokens=350)
            return f"{header}\n\n{plans_text}\n\n{explanation}"
        except ZGComputeError as e:
            return f"{header}\n\n{plans_text}\n\n⚠️ 0G Compute unavailable: {e}"

    return f"{header}\n\n{plans_text}" if plans_text else header


# ── explain_crime_move — P14: rich context on MEXC movers ────────────────

def explain_crime_move(mover: dict) -> str:
    """
    Ask 0G Compute to explain a suspicious MEXC mover.
    P14: output is actionable — continuation/reversal/trap classification.
    """
    try:
        sym   = mover["symbol"].replace("_USDT", "")
        chg   = mover["change_pct"]
        rvol  = mover.get("rvol", 1.0)
        vel   = mover.get("velocity", 1.0)
        rng   = mover.get("range_pct", 0)
        score = mover.get("crime_score", 0)
        vol   = mover.get("volume_24h", 0)

        context = (
            f"=== MEXC MOVER ANALYSIS: {sym}/USDT ===\n\n"
            f"24h Move:         {chg:+.1f}% ({mover.get('direction','UNKNOWN')})\n"
            f"Volume vs Normal: {rvol:.1f}x\n"
            f"Price Velocity:   {vel:.1f}x\n"
            f"Daily Range:      {rng:.1f}%\n"
            f"Crime Score:      {score:.0f}\n"
            f"Volume 24h:       {vol:,.0f}\n"
        )

        prompt = (
            f"{context}\n\n"
            f"Classify this move and tell the trader what to do:\n"
            f"1. Coordinated manipulation or organic move?\n"
            f"2. Pattern: pump-and-dump / stop hunt / whale accumulation / organic breakout?\n"
            f"3. Trend continuation or exhaustion/reversal risk?\n"
            f"4. What should the trader watch for next?\n"
            f"Under 100 words. Direct."
        )

        return ask([
            {"role": "system", "content":
             "You are Scout, a market intelligence assistant. Analyze MEXC perpetual futures moves. "
             "Only use provided data. Be specific about whether to trade or avoid."},
            {"role": "user", "content": prompt},
        ], max_tokens=180)
    except Exception:
        return ""


# ── generate_setups_response — P13 structured output ─────────────────────

def generate_setups_response(symbols: list = None) -> str:
    """
    P13: generate ranked, structured setup output via 0G Compute.
    This is the companion to best_opportunities() — uses SETUP_SYSTEM_PROMPT
    which forces the model into a strict entry/SL/TP format.
    """
    from app.market_data import get_klines, DEFAULT_WATCHLIST
    from app.mexc_data import get_funding_rate as mexc_funding

    symbols     = symbols or DEFAULT_WATCHLIST[:8]
    candles_map = {}
    funding_map = {}

    for sym in symbols:
        try:
            candles_map[sym] = {
                "m15":   get_klines(sym, "15m", 96),
                "h1":    get_klines(sym, "1h",  50),
                "h4":    get_klines(sym, "4h",  50),
                "daily": get_klines(sym, "1d",  30),
            }
            fd = mexc_funding(sym)
            if fd.get("ok"):
                funding_map[sym] = fd["funding_rate"]
        except Exception:
            pass

    top = rank_opportunities(symbols, candles_map, funding_map, top_n=3)
    if not top:
        return "No ranked setups available at this time."

    intel_blocks = [format_intelligence_for_model(i) for i in top]
    context = "\n\n---\n\n".join(intel_blocks)

    prompt = (
        f"Here is market intelligence for the top {len(top)} ranked symbols.\n\n"
        f"{context}\n\n"
        f"Generate a full trade setup for each. "
        f"Use only the levels in the data. "
        f"If no setup exists for a symbol, write 'No Setup — [reason]'."
    )

    try:
        return ask([
            {"role": "system", "content": SETUP_SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ], max_tokens=700)
    except ZGComputeError as e:
        return f"⚠️ 0G Compute unavailable: {e}"


# ── chat — general free-form questions ───────────────────────────────────

def chat(message: str, recent_context: list = None) -> str:
    """General chat — no symbol detected, no opportunity keywords."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if recent_context:
        messages.extend(recent_context[-6:])
    messages.append({"role": "user", "content": message})
    try:
        return ask(messages, max_tokens=400)
    except ZGComputeError as e:
        log.error(f"chat failed: {e}")
        return f"⚠️ Couldn't reach 0G Compute right now ({e})."


# ── Intent routing — P2: monitoring ≠ analysis ───────────────────────────

_KNOWN_SYMBOLS = {s.replace("USDT", ""): s for s in market_data.DEFAULT_WATCHLIST}

_OPPORTUNITY_WORDS = (
    "best", "setup", "setups", "opportunit", "what should i trade",
    "anything good", "rank", "top setup", "what's hot", "what is hot",
    "give me setups", "show me setups", "find setups", "any trades",
    "good trades", "best trades", "what to trade", "trade ideas",
)

_ANALYSIS_WORDS = (
    "what's happening", "what is happening", "what's going on",
    "analysis", "analyse", "analyze", "explain", "tell me about",
    "whats up with", "what about", "how is", "what do you think",
    "bias on", "structure on", "read on",
)

_MAJOR_WORDS = (
    "majors", "btc", "bitcoin", "eth", "ethereum", "sol", "solana",
    "market overview", "market snapshot", "all majors",
)


def _find_symbol(text: str) -> str:
    upper = text.upper()
    # Direct USDT match
    m = __import__("re").search(r'\b([A-Z]{2,10})USDT\b', upper)
    if m:
        sym = m.group(1) + "USDT"
        if sym in _KNOWN_SYMBOLS.values():
            return sym
    # Short name match
    for short, full in _KNOWN_SYMBOLS.items():
        if __import__("re").search(r'\b' + short + r'\b', upper):
            return full
    return ""


def route_message(text: str, recent_context: list = None, chat_id: int = None) -> str:
    """
    P2: strict separation between monitoring and analysis paths.

    Monitoring requests → handled in telegram_bot.py, never reach here.
    Analysis requests → explain_symbol / best_opportunities / chat.
    """
    text_lower = text.lower()

    # ── Symbol-specific analysis ──────────────────────────────────────────
    symbol = _find_symbol(text)
    if symbol:
        return explain_symbol(symbol, user_question=text)

    # ── Opportunity / setup discovery (P12/P13) ───────────────────────────
    if any(w in text_lower for w in _OPPORTUNITY_WORDS):
        return best_opportunities()

    # ── Market overview request ───────────────────────────────────────────
    if any(w in text_lower for w in _MAJOR_WORDS):
        try:
            from app.scanner import get_major_summary
            summary = get_major_summary()
            if "not yet loaded" not in summary:
                return summary
        except Exception:
            pass
        return best_opportunities()

    # ── General analysis ask ──────────────────────────────────────────────
    if any(w in text_lower for w in _ANALYSIS_WORDS):
        return chat(text, recent_context=recent_context)

    # ── Default: general chat ─────────────────────────────────────────────
    return chat(text, recent_context=recent_context)
