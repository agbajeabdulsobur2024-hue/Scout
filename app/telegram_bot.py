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
      "watch SOL 4h sweeps and BOS"
      "monitor ETH — looking for displacement after sweep of lows"
      "stop monitoring BTC"
      "remove ETH from watchlist"
    Returns {action, symbol, tf, conditions, note} or None
    """
    text_lower = text.lower()

    # Stop monitoring
    if any(w in text_lower for w in ["stop monitor", "remove", "unwatch", "stop watch"]):
        sym_match = re.search(r'\b([A-Za-z]{2,10})(usdt)?\b', text, re.IGNORECASE)
        if sym_match:
            return {"action": "remove", "symbol": sym_match.group(1)}
        return None

    # Start monitoring
    if any(w in text_lower for w in ["monitor", "watch", "alert me", "track"]):
        # Extract symbol
        sym_match = re.search(r'\b([A-Z]{2,10})(USDT)?\b', text)
        if not sym_match:
            return None
        symbol = sym_match.group(1)

        # Extract timeframe
        tf = "1h"
        tf_match = re.search(r'\b(\d+)\s*(h|hour|m|min|d|day)\b', text_lower)
        if tf_match:
            n, unit = tf_match.group(1), tf_match.group(2)
            tf = f"{n}h" if unit.startswith("h") else f"{n}m" if unit.startswith("m") else f"{n}d"

        # Extract conditions
        conditions = []
        if any(w in text_lower for w in ["sweep", "hunt", "liquidity"]):
            conditions.append("sweep")
        if any(w in text_lower for w in ["bos", "break", "structure"]):
            conditions.append("bos")
        if not conditions:
            conditions = ["sweep", "bos"]  # default: both

        # Extract note (anything after "—" or "note:" or "because")
        note = ""
        note_match = re.search(r'[—\-]{1,2}\s*(.+)$', text)
        if note_match:
            note = note_match.group(1).strip()

        return {
            "action": "add",
            "symbol": symbol,
            "tf": tf,
            "conditions": conditions,
            "note": note,
        }

    return None


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
            "<b>I watch markets 24/7 and alert you unprompted when:</b>\n"
            "• Liquidity sweep detected (H1/H4)\n"
            "• Break of Structure confirmed\n"
            "• Coordinated pump/dump on MEXC (5%+ with unusual volume)\n\n"
            "<b>Commands:</b>\n"
            "/crimes — run MEXC crime scan now\n"
            "/scan — run structure scan now\n"
            "/movers — top MEXC gainers/losers\n"
            "/watchlist — see your monitors\n"
            "/clearwatchlist — stop all monitors\n"
            "/bias SOL — HTF bias + structure for a symbol\n"
            "/funding BTC — funding rate intelligence\n\n"
            "<b>Or just talk naturally:</b>\n"
            "• <i>Monitor BTC on 1H for sweeps</i>\n"
            "• <i>Watch ETH 4H for BOS — looking for bearish setup</i>\n"
            "• <i>Stop monitoring SOL</i>\n"
            "• <i>Why is AVAX dumping?</i>\n"
            "• <i>Best setups right now</i>\n"
            "• <i>Show me MEXC crimes</i>"
        )
        return

    if text.startswith("/help"):
        send_message(chat_id,
            "<b>Scout — Full Command Reference</b>\n\n"
            "/crimes — MEXC crime scan (5%+ moves with volume anomaly)\n"
            "/scan — structure scan across default watchlist\n"
            "/movers — top MEXC gainers and losers\n"
            "/watchlist — your personal monitors\n"
            "/bias BTC — HTF bias, sweeps, BOS, inducement zones\n\n"
            "<b>Natural monitoring:</b>\n"
            "• <i>Monitor BTCUSDT on 1H for sweeps</i>\n"
            "• <i>Watch SOL 4H BOS</i>\n"
            "• <i>Stop monitoring ETH</i>\n\n"
            "All intelligence powered by 0G Compute decentralized inference."
        )
        return

    if text.startswith("/crimes"):
        send_message(chat_id, "🔍 Fetching MEXC top 20 movers...")
        try:
            import requests as _req
            resp = _req.get(
                "https://contract.mexc.com/api/v1/contract/ticker",
                timeout=15
            )
            raw = resp.json().get("data", [])
            tickers = []
            for t in raw:
                sym = t.get("symbol", "")
                if not sym.endswith("_USDT"):
                    continue
                try:
                    tickers.append({
                        "symbol":     sym.replace("_USDT", ""),
                        "change_pct": float(t.get("priceChangePercent", 0)),
                        "volume":     float(t.get("volume24", 0)),
                    })
                except Exception:
                    continue

            if not tickers:
                send_message(chat_id, "⚠️ MEXC data unavailable right now. Try again.")
                return

            sorted_t  = sorted(tickers, key=lambda x: x["change_pct"])
            losers     = sorted_t[:20]
            gainers    = sorted_t[-20:][::-1]

            gain_lines = ["📈 <b>TOP 20 GAINERS</b>\n"]
            for i, m in enumerate(gainers, 1):
                chg  = m["change_pct"]
                flag = "🚨" if chg > 30 else "⚠️" if chg > 15 else "📈"
                gain_lines.append(f"{flag} {i}. <b>{m['symbol']}</b>  {chg:+.1f}%")

            loss_lines = ["\n📉 <b>TOP 20 LOSERS</b>\n"]
            for i, m in enumerate(losers, 1):
                chg  = m["change_pct"]
                flag = "🚨" if chg < -30 else "⚠️" if chg < -15 else "📉"
                loss_lines.append(f"{flag} {i}. <b>{m['symbol']}</b>  {chg:+.1f}%")

            send_message(chat_id, "\n".join(gain_lines))
            send_message(chat_id, "\n".join(loss_lines))

        except Exception as e:
            send_message(chat_id, f"⚠️ MEXC fetch failed: {e}")
        return

    if text.startswith("/scan"):
        send_message(chat_id, "🔍 Scanning watchlist for sweeps and BOS... (~30s)")
        from app.scanner import run_structure_scan
        run_structure_scan()
        return

    if text.startswith("/movers"):
        from app.mexc_data import get_top_movers
        movers = get_top_movers(top_n=5)
        lines  = ["<b>MEXC Top Movers (24h)</b>\n"]
        lines.append("📈 <b>Gainers</b>")
        for m in movers["gainers"]:
            lines.append(f"  {m['symbol'].replace('_USDT','')}  <b>{m['change_pct']:+.1f}%</b>")
        lines.append("\n📉 <b>Losers</b>")
        for m in movers["losers"]:
            lines.append(f"  {m['symbol'].replace('_USDT','')}  <b>{m['change_pct']:+.1f}%</b>")
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
        # Normalise: strip any existing USDT suffix, then re-add
        symbol = raw.replace("_USDT", "").replace("USDT", "") + "USDT"
        from app.market_data import get_funding_intelligence
        f = get_funding_intelligence(symbol)
        if f.get("error"):
            # Try fetching directly from MEXC as last resort
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
                    send_message(chat_id, f"⚠️ No funding data found for {symbol} on MEXC or Binance.")
            except Exception as _fe:
                send_message(chat_id, f"⚠️ Funding unavailable for {symbol}: {f['error']}")
        else:
            warn = (
                "\n\n🚨 <b>SETTLEMENT WARNING</b>\n"
                "Funding settles in under 30 minutes. Avoid new entries — "
                "expect a volatility spike at settlement."
            ) if f.get("settlement_warning") else ""
            send_message(chat_id,
                f"<b>Funding Rate — {symbol}</b>\n\n{f['read']}{warn}"
            )
        return

    if text.lower().startswith("/bias"):
        parts  = text.split()
        symbol = (parts[1].upper() + "USDT").replace("USDTUSDT", "USDT") if len(parts) > 1 else "BTCUSDT"
        send_message(chat_id, f"Analysing {symbol} structure...")
        reply = reasoning.explain_structure(symbol)
        send_message(chat_id, reply)
        return

    # ── Natural monitor/unmonitor requests ───────────────────────────────
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

    # ── Free-form chat ────────────────────────────────────────────────────
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

            # Heartbeat every 5 minutes so Railway logs show it's alive
            now = time.time()
            if now - last_heartbeat > 300:
                log.info("Scout: polling alive ✓")
                last_heartbeat = now

        except requests.exceptions.RequestException as e:
            log.warning(f"polling error (will retry): {e}")
            time.sleep(3)
