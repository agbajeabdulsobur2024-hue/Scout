"""
telegram_bot.py — Scout's Telegram interface.
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

_chat_context: dict = {}
_alert_chat_id: int = int(os.environ.get("SCOUT_ALERT_CHAT_ID", "0"))


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


def _parse_monitor_request(text: str) -> dict:
    """
    Parse natural-language monitor requests.
    Examples:
      "monitor BTCUSDT on 1H for sweeps"
      "watch Sol 4h sweeps and BOS"
      "monitor Asteroid when funding goes negative"
      "stop monitoring BTC"
    """
    text_lower = text.lower()

    # Stop monitoring
    if any(w in text_lower for w in ["stop monitor", "remove", "unwatch", "stop watch"]):
        sym_match = re.search(r'\b([A-Za-z]{2,12})(usdt|USDT)?\b', text, re.IGNORECASE)
        if sym_match:
            return {"action": "remove", "symbol": sym_match.group(1).upper()}
        return None

    trigger_words = ["monitor", "watch", "alert", "track", "inform me", "tell me", "notify"]
    if not any(w in text_lower for w in trigger_words):
        return None

    STOPWORDS = {"can", "you", "me", "and", "for", "when", "the", "get", "all",
                 "now", "this", "that", "with", "from", "any", "new", "goes"}
    symbol = ""
    for word in re.findall(r'\b[A-Za-z]{2,12}\b', text):
        if word.lower() not in STOPWORDS and word.lower() not in trigger_words:
            symbol = word.upper()
            break
    if not symbol:
        return None

    tf = "1h"
    tf_match = re.search(r'\b(\d+)\s*(h|hour|m|min|d|day)\b', text_lower)
    if tf_match:
        n, unit = tf_match.group(1), tf_match.group(2)
        tf = f"{n}h" if unit.startswith("h") else f"{n}m" if unit.startswith("m") else f"{n}d"

    conditions = []
    if any(w in text_lower for w in ["sweep", "hunt", "liquidity"]):
        conditions.append("sweep")
    if any(w in text_lower for w in ["bos", "break", "structure"]):
        conditions.append("bos")
    if any(w in text_lower for w in ["funding", "fund", "rate"]):
        if any(w in text_lower for w in ["negative", "neg", "shorts pay", "below zero"]):
            conditions.append("funding_negative")
        elif any(w in text_lower for w in ["positive", "pos", "longs pay", "above zero"]):
            conditions.append("funding_positive")
        else:
            conditions.append("funding_change")
    if not conditions:
        conditions = ["sweep", "bos"]

    note = ""
    note_match = re.search(r'[—\-]{1,2}\s*(.+)$', text)
    if note_match:
        note = note_match.group(1).strip()

    return {
        "action":     "add",
        "symbol":     symbol,
        "tf":         tf,
        "conditions": conditions,
        "note":       note,
    }


def _fetch_mexc_tickers() -> list:
    """
    Fetch and parse MEXC futures tickers.
    Tries multiple field names for priceChangePercent to handle API variations.
    Returns list of {symbol, change_pct, volume, price}.
    """
    resp = requests.get(
        "https://contract.mexc.com/api/v1/contract/ticker",
        timeout=15,
    )
    resp.raise_for_status()
    raw = resp.json().get("data", [])

    # DEBUG: Log the first ticker's keys so we can see exactly what MEXC returns
    if raw:
        log.info(f"MEXC ticker fields (first record): {list(raw[0].keys())}")
        log.info(f"MEXC ticker sample: {raw[0]}")

    tickers = []
    for t in raw:
        sym = t.get("symbol", "")
        if not sym.endswith("_USDT"):
            continue

        # Try all known field name variants for 24h change
        change_pct = None
        for field in ("priceChangePercent", "changeRate", "riseFallRate", "priceChange24h"):
            val = t.get(field)
            if val is not None:
                try:
                    fval = float(val)
                    # MEXC sometimes returns as decimal (0.05 = 5%) or percentage (5.0)
                    # Detect which: if abs value < 1.5, it's probably a decimal
                    if abs(fval) < 1.5 and fval != 0:
                        fval = fval * 100  # convert to percentage
                    change_pct = fval
                    break
                except (ValueError, TypeError):
                    continue

        if change_pct is None:
            # Last resort: compute from lastPrice and fairPrice or open24h
            try:
                last  = float(t.get("lastPrice", 0))
                open_ = float(t.get("open24h", t.get("openPrice", 0)))
                if open_ > 0 and last > 0:
                    change_pct = (last - open_) / open_ * 100
            except Exception:
                pass

        if change_pct is None:
            continue  # skip this ticker — can't compute change

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


def handle_update(update: dict) -> None:
    global _alert_chat_id

    msg     = update.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    text    = msg.get("text", "").strip()
    if not chat_id or not text:
        return

    if not _alert_chat_id:
        _alert_chat_id = chat_id

    # ── Commands ──────────────────────────────────────────────────────────
    if text.startswith("/start"):
        send_message(chat_id,
            "I'm <b>Scout</b> — an AI market intelligence companion running on "
            "<b>0G Compute</b>'s decentralized inference network.\n\n"
            "<b>I watch markets 24/7 and alert you when:</b>\n"
            "• Liquidity sweep detected (M15/H1/H4)\n"
            "• Break of Structure confirmed\n"
            "• Coordinated pump/dump on MEXC\n\n"
            "<b>Commands:</b>\n"
            "/crimes — MEXC top movers (gainers + losers)\n"
            "/scan — run structure scan across watchlist\n"
            "/opportunities — ranked setups right now\n"
            "/bias SOL — full SMC picture for a symbol\n"
            "/movers — quick MEXC top 5 movers\n"
            "/funding BTC — funding rate intelligence\n"
            "/watchlist — your personal monitors\n"
            "/history — alert history from 0G Storage\n\n"
            "<b>Or just talk naturally:</b>\n"
            "• <i>What's happening with BTC?</i>\n"
            "• <i>Best setups right now</i>\n"
            "• <i>Monitor SOL on 1H for sweeps</i>\n"
            "• <i>Why is AVAX dumping?</i>"
        )
        return

    if text.startswith("/help"):
        send_message(chat_id,
            "<b>Scout — Full Command Reference</b>\n\n"
            "/crimes — MEXC crime scanner (top 20 gainers + losers)\n"
            "/scan — structure scan across default watchlist\n"
            "/opportunities — ranked SMC setups, scored by intelligence engine\n"
            "/movers — MEXC top 5 gainers/losers\n"
            "/bias BTC — HTF bias, sweeps, BOS, inducement zones\n"
            "/funding BTC — funding rate intelligence\n"
            "/watchlist — your personal monitors\n"
            "/history — past alert log from 0G Storage\n\n"
            "<b>Natural language:</b>\n"
            "• <i>Monitor BTCUSDT on 1H for sweeps</i>\n"
            "• <i>Watch SOL 4H BOS</i>\n"
            "• <i>Stop monitoring ETH</i>\n\n"
            "All intelligence powered by 0G Compute."
        )
        return

    # ── /crimes — FIXED ────────────────────────────────────────────────────
    if text.startswith("/crimes"):
        send_message(chat_id, "🔍 Fetching MEXC top movers...")
        try:
            tickers = _fetch_mexc_tickers()

            if not tickers:
                send_message(chat_id, "⚠️ MEXC data unavailable right now. Try again.")
                return

            # Filter: must have a real move (not 0.0%)
            live_tickers = [t for t in tickers if abs(t["change_pct"]) > 0.01]
            if not live_tickers:
                # Fallback: all tickers even if change is 0 — something is wrong with the feed
                live_tickers = tickers
                send_message(chat_id,
                    "⚠️ Warning: all change values are near 0%. "
                    "MEXC may be returning stale data. Showing raw feed.\n"
                )

            sorted_t = sorted(live_tickers, key=lambda x: x["change_pct"])
            losers   = sorted_t[:20]
            gainers  = sorted_t[-20:][::-1]

            gain_lines = ["📈 <b>TOP 20 GAINERS</b>\n"]
            for i, m in enumerate(gainers, 1):
                chg  = m["change_pct"]
                flag = "🚨" if chg > 30 else "⚠️" if chg > 15 else "📈"
                gain_lines.append(f"{flag} {i}. <b>{m['symbol']}</b>  {chg:+.2f}%")

            loss_lines = ["\n📉 <b>TOP 20 LOSERS</b>\n"]
            for i, m in enumerate(losers, 1):
                chg  = m["change_pct"]
                flag = "🚨" if chg < -30 else "⚠️" if chg < -15 else "📉"
                loss_lines.append(f"{flag} {i}. <b>{m['symbol']}</b>  {chg:+.2f}%")

            send_message(chat_id, "\n".join(gain_lines))
            send_message(chat_id, "\n".join(loss_lines))

            # Ask 0G Compute to explain the top crime if significant
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
                        f"🤖 <b>0G Compute — Top Mover Analysis</b>\n\n"
                        f"<b>{top_mover['symbol']}</b> ({top_mover['change_pct']:+.2f}%)\n\n"
                        f"{explanation}"
                    )

        except Exception as e:
            log.error(f"/crimes error: {e}")
            send_message(chat_id, f"⚠️ MEXC fetch failed: {e}")
        return

    # ── /scan ──────────────────────────────────────────────────────────────
    if text.startswith("/scan"):
        send_message(chat_id, "🔍 Scanning watchlist for sweeps and BOS... (~30s)")
        from app.scanner import run_structure_scan
        run_structure_scan()
        return

    # ── /opportunities — NEW ───────────────────────────────────────────────
    if text.startswith("/opportunities") or text.startswith("/opp"):
        send_message(chat_id, "⚡ Ranking setups by intelligence score... (~45s)")
        try:
            reply = reasoning.best_opportunities()
            send_message(chat_id, reply)
        except Exception as e:
            send_message(chat_id, f"⚠️ Opportunity scan failed: {e}")
        return

    # ── /movers ────────────────────────────────────────────────────────────
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

    if text.startswith("/watchlist"):
        from app.scanner import list_user_monitors
        send_message(chat_id, list_user_monitors(chat_id))
        return

    if text.startswith("/clearwatchlist") or text.lower() in (
        "clear watchlist", "reset watchlist", "clear my watchlist",
        "reset monitors", "clear monitors", "stop all monitors",
        "remove all monitors", "clear all", "stop monitoring everything",
    ):
        from app.scanner import clear_all_monitors
        send_message(chat_id, clear_all_monitors(chat_id))
        return

    if text.startswith("/history"):
        send_message(chat_id, "Fetching your alert history from 0G Storage...")
        try:
            from app.zg_storage import load_pointer, download
            root = load_pointer()
            if not root:
                send_message(chat_id, "No history found yet — Scout will build it as alerts fire.")
                return
            state = download(root)
            log_entries = (state or {}).get("alert_log", [])
            if not log_entries:
                send_message(chat_id, "No alerts logged yet.")
                return
            recent = log_entries[-10:]
            lines  = ["<b>Last 10 Scout Alerts (from 0G Storage)</b>\n"]
            for e in reversed(recent):
                ts  = e.get("ts", "")[:16].replace("T", " ")
                typ = e.get("type", "").upper()
                sym = e.get("symbol", "").replace("_USDT", "").replace("USDT", "")
                lines.append(f"<b>{ts}</b>  {typ}  {sym}")
            send_message(chat_id, "\n".join(lines))
        except Exception as e:
            send_message(chat_id, f"⚠️ History unavailable: {e}")
        return

    if text.lower().startswith("/funding"):
        parts  = text.split()
        raw    = parts[1].upper() if len(parts) > 1 else "BTC"
        symbol = raw.replace("_USDT", "").replace("USDT", "") + "USDT"
        from app.market_data import get_funding_intelligence
        f = get_funding_intelligence(symbol)
        if f.get("error"):
            try:
                from app.mexc_data import get_funding_rate
                fd = get_funding_rate(symbol)
                if fd.get("ok"):
                    rate = fd["funding_rate"]
                    next_ms = fd.get("next_settle_time", 0)
                    now_ms  = int(__import__("time").time() * 1000)
                    mins    = max(0, (next_ms - now_ms) // 60000) if next_ms else 999
                    send_message(chat_id,
                        f"<b>Funding Rate — {symbol} (MEXC)</b>\n\n"
                        f"Rate: {rate * 100:+.4f}%\n"
                        f"Settlement in: {mins}min\n"
                        f"{'Longs pay shorts' if rate > 0 else 'Shorts pay longs' if rate < 0 else 'Neutral'}"
                    )
                else:
                    send_message(chat_id, f"⚠️ No funding data found for {symbol}.")
            except Exception as _fe:
                send_message(chat_id, f"⚠️ Funding unavailable: {f['error']}")
        else:
            warn = (
                "\n\n🚨 <b>SETTLEMENT WARNING</b>\n"
                "Funding settles in under 30 minutes. Avoid new entries."
            ) if f.get("settlement_warning") else ""
            send_message(chat_id,
                f"<b>Funding Rate — {symbol}</b>\n\n{f['read']}{warn}"
            )
        return

    if text.lower().startswith("/bias"):
        parts  = text.split()
        if len(parts) < 2:
            send_message(chat_id,
                "Which symbol? e.g. <b>/bias SOL</b>\n"
                "Or say: <i>what's the bias on ETH</i>"
            )
            return
        symbol = (parts[1].upper() + "USDT").replace("USDTUSDT", "USDT")
        send_message(chat_id, f"Analysing {symbol}...")
        reply = reasoning.explain_structure(symbol)
        send_message(chat_id, reply)
        return

    # ── Natural monitor/unmonitor ─────────────────────────────────────────
    monitor_req = _parse_monitor_request(text)
    if monitor_req:
        from app.scanner import add_user_monitor, remove_user_monitor
        if monitor_req["action"] == "add":
            reply = add_user_monitor(
                chat_id=chat_id,
                symbol=monitor_req["symbol"],
                tf=monitor_req.get("tf", "1h"),
                conditions=monitor_req.get("conditions", ["sweep", "bos"]),
                note=monitor_req.get("note", ""),
            )
        else:
            reply = remove_user_monitor(chat_id, monitor_req["symbol"])
        send_message(chat_id, reply)
        return

    # ── Free-form chat ─────────────────────────────────────────────────────
    log.info(f"chat {chat_id}: {text[:80]}")
    history = _chat_context.setdefault(chat_id, [])
    reply   = reasoning.route_message(text, recent_context=history)
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
    offset = 0
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
