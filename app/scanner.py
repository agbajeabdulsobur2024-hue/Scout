"""
scanner.py — Scout's proactive intelligence scanner.

Runs as a background thread. Every N minutes it:
  1. Scans MEXC for crime (coordinated pumps/dumps)
  2. Scans Binance watchlist for sweeps, BOS, and structure breaks
  3. If something fires, asks 0G Compute to explain it
  4. Sends a Telegram alert unprompted

This is what makes Scout an agent, not just a chatbot. It watches
the market for you and speaks up when something real happens.
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger("scout")

# How often to run each scan (seconds)
MEXC_SCAN_INTERVAL      = int(os.environ.get("MEXC_SCAN_INTERVAL",  "300"))   # 5 min
STRUCTURE_SCAN_INTERVAL = int(os.environ.get("STRUCTURE_SCAN_INTERVAL", "600")) # 10 min

# Alert dedup — don't re-alert the same event within this window (seconds)
ALERT_COOLDOWN = int(os.environ.get("ALERT_COOLDOWN", "1800"))  # 30 min

# Binance symbols to watch for sweeps/BOS
STRUCTURE_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "AVAXUSDT",
    "LINKUSDT", "ADAUSDT", "DOTUSDT", "MATICUSDT", "XRPUSDT",
]

_alert_cooldowns: dict = {}   # {event_key: timestamp}
_send_fn = None               # set by start_scanner() to telegram_bot.send_message
_chat_id = None               # set by start_scanner()
_running = False


def _is_cooled_down(key: str) -> bool:
    last = _alert_cooldowns.get(key, 0)
    return time.time() - last > ALERT_COOLDOWN


def _mark_alerted(key: str):
    _alert_cooldowns[key] = time.time()


def _send_alert(text: str):
    if _send_fn and _chat_id:
        try:
            _send_fn(_chat_id, text)
        except Exception as e:
            log.error(f"scanner: alert send failed — {e}")


def _explain_crime(crime: dict) -> str:
    """Ask 0G Compute to explain a suspicious MEXC move."""
    try:
        from app.zg_compute import ask, ZGComputeError
        sym    = crime["symbol"].replace("_USDT", "")
        chg    = crime["change_pct"]
        rvol   = crime["rvol"]
        vel    = crime["velocity"]
        rng    = crime["range_pct"]
        prompt = (
            f"A MEXC futures token just made a suspicious move:\n"
            f"Symbol: {sym}\n"
            f"24h change: {chg:+.1f}%\n"
            f"Volume vs normal (RVOL): {rvol:.1f}x\n"
            f"Price velocity (recent vs prior): {vel:.1f}x\n"
            f"Daily range: {rng:.1f}% of price\n\n"
            f"In 2-3 sentences: does this look like coordinated manipulation "
            f"(pump/dump scheme)? What signals support or contradict that? "
            f"What should a trader watch for next?"
        )
        return ask([
            {"role": "system", "content":
             "You are Scout, a market intelligence companion. Be direct and specific. "
             "Do not hedge excessively. If it looks like manipulation, say so."},
            {"role": "user", "content": prompt},
        ], max_tokens=150)
    except Exception as e:
        log.debug(f"_explain_crime failed: {e}")
        return ""


def _explain_sweep(symbol: str, sweep: dict, bias_data: dict) -> str:
    """Ask 0G Compute to explain a detected sweep."""
    try:
        from app.zg_compute import ask
        sym   = symbol.replace("USDT", "")
        level = sweep.get("level", 0)
        direction = sweep.get("direction", "")
        desc  = sweep.get("description", "")
        daily_bias = bias_data.get("daily_bias", "neutral")
        h4_bias    = bias_data.get("h4_bias", "neutral")
        prompt = (
            f"A liquidity sweep was just detected on {sym} (H1 timeframe):\n"
            f"{desc}\n"
            f"HTF context: Daily bias = {daily_bias}, H4 bias = {h4_bias}\n\n"
            f"In 2-3 sentences: what does this sweep mean for the next likely "
            f"move? Is this a valid entry signal or a trap? What confirms or "
            f"invalidates this setup?"
        )
        return ask([
            {"role": "system", "content":
             "You are Scout, a market intelligence companion specializing in "
             "Smart Money Concepts. Be specific about price levels and structure."},
            {"role": "user", "content": prompt},
        ], max_tokens=150)
    except Exception as e:
        log.debug(f"_explain_sweep failed: {e}")
        return ""


def run_mexc_crime_scan():
    """Scan MEXC for suspicious moves and alert if found."""
    log.info("scanner: running MEXC crime scan...")
    try:
        from app.mexc_data import scan_for_crimes
        crimes = scan_for_crimes(top_n=5)
        if not crimes:
            log.info("scanner: MEXC scan clean — no crimes detected")
            return

        for crime in crimes:
            sym   = crime["symbol"]
            chg   = crime["change_pct"]
            score = crime["crime_score"]
            key   = f"crime_{sym}_{int(abs(chg) / 10)}"  # dedup by symbol + magnitude bucket

            if not _is_cooled_down(key):
                continue

            direction = crime["direction"]
            severity  = crime["severity"]
            rvol      = crime["rvol"]
            rng       = crime["range_pct"]

            explanation = _explain_crime(crime)

            msg_lines = [
                f"{severity} — MEXC CRIME DETECTED",
                f"",
                f"Symbol: {sym.replace('_USDT', '')}/USDT",
                f"Move: {chg:+.1f}%  ({direction})",
                f"Volume: {rvol:.1f}x normal",
                f"Daily range: {rng:.1f}%",
                f"Crime score: {score:.0f}",
            ]
            if explanation:
                msg_lines += ["", "Scout's read:", explanation]
            msg_lines += [
                "",
                f"Scanned at {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
            ]

            _send_alert("\n".join(msg_lines))
            _mark_alerted(key)
            log.info(f"scanner: crime alert sent for {sym} ({chg:+.1f}%)")

    except Exception as e:
        log.error(f"scanner: MEXC crime scan error — {e}")


def run_structure_scan():
    """Scan Binance watchlist for sweeps, BOS, and structure breaks."""
    log.info("scanner: running structure scan...")
    try:
        from app.market_data import get_klines
        from app.structure import full_structure_snapshot

        for symbol in STRUCTURE_WATCHLIST:
            try:
                candles_h1    = get_klines(symbol, "1h",  50)
                candles_h4    = get_klines(symbol, "4h",  50)
                candles_daily = get_klines(symbol, "1d",  30)

                if not candles_h1:
                    continue

                snap = full_structure_snapshot(symbol, candles_h1, candles_h4, candles_daily)
                sweep = snap.get("recent_sweep")
                bos   = snap.get("bos_h1", {})
                bias  = snap.get("bias", "neutral")
                inducements = snap.get("inducement_zones", [])

                # ── Alert on fresh sweep (within last 3 H1 candles) ──────
                if sweep and sweep.get("age_candles", 99) <= 3:
                    key = f"sweep_{symbol}_{round(sweep['level'], 2)}"
                    if _is_cooled_down(key):
                        explanation = _explain_sweep(symbol, sweep, snap["bias_data"])
                        sym_short = symbol.replace("USDT", "")

                        indu_lines = []
                        for z in inducements[:2]:
                            indu_lines.append(
                                f"  • {z['price']:.4f}  ({z['distance_pct']:+.1f}%)  — {z['note']}"
                            )

                        msg_lines = [
                            f"🎯 LIQUIDITY SWEEP — {sym_short}",
                            f"",
                            f"{sweep['description']}",
                            f"HTF Bias: {bias.upper()}",
                            f"Depth: {sweep['depth_pct']:.2f}%",
                        ]
                        if indu_lines:
                            msg_lines += ["", "Inducement zones ahead:"] + indu_lines
                        if explanation:
                            msg_lines += ["", "Scout's read:", explanation]
                        msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")

                        _send_alert("\n".join(msg_lines))
                        _mark_alerted(key)
                        log.info(f"scanner: sweep alert sent for {symbol}")

                # ── Alert on fresh BOS (within last 5 H1 candles) ────────
                if bos.get("broken") and bos.get("candle_idx", 0) >= len(candles_h1) - 5:
                    bos_bias = bos.get("bias", "")
                    key = f"bos_{symbol}_{round(bos.get('level', 0), 2)}_{bos_bias}"
                    if _is_cooled_down(key):
                        sym_short = symbol.replace("USDT", "")
                        msg_lines = [
                            f"⚡ BREAK OF STRUCTURE — {sym_short}",
                            f"",
                            f"{bos.get('description', '')}",
                            f"HTF Bias: {bias.upper()}",
                            f"Alignment: {'✅ WITH bias' if bos_bias == bias else '⚠️ AGAINST bias'}",
                        ]
                        if inducements:
                            next_zone = inducements[0]
                            msg_lines.append(
                                f"Next inducement: {next_zone['price']:.4f} "
                                f"({next_zone['distance_pct']:+.1f}%)"
                            )
                        msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                        _send_alert("\n".join(msg_lines))
                        _mark_alerted(key)
                        log.info(f"scanner: BOS alert sent for {symbol}")

            except Exception as e:
                log.debug(f"scanner: structure scan error for {symbol} — {e}")
                continue

    except Exception as e:
        log.error(f"scanner: structure scan error — {e}")


def _mexc_loop():
    while _running:
        run_mexc_crime_scan()
        time.sleep(MEXC_SCAN_INTERVAL)


def _structure_loop():
    # Stagger by 60s so both loops don't fire at the same time on startup
    time.sleep(60)
    while _running:
        run_structure_scan()
        time.sleep(STRUCTURE_SCAN_INTERVAL)


def start_scanner(send_fn, chat_id: int):
    """
    Start the background scanner threads.
    send_fn: function(chat_id, text) — telegram_bot.send_message
    chat_id: the Telegram chat ID to send alerts to
    """
    global _send_fn, _chat_id, _running
    _send_fn = send_fn
    _chat_id = chat_id
    _running = True

    t1 = threading.Thread(target=_mexc_loop,      daemon=True, name="mexc-scanner")
    t2 = threading.Thread(target=_structure_loop, daemon=True, name="structure-scanner")
    t1.start()
    t2.start()
    log.info(f"scanner: started — MEXC every {MEXC_SCAN_INTERVAL}s, structure every {STRUCTURE_SCAN_INTERVAL}s")


def stop_scanner():
    global _running
    _running = False
    log.info("scanner: stopped")
