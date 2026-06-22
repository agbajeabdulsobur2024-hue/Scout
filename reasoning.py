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
    "You are Scout, an AI market intelligence companion. "
    "You ONLY reason using the specific market data and structure information provided to you. "
    "NEVER give generic trading theory, NEVER explain what SMC concepts are, "
    "NEVER say 'here is how you might approach it' or give step-by-step frameworks. "
    "If data is provided — use it. Give a direct, specific answer about THAT data. "
    "If no data is provided — say so plainly and stop. "
    "Your SMC framework (use it silently, don't explain it): "
    "Sweeps hunt stop losses at swing highs/lows. Displacement = strong impulse after sweep confirming it was engineered. "
    "BOS = close beyond prior swing with 0.15% minimum, confirms trend. "
    "OB = last opposing candle before displacement = entry zone. "
    "Inducement = equal highs/lows ahead of price = next liquidity target. "
    "HTF bias: Daily > H4 > H1 > M15. Only take trades in direction of HTF bias. "
    "Funding: positive = longs pay = bearish pressure at settlement. Negative = shorts pay = squeeze risk. "
    "Be direct, specific, under 120 words. Name price levels. Give a directional read."
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
    Explain what's happening with one symbol.
    Tries Binance first, falls back to MEXC for futures-only tokens.
    """
    snap = None

    # Try Binance first
    try:
        snap = market_data.snapshot(symbol)
        snap["signal_strength"] = market_data.signal_strength(snap)
        if not snap.get("ticker"):
            snap = None
    except Exception:
        snap = None

    # Fall back to MEXC for tokens not on Binance (HYPE, ASTEROID, etc.)
    if snap is None or not snap.get("ticker"):
        try:
            import requests as _req
            # Normalise: HYPEUSDT → HYPE_USDT for MEXC
            mexc_sym = symbol.replace("USDT", "_USDT")
            resp = _req.get(
                f"https://contract.mexc.com/api/v1/contract/ticker?symbol={mexc_sym}",
                timeout=10
            )
            d = resp.json().get("data", {})
            if d:
                chg = round(float(d.get("riseFallRate") or 0) * 100, 2)
                price = float(d.get("lastPrice") or 0)
                high  = float(d.get("high24Price") or 0)
                low   = float(d.get("lower24Price") or 0)
                vol   = float(d.get("volume24") or 0)
                fund  = float(d.get("fundingRate") or 0)

                context = (
                    f"Symbol: {symbol} (MEXC Futures)\n"
                    f"Price: {price}\n"
                    f"24h change: {chg:+.2f}%\n"
                    f"24h high: {high}  low: {low}\n"
                    f"Volume 24h: {vol:,.0f}\n"
                    f"Funding rate: {fund*100:+.4f}%"
                )
                question = user_question or f"What is happening with {symbol} right now and is it worth watching?"
                try:
                    return ask([
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user",   "content": f"MEXC Futures data:\n{context}\n\nQuestion: {question}"},
                    ], max_tokens=150)
                except ZGComputeError as e:
                    return f"MEXC data for {symbol}:\n{context}\n\n⚠️ 0G Compute unavailable: {e}"
        except Exception as e:
            log.debug(f"MEXC fallback for {symbol}: {e}")
        return f"No data found for {symbol} on Binance or MEXC futures. Check the symbol name."

    context = _format_snapshot(snap)
    question = user_question or f"What does this data suggest is happening with {symbol} right now, and why?"
    try:
        return ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Current data:\n{context}\n\nQuestion: {question}"},
        ])
    except ZGComputeError as e:
        log.error(f"explain_symbol({symbol}) failed: {e}")
        return f"⚠️ Couldn't reach 0G Compute right now. Raw data:\n{context}"


def best_opportunities(symbols: list = None, top_n: int = 3) -> str:
    """
    SMC-based opportunity scan — replaces the old volume/range signal strength.
    Scans the watchlist for symbols with confirmed sweep + displacement,
    ranks by HTF bias alignment and displacement quality.
    0G Compute explains the top setups.
    """
    from app.market_data import get_klines
    from app.structure import full_structure_snapshot

    symbols = symbols or market_data.DEFAULT_WATCHLIST
    results = []

    for symbol in symbols:
        try:
            c1h = get_klines(symbol, "1h", 50)
            c4h = get_klines(symbol, "4h", 50)
            cd  = get_klines(symbol, "1d", 30)
            snap = full_structure_snapshot(symbol, c1h, c4h, cd)

            sweep = snap.get("recent_sweep")
            disp  = snap.get("displacement", {})
            bos   = snap.get("bos_h1", {})
            bias  = snap.get("bias", "neutral")
            indu  = snap.get("inducement_zones", [])
            price = snap.get("current_price", 0)

            # Score: sweep + displacement = strongest, BOS alone = moderate
            score = 0
            if sweep and sweep.get("entry_valid"):
                score += 40
                if disp.get("confirmed"):
                    score += 40
                if sweep.get("has_rejection"):
                    score += 10
                if sweep.get("is_equal_hl"):
                    score += 10
            elif bos.get("broken"):
                score += 30

            # Bias alignment bonus
            if bias != "neutral":
                score += 10

            if score > 0:
                results.append({
                    "symbol": symbol,
                    "score":  score,
                    "bias":   bias,
                    "sweep":  sweep,
                    "disp":   disp,
                    "bos":    bos,
                    "indu":   indu,
                    "price":  price,
                    "snap":   snap,
                })
        except Exception as e:
            log.debug(f"best_opportunities {symbol}: {e}")

    if not results:
        return (
            "No confirmed SMC setups across the watchlist right now.\n"
            "Scanner checks every 10 minutes — you'll get an alert when one forms."
        )

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:top_n]

    # Build context for 0G Compute
    blocks = []
    for r in top:
        sym   = r["symbol"].replace("USDT", "")
        bias  = r["bias"].upper()
        sweep = r["sweep"]
        disp  = r["disp"]
        bos   = r["bos"]
        indu  = r["indu"]

        lines = [f"Symbol: {sym}  |  Bias: {bias}  |  Price: {r['price']:.4f}"]
        if sweep:
            lines.append(f"Sweep: {sweep.get('description', '')}")
        if disp.get("confirmed"):
            lines.append(f"Displacement: {disp.get('description', '')}")
        if bos.get("broken"):
            lines.append(f"BOS: {bos.get('description', '')}")
        if indu:
            lines.append(f"Next inducement: {indu[0]['price']:.4f} ({indu[0]['distance_pct']:+.1f}%)")
        blocks.append("\n".join(lines))

    context   = "\n\n".join(blocks)
    header    = "\n".join(
        f"{i+1}. {r['symbol'].replace('USDT','')}  — setup score {r['score']}/100  ({r['bias'].upper()} bias)"
        for i, r in enumerate(top)
    )
    question  = (
        "These are the highest-scoring SMC setups right now based on sweep, "
        "displacement, and bias alignment. For each one, give a 1-2 sentence "
        "read: is this worth watching, what confirms it, what invalidates it."
    )

    try:
        explanation = ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"{context}\n\n{question}"},
        ], max_tokens=350)
        return f"{header}\n\n{explanation}"
    except ZGComputeError as e:
        return f"{header}\n\n⚠️ 0G Compute unavailable: {e}"


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
# Add popular MEXC futures tokens not on Binance
_MEXC_ONLY = [
    "HYPE", "ASTEROID", "HEI", "RE", "BSB", "GUA", "ESPORTS",
    "MXSOL", "LEAD", "ZINC", "BEAT", "LAB", "ALICE", "BTW",
]
for sym in _MEXC_ONLY:
    _KNOWN_SYMBOLS[sym] = sym + "USDT"
_OPPORTUNITY_WORDS = ("best setup", "what should i trade", "anything good",
                      "which setup", "what to trade", "get in", "entry now",
                      "best opportunit", "good trade")

_MEXC_WORDS = ("mexc", "futures token", "which token", "crime",
               "what's pumping", "what's dumping", "what is pumping",
               "what is moving", "what's moving", "movers")

_SKIP_WORDS = {"can", "you", "me", "and", "for", "when", "the", "get", "all",
               "now", "this", "that", "with", "from", "any", "new", "goes",
               "is", "are", "what", "why", "how", "does", "will", "should",
               "analyze", "analyse", "check", "show", "tell", "give", "look",
               "at", "on", "in", "of", "to", "a", "an", "about", "its",
               "monitor", "watch", "alert", "track", "scan", "token", "coin",
               "trade", "trading", "price", "market", "crypto"}


def _extract_symbol(text: str) -> str:
    """Extract a crypto symbol from free text — works for ANY symbol including HYPE, ASTEROID."""
    import re as _re
    upper = text.upper()

    # First: known watchlist symbols (exact word boundary match)
    for short, full in _KNOWN_SYMBOLS.items():
        if _re.search(rf'\\b{short}\\b', upper):
            return full

    # Second: any word that looks like a ticker
    words = _re.findall(r'\\b([A-Za-z]{2,12})\\b', text)
    for word in words:
        if word.lower() not in _SKIP_WORDS and len(word) <= 10:
            sym = word.upper()
            return sym + "USDT" if not sym.endswith("USDT") else sym
    return ""


def _quick_mexc_movers() -> str:
    """Fetch top 5 MEXC movers inline."""
    try:
        import requests as _req
        resp  = _req.get("https://contract.mexc.com/api/v1/contract/ticker", timeout=15)
        raw   = resp.json().get("data", [])
        tickers = []
        for t in raw:
            sym = t.get("symbol", "")
            if not sym.endswith("_USDT"):
                continue
            try:
                chg = round(float(t.get("riseFallRate") or 0) * 100, 2)
                tickers.append({"symbol": sym.replace("_USDT", ""), "change_pct": chg})
            except Exception:
                continue
        if not tickers:
            return "Couldn't reach MEXC right now — try /crimes for the full list."
        sorted_t = sorted(tickers, key=lambda x: x["change_pct"])
        losers   = sorted_t[:5]
        gainers  = sorted_t[-5:][::-1]
        lines    = ["<b>MEXC Futures top movers right now:</b>\n", "📈 <b>Gainers</b>"]
        for m in gainers:
            lines.append(f"  {m['symbol']}  {m['change_pct']:+.2f}%")
        lines.append("\n📉 <b>Losers</b>")
        for m in losers:
            lines.append(f"  {m['symbol']}  {m['change_pct']:+.2f}%")
        lines.append("\nUse /crimes for the full top 20.")
        return "\n".join(lines)
    except Exception as e:
        return f"MEXC fetch failed: {e}. Try /crimes."


def route_message(text: str, recent_context: list = None) -> str:
    """Route free-text to the right handler. Always attach real data to 0G."""
    text_lower = text.lower()

    # MEXC queries → top movers
    if any(w in text_lower for w in _MEXC_WORDS):
        return _quick_mexc_movers()

    # Setup/opportunity queries → SMC scan
    if any(w in text_lower for w in _OPPORTUNITY_WORDS):
        return best_opportunities()

    # Any symbol mentioned → fetch real data and explain it
    symbol = _extract_symbol(text)
    if symbol:
        return explain_symbol(symbol, user_question=text)

    # General question → 0G chat (no data to attach, accept it)
    return chat(text, recent_context=recent_context)



def explain_crime_move(mover: dict) -> str:
    """Ask 0G Compute to explain the most extreme MEXC mover."""
    try:
        sym = mover["symbol"].replace("_USDT", "")
        chg = mover["change_pct"]
        vol = mover.get("volume_24h", 0)
        prompt = (
            f"MEXC futures — {sym}/USDT moved {chg:+.1f}% in 24h "
            f"with volume {vol:,.0f}.\n\n"
            f"In 2-3 sentences: does this look coordinated or organic? "
            f"What should a trader watch for — continuation, reversal, or trap?"
        )
        return ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ], max_tokens=120)
    except Exception:
        return ""
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

    # ── Funding rate intelligence ─────────────────────────────────────────
    from app.market_data import get_funding_intelligence
    funding = get_funding_intelligence(symbol, bias=bias)

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
    if not funding.get("error"):
        context_lines.append(f"Funding rate: {funding.get('read', '')}")
        if funding.get("settlement_warning"):
            context_lines.append("⚠️ WARNING: Within 30 min of funding settlement — high volatility risk")

    # ── Displacement + OB ────────────────────────────────────────────────
    displacement = snap.get("displacement", {})
    order_block  = snap.get("order_block", {})
    if displacement.get("confirmed"):
        context_lines.append(f"Displacement: {displacement.get('description', '')}")
        if order_block.get("found"):
            context_lines.append(f"Order Block: {order_block.get('description', '')}")
            context_lines.append(
                f"OB zone: {order_block.get('low', 0):.4f} – {order_block.get('high', 0):.4f}"
            )
    else:
        context_lines.append(
            f"Displacement: {displacement.get('description', displacement.get('reason', 'not confirmed'))}"
        )

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
