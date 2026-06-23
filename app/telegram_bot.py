"""
telegram_bot.py — Scout's Telegram interface.

Implements Scout Master Roadmap:
  P1:  Direction-aware monitor parsing — direction extracted and stored with every condition
  P2:  Strict vs Assisted monitoring mode — default strict, assisted only when granted
  P3:  Full trade setups on analysis requests — entry, SL, TP1, TP2, RR, confluence
  P5:  Custom SMC workflow parsing — multi-condition sequences from natural language
  P6:  Multi-stage setup engine — via workflow.py integration
  P8:  Trend continuation vs change in /majors
  P9:  Major asset engine — /majors command
  P10/11: MEXC movers — /crimes shows gainers + losers with classifications
  P12: Market-wide opportunity discovery — /scan, /opportunities
  P13: Auto setup generation — /setups
  P14: Rich alert quality — full confluence + levels in every alert
"""

import os
import re
import time
import logging
import requests
from app import reasoning

log = logging.getLogger("scout")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_BASE  = f"https://api.telegram.org/bot{BOT_TOKEN}"

_chat_context: dict  = {}
_alert_chat_id: int  = int(os.environ.get("SCOUT_ALERT_CHAT_ID", "0"))

# ── Direction-aware monitor parsing (Priority 1) ──────────────────────────

_DIRECTION_WORDS = {
    "bullish": "bullish", "bearish": "bearish",
    "long":    "bullish", "short":   "bearish",
    "up":      "bullish", "down":    "bearish",
    "bull":    "bullish", "bear":    "bearish",
    "longs":   "bullish", "shorts":  "bearish",
}

_CONDITION_KEYWORDS = {
    "sweep":   ["sweep", "hunt", "liquidity", "stop hunt"],
    "bos":     ["bos", "break of structure", "break", "structure break"],
    "choch":   ["choch", "change of character", "character change"],
    "displacement": ["displacement", "disp", "impulse"],
    "order_block_retest": ["ob", "order block", "ob retest"],
    "fvg_fill": ["fvg", "fair value gap"],
}

_FUNDING_KEYWORDS = {
    "funding_negative": ["negative", "neg", "shorts pay", "below zero"],
    "funding_positive": ["positive", "pos", "longs pay", "above zero"],
    "funding_change":   ["funding", "fund", "rate"],
}

# Words that trigger a workflow (multi-step) parse
_WORKFLOW_TRIGGERS = [
    "then", "after", "wait for", "followed by",
    "1.", "2.", "step 1", "first", "second", "third",
]

# Words that indicate this is a monitoring request
_MONITOR_TRIGGERS = [
    "monitor", "watch", "alert", "track", "inform me",
    "tell me when", "notify me", "let me know", "keep watching",
]

# Words that cancel monitoring
_REMOVE_TRIGGERS = [
    "stop monitor", "remove", "unwatch", "stop watch",
    "stop tracking", "cancel monitor", "delete monitor",
]


def send_message(chat_id: int, text: str) -> None:
    if not chat_id:
        return
    try:
        requests.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        log.error(f"send_message failed: {e}")


def _extract_direction(text_lower: str) -> str:
    """P1: extract explicit direction from text."""
    for word, direction in _DIRECTION_WORDS.items():
        if re.search(r'\b' + re.escape(word) + r'\b', text_lower):
            return direction
    return ""


def _extract_tf(text_lower: str) -> str:
    m = re.search(r'\b(\d+)\s*(h|hour|m|min|d|day)\b', text_lower)
    if m:
        n, u = m.group(1), m.group(2)
        return f"{n}h" if u.startswith("h") else f"{n}m" if u.startswith("m") else f"{n}d"
    for word, tf in [("daily", "1d"), ("hourly", "1h"), ("4h", "4h"), ("1h", "1h"),
                     ("15m", "15m"), ("15 min", "15m"), ("5m", "5m"), ("h4", "4h"),
                     ("h1", "1h"), ("m15", "15m")]:
        if word in text_lower:
            return tf
    return "1h"


def _extract_symbol(text: str) -> str:
    STOPWORDS = {
        "monitor", "watch", "alert", "track", "me", "and", "for", "when",
        "the", "get", "all", "now", "this", "that", "with", "from", "any",
        "new", "goes", "can", "you", "then", "also", "wait", "tell", "let",
        "know", "if", "see", "on", "h4", "1h", "4h", "15m", "step", "first",
        "second", "third", "bearish", "bullish", "short", "long", "at", "in",
        "after", "until", "once", "time", "move", "price", "bias", "regime",
        "sweep", "bos", "choch", "ob", "fvg", "break", "structure", "block",
        "order", "fair", "value", "gap", "change", "character", "displacement",
    }
    for word in re.findall(r'\b[A-Za-z]{2,10}\b', text):
        if word.lower() not in STOPWORDS:
            sym = word.upper()
            if not sym.endswith("USDT"):
                sym = sym + "USDT"
            # Sanity: looks like a ticker (2-8 chars before USDT)
            base = sym.replace("USDT", "")
            if 2 <= len(base) <= 8:
                return sym
    return ""


def _parse_conditions(text_lower: str, direction: str) -> list:
    """
    P1: parse conditions into direction-aware dicts.
    Each condition carries its direction so mismatches can be silently ignored.
    """
    conditions = []

    for event, keywords in _CONDITION_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            conditions.append({
                "event":     event,
                "direction": direction,
                "strict":    bool(direction),
            })

    # Funding checks
    for event, keywords in _FUNDING_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            conditions.append({"event": event, "direction": "", "strict": False})
            break

    return conditions or [
        {"event": "sweep", "direction": direction, "strict": bool(direction)},
        {"event": "bos",   "direction": direction, "strict": bool(direction)},
    ]


def _detect_mode(text_lower: str) -> str:
    """P2: strict is default. Assisted only when user grants permission."""
    assisted_signals = [
        "anything else", "if you see anything", "also watch",
        "keep an eye", "anything important", "broader", "also alert",
        "and anything else", "plus anything",
    ]
    if any(s in text_lower for s in assisted_signals):
        return "assisted"
    return "strict"


def _is_workflow_request(text: str) -> bool:
    """Detect if user is defining a multi-step workflow."""
    text_lower = text.lower()
    has_steps  = bool(re.search(r'\d+[\.\)]\s*\w', text))
    has_then   = any(t in text_lower for t in _WORKFLOW_TRIGGERS)
    has_monitor = any(t in text_lower for t in _MONITOR_TRIGGERS)
    return has_monitor and (has_steps or has_then)


def _parse_monitor_request(text: str) -> dict | None:
    """
    P1/P2: Parse natural-language monitor requests.

    Extracts:
      - symbol
      - direction (bearish/bullish) — stored with every condition
      - conditions — direction-aware list
      - timeframe
      - mode (strict/assisted)
      - note

    Returns None if not a monitor request.
    """
    text_lower = text.lower()

    # ── Remove request ────────────────────────────────────────────────────
    if any(w in text_lower for w in _REMOVE_TRIGGERS):
        sym_match = re.search(r'\b([A-Za-z]{2,10})(usdt|USDT)?\b', text, re.IGNORECASE)
        if sym_match:
            return {"action": "remove", "symbol": sym_match.group(1).upper()}
        return None

    # ── Must be a monitor trigger ─────────────────────────────────────────
    if not any(w in text_lower for w in _MONITOR_TRIGGERS):
        return None

    # ── Workflow: multi-step sequence — delegate to workflow.py ───────────
    if _is_workflow_request(text):
        return {"action": "workflow", "text": text}

    # ── Simple monitor ────────────────────────────────────────────────────
    symbol    = _extract_symbol(text)
    if not symbol:
        return None

    direction  = _extract_direction(text_lower)
    tf         = _extract_tf(text_lower)
    conditions = _parse_conditions(text_lower, direction)
    mode       = _detect_mode(text_lower)

    note_match = re.search(r'[—\-]{1,2}\s*(.+)$', text)
    note       = note_match.group(1).strip() if note_match else ""

    return {
        "action":     "add",
        "symbol":     symbol,
        "tf":         tf,
        "conditions": conditions,
        "direction":  direction,
        "mode":       mode,
        "note":       note,
    }


# ── Fetch MEXC tickers (used by /crimes) ─────────────────────────────────

_NON_CRYPTO_BOT = {
    "stock", "xau", "xag", "usd_", "eur", "gbp", "jpy", "aud",
    "chf", "cad", "nzd", "oil", "gas", "corn", "wheat", "sp500",
    "nasdaq", "dow", "gold", "silver",
}


def _fetch_mexc_tickers() -> list:
    resp = requests.get(
        "https://contract.mexc.com/api/v1/contract/ticker",
        timeout=15,
    )
    resp.raise_for_status()
    raw = resp.json().get("data", [])

    tickers = []
    for t in raw:
        sym = t.get("symbol", "")
        if not sym.endswith("_USDT"):
            continue
        # Filter: crypto perpetuals only — no stocks, forex, commodities
        if any(s in sym.lower() for s in _NON_CRYPTO_BOT):
            continue

        # Robust change % parsing
        change_pct = None
        for field in ("priceChangePercent",):
            val = t.get(field)
            if val is not None:
                try:
                    fval = float(val)
                    if abs(fval) < 2.0 and fval != 0:
                        fval *= 100
                    change_pct = fval
                    break
                except (ValueError, TypeError):
                    continue
        if change_pct is None:
            for field in ("changeRate", "riseFallRate"):
                val = t.get(field)
                if val is not None:
                    try:
                        change_pct = float(val) * 100
                        break
                    except (ValueError, TypeError):
                        continue
        if change_pct is None:
            try:
                last  = float(t.get("lastPrice", 0))
                open_ = float(t.get("open24h", t.get("openPrice", 0)))
                if open_ > 0 and last > 0:
                    change_pct = (last - open_) / open_ * 100
            except Exception:
                pass
        if change_pct is None:
            continue
        try:
            tickers.append({
                "symbol":     sym.replace("_USDT", ""),
                "change_pct": round(change_pct, 2),
                "volume":     float(t.get("volume24", t.get("vol24", t.get("amount24", 0)))),
                "price":      float(t.get("lastPrice", 0)),
            })
        except (ValueError, TypeError):
            continue
    return tickers


# ── Main update handler ───────────────────────────────────────────────────

def handle_update(update: dict) -> None:
    global _alert_chat_id

    msg     = update.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    text    = msg.get("text", "").strip()
    if not chat_id or not text:
        return

    if not _alert_chat_id:
        _alert_chat_id = chat_id

    text_lower = text.lower()

    # ── /start ────────────────────────────────────────────────────────────
    if text.startswith("/start"):
        send_message(chat_id,
            "I'm <b>Scout</b> — your personal SMC trading assistant.\n\n"
            "I monitor markets 24/7 and alert you <b>only when meaningful conditions occur</b>.\n\n"
            "<b>Commands:</b>\n"
            "/setups — ranked SMC setups with full trade plans\n"
            "/majors — BTC, ETH, SOL, TON, BNB, XRP, SUI snapshot\n"
            "/scan — structure scan across watchlist\n"
            "/bias SOL — full SMC picture for any symbol\n"
            "/crimes — MEXC top gainers + losers\n"
            "/movers — MEXC quick top 5\n"
            "/funding BTC — funding rate intelligence\n"
            "/watchlist — your active monitors\n"
            "/workflows — your active custom workflows\n"
            "/history — past alert log\n\n"
            "<b>Monitor (strict — direction-aware):</b>\n"
            "• <i>Monitor SYN for bearish BOS</i>\n"
            "• <i>Watch ETH 4H for bullish sweep</i>\n"
            "• <i>Stop monitoring BTC</i>\n\n"
            "<b>Custom workflow (multi-step):</b>\n"
            "<i>Monitor SYN. H4 bearish. Wait for:\n"
            "1. Bearish CHoCH\n2. Bearish BOS\n3. Sweep highs\n4. Bearish OB retest</i>\n\n"
            "<b>Analysis:</b>\n"
            "• <i>What's happening with BTC?</i>\n"
            "• <i>Give me setups right now</i>\n"
            "• <i>Best opportunities</i>"
        )
        return

    # ── /help ─────────────────────────────────────────────────────────────
    if text.startswith("/help"):
        send_message(chat_id,
            "<b>Scout — Command Reference</b>\n\n"
            "<b>Setup discovery:</b>\n"
            "/setups — auto-generate ranked setups with entry/SL/TP\n"
            "/opportunities — same as /setups\n"
            "/scan — run structure scan now\n\n"
            "<b>Symbol analysis:</b>\n"
            "/bias SOL — HTF bias, regime, sweeps, BOS, OB, FVG\n"
            "/funding BTC — funding intelligence\n\n"
            "<b>Market overview:</b>\n"
            "/majors — BTC/ETH/SOL/TON/BNB/XRP/SUI classifications\n"
            "/crimes — MEXC top 20 gainers + losers with classification\n"
            "/movers — top 5 movers quick view\n\n"
            "<b>Your monitors:</b>\n"
            "/watchlist — view active monitors\n"
            "/workflows — view active custom workflows\n"
            "/clearwatchlist — remove all monitors\n"
            "/history — alert log from 0G Storage\n\n"
            "<b>Monitoring (strict by default):</b>\n"
            "• <i>Monitor SYN for bearish BOS</i> — only fires on bearish BOS\n"
            "• <i>Watch BTC 4H for bullish sweep</i>\n"
            "• <i>Monitor SOL for bearish BOS and tell me if you see anything else important</i> — assisted mode\n\n"
            "<b>Custom workflows:</b>\n"
            "<i>Monitor SYN. H4 bearish. Wait for:\n"
            "1. Bearish CHoCH\n2. Bearish BOS\n3. Sweep highs\n4. OB retest</i>\n\n"
            "Powered by 0G Compute decentralised inference."
        )
        return

    # ── /pause / /resume (Fix 2) ──────────────────────────────────────────
    if text.startswith("/pause"):
        from app.scanner import pause_alerts
        send_message(chat_id, pause_alerts(chat_id))
        return

    if text.startswith("/resume"):
        from app.scanner import resume_alerts
        send_message(chat_id, resume_alerts(chat_id))
        return

    # ── /setups / /opportunities — Priority 13 ────────────────────────────
    if text.startswith(("/setups", "/opportunities", "/opp")):
        send_message(chat_id, "⚡ Ranking setups... scanning market structure (~45s)")
        try:
            reply = reasoning.best_opportunities()
            send_message(chat_id, reply)
        except Exception as e:
            send_message(chat_id, f"⚠️ Opportunity scan failed: {e}")
        return

    # ── /majors — Priority 9 ──────────────────────────────────────────────
    if text.startswith("/majors"):
        send_message(chat_id, "🔍 Fetching major asset intelligence...")
        try:
            from app.scanner import get_major_summary, run_major_asset_scan
            summary = get_major_summary()
            if "not yet loaded" in summary:
                send_message(chat_id, "⏳ Running major scan now (~30s)...")
                run_major_asset_scan()
                summary = get_major_summary()
            send_message(chat_id, summary)
        except Exception as e:
            send_message(chat_id, f"⚠️ Major scan failed: {e}")
        return

    # ── /crimes — Priorities 10 + 11 ──────────────────────────────────────
    if text.startswith("/crimes"):
        send_message(chat_id, "🔍 Fetching MEXC top movers...")
        # Fix 1: stamp so background movers scan skips next cycle
        try:
            from app.scanner import stamp_manual_crimes
            stamp_manual_crimes()
        except Exception:
            pass
        try:
            tickers = _fetch_mexc_tickers()
            if not tickers:
                send_message(chat_id, "⚠️ MEXC data unavailable right now. Try again.")
                return

            live_tickers = [t for t in tickers if abs(t["change_pct"]) > 0.01]
            if not live_tickers:
                live_tickers = tickers

            sorted_t = sorted(live_tickers, key=lambda x: x["change_pct"])
            losers   = sorted_t[:20]
            gainers  = sorted_t[-20:][::-1]

            # P10: gainers with classification
            gain_lines = ["📈 <b>TOP 20 GAINERS</b>\n"]
            for i, m in enumerate(gainers, 1):
                chg  = m["change_pct"]
                flag = "🚀" if chg > 20 else "⚠️" if chg > 10 else "📈"
                gain_lines.append(f"{flag} {i}. <b>{m['symbol']}</b>  {chg:+.2f}%")

            # P11: losers with classification
            loss_lines = ["\n📉 <b>TOP 20 LOSERS</b>\n"]
            for i, m in enumerate(losers, 1):
                chg  = m["change_pct"]
                flag = "💥" if chg < -20 else "⚠️" if chg < -10 else "📉"
                loss_lines.append(f"{flag} {i}. <b>{m['symbol']}</b>  {chg:+.2f}%")

            send_message(chat_id, "\n".join(gain_lines))
            send_message(chat_id, "\n".join(loss_lines))

            # Ask 0G Compute to explain the top gainer if significant (P14: rich alerts)
            if gainers and abs(gainers[0]["change_pct"]) >= 5:
                top_mover = gainers[0]
                explanation = reasoning.explain_crime_move({
                    "symbol":     top_mover["symbol"] + "_USDT",
                    "change_pct": top_mover["change_pct"],
                    "volume_24h": top_mover.get("volume", 0),
                    "direction":  "PUMP",
                    "rvol":       1.0,
                    "velocity":   1.0,
                    "range_pct":  0,
                    "crime_score": abs(top_mover["change_pct"]) * 2,
                })
                if explanation:
                    send_message(chat_id,
                        f"🤖 <b>Top Mover Analysis — {top_mover['symbol']}</b>\n"
                        f"{top_mover['change_pct']:+.2f}%\n\n{explanation}"
                    )
        except Exception as e:
            log.error(f"/crimes error: {e}")
            send_message(chat_id, f"⚠️ MEXC fetch failed: {e}")
        return

    # ── /scan ─────────────────────────────────────────────────────────────
    if text.startswith("/scan"):
        send_message(chat_id, "🔍 Scanning watchlist for structure events... (~30s)")
        from app.scanner import run_structure_scan
        run_structure_scan()
        return

    # ── /movers ───────────────────────────────────────────────────────────
    if text.startswith("/movers"):
        from app.mexc_data import get_top_movers
        movers = get_top_movers(top_n=5)
        lines  = ["<b>MEXC Top Movers (24h)</b>\n"]
        lines.append("📈 <b>Gainers</b>")
        for m in movers["gainers"]:
            lines.append(f"  {m['symbol'].replace('_USDT','')}  <b>{m['change_pct']:+.2f}%</b>")
        lines.append("\n📉 <b>Losers</b>")
        for m in movers["losers"]:
            lines.append(f"  {m['symbol'].replace('_USDT','')}  <b>{m['change_pct']:+.2f}%</b>")
        send_message(chat_id, "\n".join(lines))
        return

    # ── /watchlist ────────────────────────────────────────────────────────
    if text.startswith("/watchlist"):
        from app.scanner import list_user_monitors
        send_message(chat_id, list_user_monitors(chat_id))
        return

    # ── /workflows — P5/P6/P7 ────────────────────────────────────────────
    if text.startswith("/workflows"):
        from app import workflow as wf_mod
        send_message(chat_id, wf_mod.list_workflows(chat_id))
        return

    # ── /clearwatchlist ───────────────────────────────────────────────────
    if (text.startswith("/clearwatchlist")
            or text_lower in (
                "clear watchlist", "reset watchlist", "clear my watchlist",
                "reset monitors", "clear monitors", "stop all monitors",
                "remove all monitors", "clear all", "stop monitoring everything",
            )):
        from app.scanner import clear_all_monitors
        from app import workflow as wf_mod
        send_message(chat_id, clear_all_monitors(chat_id))
        wf_mod.clear_workflows(chat_id)
        return

    # ── /history ──────────────────────────────────────────────────────────
    if text.startswith("/history"):
        send_message(chat_id, "Fetching alert history from 0G Storage...")
        try:
            from app.zg_storage import load_pointer, download
            root = load_pointer()
            if not root:
                send_message(chat_id, "No history yet — Scout will build it as alerts fire.")
                return
            state = download(root)
            log_entries = (state or {}).get("alert_log", [])
            if not log_entries:
                send_message(chat_id, "No alerts logged yet.")
                return
            recent = log_entries[-10:]
            lines  = ["<b>Last 10 Scout Alerts (0G Storage)</b>\n"]
            for e in reversed(recent):
                ts  = e.get("ts", "")[:16].replace("T", " ")
                typ = e.get("type", "").upper()
                sym = e.get("symbol", "").replace("_USDT", "").replace("USDT", "")
                lines.append(f"<b>{ts}</b>  {typ}  {sym}")
            send_message(chat_id, "\n".join(lines))
        except Exception as e:
            send_message(chat_id, f"⚠️ History unavailable: {e}")
        return

    # ── /funding ──────────────────────────────────────────────────────────
    if text_lower.startswith("/funding"):
        parts  = text.split()
        raw    = parts[1].upper() if len(parts) > 1 else "BTC"
        symbol = raw.replace("_USDT", "").replace("USDT", "") + "USDT"
        from app.market_data import get_funding_intelligence
        f = get_funding_intelligence(symbol)
        if f.get("error"):
            try:
                from app.mexc_data import get_funding_rate
                fd = mexc_funding = get_funding_rate(symbol)
                if fd.get("ok"):
                    rate    = fd["funding_rate"]
                    next_ms = fd.get("next_settle_time", 0)
                    now_ms  = int(time.time() * 1000)
                    mins    = max(0, (next_ms - now_ms) // 60000) if next_ms else 999
                    send_message(chat_id,
                        f"<b>Funding Rate — {symbol} (MEXC)</b>\n\n"
                        f"Rate: {rate * 100:+.4f}%\n"
                        f"Settlement in: {mins}min\n"
                        f"{'Longs pay shorts' if rate > 0 else 'Shorts pay longs' if rate < 0 else 'Neutral'}"
                    )
                else:
                    send_message(chat_id, f"⚠️ No funding data found for {symbol}.")
            except Exception as fe:
                send_message(chat_id, f"⚠️ Funding unavailable: {f.get('error', str(fe))}")
        else:
            warn = (
                "\n\n🚨 <b>SETTLEMENT WARNING</b>\n"
                "Funding settles in under 30 minutes. Avoid new entries."
            ) if f.get("settlement_warning") else ""
            send_message(chat_id, f"<b>Funding Rate — {symbol}</b>\n\n{f['read']}{warn}")
        return

    # ── /bias — P3 full trade setup ───────────────────────────────────────
    if text_lower.startswith("/bias"):
        parts = text.split()
        if len(parts) < 2:
            send_message(chat_id,
                "Which symbol? e.g. <b>/bias SOL</b>\n"
                "Or say: <i>What's happening with ETH?</i>"
            )
            return
        symbol = (parts[1].upper() + "USDT").replace("USDTUSDT", "USDT")
        send_message(chat_id, f"Analysing {symbol.replace('USDT','')}...")
        reply = reasoning.explain_structure(symbol)
        send_message(chat_id, reply)
        return

    # ── Natural language: monitor / workflow (P1, P2, P5) ─────────────────
    monitor_req = _parse_monitor_request(text)
    if monitor_req:
        action = monitor_req["action"]

        if action == "remove":
            from app.scanner import remove_user_monitor
            from app import workflow as wf_mod
            reply = remove_user_monitor(chat_id, monitor_req["symbol"])
            wf_mod.remove_workflow(chat_id, monitor_req["symbol"])
            send_message(chat_id, reply)
            return

        if action == "workflow":
            # P5: multi-stage workflow
            from app import workflow as wf_mod
            from app.scanner import add_user_monitor
            wf = wf_mod.parse_workflow_from_text(monitor_req["text"], chat_id)
            if wf:
                reply = wf_mod.add_workflow(chat_id, wf)
                # Also add a simple monitor so the scanner picks it up
                add_user_monitor(
                    chat_id    = chat_id,
                    symbol     = wf["symbol"],
                    direction  = wf["bias"].get("market_structure", ""),
                    mode       = wf["mode"],
                )
            else:
                reply = (
                    "⚠️ Couldn't parse that workflow. Try this format:\n\n"
                    "<i>Monitor SYN. H4 bearish. Wait for:\n"
                    "1. Bearish CHoCH\n"
                    "2. Bearish BOS\n"
                    "3. Sweep highs\n"
                    "4. Bearish OB retest</i>"
                )
            send_message(chat_id, reply)
            return

        if action == "add":
            from app.scanner import add_user_monitor
            reply = add_user_monitor(
                chat_id    = chat_id,
                symbol     = monitor_req["symbol"],
                tf         = monitor_req.get("tf", "1h"),
                conditions = monitor_req.get("conditions", []),
                note       = monitor_req.get("note", ""),
                direction  = monitor_req.get("direction", ""),
                mode       = monitor_req.get("mode", "strict"),
            )
            send_message(chat_id, reply)
            return

    # ── Free-form chat (P3/P12/P13: setups on demand) ─────────────────────
    log.info(f"chat {chat_id}: {text[:80]}")
    history = _chat_context.setdefault(chat_id, [])
    reply   = reasoning.route_message(text, recent_context=history, chat_id=chat_id)
    history.append({"role": "user",      "content": text})
    history.append({"role": "assistant", "content": reply})
    _chat_context[chat_id] = history[-10:]
    send_message(chat_id, reply)


def get_alert_chat_id() -> int:
    return _alert_chat_id


def run_polling_loop() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    from app import scanner
    log.info("Scout: starting Telegram polling loop")
    offset         = 0
    last_heartbeat = time.time()

    while True:
        try:
            resp = requests.get(
                f"{API_BASE}/getUpdates",
                params={"timeout": 30, "offset": offset},
                timeout=35,
            )
            resp.raise_for_status()
            updates = resp.json().get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                    if _alert_chat_id and not scanner._running:
                        scanner.start_scanner(
                            send_fn=send_message,
                            chat_id=_alert_chat_id,
                        )
                except Exception as e:
                    log.error(f"handle_update error: {e}")

            now = time.time()
            if now - last_heartbeat > 300:
                log.info("Scout: polling alive ✓")
                last_heartbeat = now

        except requests.exceptions.RequestException as e:
            log.warning(f"polling error (will retry): {e}")
            time.sleep(3)
