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

# ── User watchlists ────────────────────────────────────────────────────────
# Stores per-user custom monitoring requests.
# Format: {chat_id: [{symbol, tf, conditions, note}]}
_user_watchlists: dict = {}


def add_user_monitor(chat_id: int, symbol: str, tf: str = "1h",
                     conditions: list = None, note: str = "") -> str:
    """
    Register a symbol for a user's personal watchlist.
    The scanner will check this symbol on every structure scan and alert
    that specific user when conditions are met.
    """
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"

    entry = {
        "symbol":     symbol,
        "tf":         tf,
        "conditions": conditions or ["sweep", "bos"],
        "note":       note,
    }
    if chat_id not in _user_watchlists:
        _user_watchlists[chat_id] = []

    # Don't add duplicates
    existing = [w["symbol"] for w in _user_watchlists[chat_id]]
    if symbol in existing:
        return f"{symbol} is already on your watchlist."

    _user_watchlists[chat_id].append(entry)
    log.info(f"scanner: {chat_id} added {symbol} to personal watchlist")

    # Persist to 0G Storage so watchlist survives restarts
    import threading
    threading.Thread(target=_persist_state, daemon=True).start()

    return (
        f"✅ Now monitoring {symbol} on {tf.upper()} for "
        f"{', '.join(entry['conditions'])}.\n"
        f"You'll get an alert the moment something happens."
        + (f"\nNote: {note}" if note else "")
    )


def remove_user_monitor(chat_id: int, symbol: str) -> str:
    """Remove a symbol from a user's personal watchlist."""
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    if chat_id not in _user_watchlists:
        return f"{symbol} wasn't on your watchlist."
    before = len(_user_watchlists[chat_id])
    _user_watchlists[chat_id] = [
        w for w in _user_watchlists[chat_id] if w["symbol"] != symbol
    ]
    if len(_user_watchlists[chat_id]) < before:
        return f"✅ Stopped monitoring {symbol}."
    return f"{symbol} wasn't on your watchlist."


def clear_all_monitors(chat_id: int) -> str:
    """Remove all monitors for a user."""
    if chat_id in _user_watchlists:
        count = len(_user_watchlists[chat_id])
        _user_watchlists[chat_id] = []
        return f"✅ Cleared {count} monitor(s). Your watchlist is now empty."
    return "Your watchlist was already empty."
    """Return a formatted list of what's being monitored for a user."""
    watches = _user_watchlists.get(chat_id, [])
    if not watches:
        return "Your watchlist is empty. Say 'monitor BTCUSDT on 1H for sweeps' to add one."
    lines = ["<b>Your active monitors:</b>\n"]
    for w in watches:
        conds = ", ".join(w["conditions"])
        lines.append(f"• <b>{w['symbol']}</b> — {w['tf'].upper()} — {conds}")
        if w.get("note"):
            lines.append(f"  {w['note']}")
    return "\n".join(lines)


# Alert dedup — cooldowns per event type
SWEEP_COOLDOWN = int(os.environ.get("SWEEP_COOLDOWN", "3600"))   # 1 hour
BOS_COOLDOWN   = int(os.environ.get("BOS_COOLDOWN",   "14400"))  # 4 hours — a BOS doesn't expire
CRIME_COOLDOWN = int(os.environ.get("CRIME_COOLDOWN", "1800"))   # 30 min for crime moves

def _is_cooled_down(key: str, cooldown: int = None) -> bool:
    """Check if enough time has passed since last alert for this key."""
    if cooldown is None:
        cooldown = ALERT_COOLDOWN
    last = _alert_cooldowns.get(key, 0)
    return time.time() - last > cooldown


def _mark_alerted(key: str):
    _alert_cooldowns[key] = time.time()


def _persist_state():
    """Save cooldowns + watchlists to 0G Storage. Fire-and-forget."""
    try:
        from app.zg_storage import save_state
        save_state({
            "cooldowns":  _alert_cooldowns,
            "watchlists": {str(k): v for k, v in _user_watchlists.items()},
        })
    except Exception as e:
        log.debug(f"scanner: _persist_state failed — {e}")


def _send_alert(text: str, alert_type: str = "alert", symbol: str = ""):
    if _send_fn and _chat_id:
        try:
            _send_fn(_chat_id, text)
        except Exception as e:
            log.error(f"scanner: alert send failed — {e}")

    # ── Log to 0G Storage (decentralized intelligence history) ───────────
    try:
        from app.zg_storage import append_alert_log
        append_alert_log({
            "type":    alert_type,
            "symbol":  symbol,
            "message": text[:500],
            "ts":      datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass


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
        from app.mexc_data import scan_for_crimes, get_funding_rate
        crimes = scan_for_crimes(top_n=5)
        if not crimes:
            log.info("scanner: MEXC scan clean — no crimes detected")
            return

        for crime in crimes:
            sym   = crime["symbol"]
            chg   = crime["change_pct"]
            score = crime["crime_score"]
            key   = f"crime_{sym}_{int(abs(chg) / 10)}"

            if not _is_cooled_down(key, CRIME_COOLDOWN):
                continue

            direction = crime["direction"]
            severity  = crime["severity"]
            rvol      = crime["rvol"]
            rng       = crime["range_pct"]

            # ── MEXC funding rate — critical context for crime assessment ──
            # Positive funding on a pump = longs paying heavily = reversal risk
            # Negative funding on a dump = shorts paying = potential squeeze
            # This is the data Binance can't give us for MEXC-only pairs
            funding_line = ""
            try:
                fd = get_funding_rate(sym)
                if fd.get("ok"):
                    rate     = fd["funding_rate"]
                    rate_pct = f"{rate * 100:+.4f}%"
                    next_ms  = fd.get("next_settle_time", 0)
                    now_ms   = int(time.time() * 1000)
                    mins     = max(0, (next_ms - now_ms) // 60000) if next_ms else 999

                    if direction == "PUMP" and rate > 0.001:
                        funding_line = f"⚠️ Funding: {rate_pct} (longs paying — reversal risk at settlement in {mins}min)"
                    elif direction == "PUMP" and rate < -0.0005:
                        funding_line = f"🔥 Funding: {rate_pct} (shorts paying — fuel for continuation, settle {mins}min)"
                    elif direction == "DUMP" and rate < -0.001:
                        funding_line = f"⚠️ Funding: {rate_pct} (shorts paying — squeeze risk at settlement in {mins}min)"
                    elif direction == "DUMP" and rate > 0.0005:
                        funding_line = f"🔥 Funding: {rate_pct} (longs paying — fuel for dump, settle {mins}min)"
                    else:
                        funding_line = f"Funding: {rate_pct} (settle {mins}min)"
            except Exception as _fe:
                log.debug(f"funding fetch failed for {sym}: {_fe}")

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
            if funding_line:
                msg_lines.append(funding_line)
            if explanation:
                msg_lines += ["", explanation]
            msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")

            _send_alert("\n".join(msg_lines), alert_type="crime", symbol=sym)
            _mark_alerted(key)
            _persist_state()
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
                    sweep_time = sweep.get("open_time", "")[:16]
                    key = f"sweep_{symbol}_{round(sweep['level'], 2)}_{sweep_time}"
                    if _is_cooled_down(key, SWEEP_COOLDOWN):
                        explanation  = _explain_sweep(symbol, sweep, snap["bias_data"])
                        displacement = snap.get("displacement", {})
                        order_block  = snap.get("order_block", {})
                        sym_short    = symbol.replace("USDT", "")

                        indu_lines = []
                        for z in snap.get("inducement_zones", [])[:2]:
                            indu_lines.append(
                                f"  • {z['price']:.4f}  ({z['distance_pct']:+.1f}%)  — {z['note']}"
                            )

                        msg_lines = [
                            f"🎯 LIQUIDITY SWEEP — {sym_short}",
                            f"",
                            f"{sweep['description']}",
                            f"HTF Bias: {bias.upper()}",
                            f"Depth: {sweep['depth_pct']:.2f}%  ATR: {sweep['depth_atr']:.1f}×",
                        ]

                        if displacement.get("confirmed"):
                            msg_lines.append(f"✅ {displacement['description']}")
                            if order_block.get("found"):
                                msg_lines.append(
                                    f"📦 OB: {order_block['low']:.4f}–{order_block['high']:.4f}"
                                )
                        else:
                            msg_lines.append(f"⚠️ {displacement.get('description', 'Displacement not confirmed')}")

                        if indu_lines:
                            msg_lines += ["", "Inducement zones:"] + indu_lines
                        if explanation:
                            msg_lines += ["", explanation]
                        msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")

                        _send_alert("\n".join(msg_lines), alert_type="sweep", symbol=symbol)
                        _mark_alerted(key)
                        _persist_state()
                        log.info(f"scanner: sweep alert sent for {symbol}")

                # ── Alert on fresh BOS (within last 5 H1 candles) ────────
                if bos.get("broken") and bos.get("candle_idx", 0) >= len(candles_h1) - 2:
                    bos_bias = bos.get("bias", "")
                    bos_time = bos.get("open_time", "")[:16]
                    key = f"bos_{symbol}_{round(bos.get('level', 0), 2)}_{bos_bias}_{bos_time}"
                    if _is_cooled_down(key, BOS_COOLDOWN):
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
                        _send_alert("\n".join(msg_lines), alert_type="bos", symbol=symbol)
                        _mark_alerted(key)
                        _persist_state()
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
        _run_user_watchlist_scan()
        time.sleep(STRUCTURE_SCAN_INTERVAL)


def _run_user_watchlist_scan():
    """Scan each user's personal watchlist and alert that specific user."""
    if not _user_watchlists:
        return
    try:
        from app.market_data import get_klines
        from app.structure import full_structure_snapshot

        for chat_id, watches in _user_watchlists.items():
            for watch in watches:
                symbol = watch["symbol"]
                tf     = watch.get("tf", "1h")
                conds  = watch.get("conditions", ["sweep", "bos"])
                try:
                    candles_h1    = get_klines(symbol, tf,   50)
                    candles_h4    = get_klines(symbol, "4h", 50)
                    candles_daily = get_klines(symbol, "1d", 30)
                    snap = full_structure_snapshot(symbol, candles_h1, candles_h4, candles_daily)

                    bias   = snap.get("bias", "neutral")
                    sweep  = snap.get("recent_sweep")
                    bos    = snap.get("bos_h1", {})
                    indu   = snap.get("inducement_zones", [])
                    price  = snap.get("current_price", 0)

                    if "sweep" in conds and sweep and sweep.get("age_candles", 99) <= 3:
                        sweep_time = sweep.get("open_time", "")[:16]
                        key = f"user_{chat_id}_sweep_{symbol}_{round(sweep['level'], 2)}_{sweep_time}"
                        if _is_cooled_down(key, SWEEP_COOLDOWN):
                            explanation = _explain_sweep(symbol, sweep, snap["bias_data"])
                            sym_short = symbol.replace("USDT", "")
                            msg_lines = [
                                f"🎯 <b>YOUR WATCHLIST ALERT — {sym_short}</b>",
                                f"",
                                f"{sweep['description']}",
                                f"Bias: <b>{bias.upper()}</b>  |  Price: {price:.4f}",
                            ]
                            if indu:
                                z = indu[0]
                                msg_lines.append(f"Next inducement: {z['price']:.4f} ({z['distance_pct']:+.1f}%)")
                            if explanation:
                                msg_lines += ["", explanation]
                            if watch.get("note"):
                                msg_lines.append(f"\n📌 Your note: {watch['note']}")
                            msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                            if _send_fn:
                                _send_fn(chat_id, "\n".join(msg_lines))
                            _mark_alerted(key)

                    if "bos" in conds and bos.get("broken") and bos.get("candle_idx", 0) >= len(candles_h1) - 2:
                        bos_bias = bos.get("bias", "")
                        bos_time = bos.get("open_time", "")[:16]
                        key = f"user_{chat_id}_bos_{symbol}_{round(bos.get('level',0),2)}_{bos_bias}_{bos_time}"
                        if _is_cooled_down(key, BOS_COOLDOWN):
                            sym_short = symbol.replace("USDT", "")
                            msg_lines = [
                                f"⚡ <b>YOUR WATCHLIST ALERT — {sym_short}</b>",
                                f"",
                                f"{bos.get('description', '')}",
                                f"HTF Bias: <b>{bias.upper()}</b>",
                                f"Alignment: {'✅ WITH bias' if bos_bias == bias else '⚠️ AGAINST bias'}",
                            ]
                            if indu:
                                z = indu[0]
                                msg_lines.append(f"Next inducement: {z['price']:.4f} ({z['distance_pct']:+.1f}%)")
                            if watch.get("note"):
                                msg_lines.append(f"\n📌 Your note: {watch['note']}")
                            msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                            if _send_fn:
                                _send_fn(chat_id, "\n".join(msg_lines))
                            _mark_alerted(key)

                    # ── Funding rate monitoring ───────────────────────────
                    funding_conds = [c for c in conds if c.startswith("funding")]
                    if funding_conds:
                        try:
                            from app.mexc_data import get_funding_rate
                            fd = get_funding_rate(symbol)
                            if fd.get("ok"):
                                rate = fd["funding_rate"]
                                next_ms = fd.get("next_settle_time", 0)
                                now_ms  = int(time.time() * 1000)
                                mins    = max(0, (next_ms - now_ms) // 60000) if next_ms else 999
                                sym_short = symbol.replace("USDT", "").replace("_USDT", "")

                                fire = False
                                rate_desc = ""
                                if "funding_negative" in funding_conds and rate < 0:
                                    fire = True
                                    rate_desc = f"Funding went NEGATIVE: {rate*100:+.4f}% (shorts paying)"
                                elif "funding_positive" in funding_conds and rate > 0.001:
                                    fire = True
                                    rate_desc = f"Funding HIGH POSITIVE: {rate*100:+.4f}% (longs paying)"
                                elif "funding_change" in funding_conds and abs(rate) > 0.0005:
                                    fire = True
                                    rate_desc = f"Funding notable: {rate*100:+.4f}%"

                                if fire:
                                    key = f"user_{chat_id}_funding_{symbol}_{rate > 0}"
                                    if _is_cooled_down(key, 3600):
                                        msg = (
                                            f"💰 <b>YOUR FUNDING ALERT — {sym_short}</b>\n\n"
                                            f"{rate_desc}\n"
                                            f"Settlement in: {mins}min\n"
                                            f"{'Bearish pressure at settlement — longs may close' if rate > 0 else 'Short squeeze risk — shorts paying to hold'}"
                                        )
                                        if watch.get("note"):
                                            msg += f"\n\n📌 {watch['note']}"
                                        if _send_fn:
                                            _send_fn(chat_id, msg)
                                        _mark_alerted(key)
                                        _persist_state()
                        except Exception as _fe:
                            log.debug(f"funding monitor {symbol}: {_fe}")

                except Exception as e:
                    log.debug(f"user watchlist scan {symbol} for {chat_id}: {e}")
    except Exception as e:
        log.error(f"_run_user_watchlist_scan error: {e}")


def start_scanner(send_fn, chat_id: int):
    """
    Start the background scanner threads.
    Restores prior state from 0G Storage on startup.
    """
    global _send_fn, _chat_id, _running, _alert_cooldowns, _user_watchlists

    _send_fn = send_fn
    _chat_id = chat_id

    # ── Restore state from 0G Storage ────────────────────────────────────
    # This is what makes Scout's memory survive restarts and deploys.
    # Cooldowns, watchlists, and alert history are all persisted on 0G.
    try:
        from app.zg_storage import load_state
        prior = load_state()
        if prior:
            _alert_cooldowns  = prior.get("cooldowns",   {})
            _user_watchlists  = prior.get("watchlists",  {})
            log.info(
                f"scanner: restored from 0G Storage — "
                f"{len(_alert_cooldowns)} cooldowns, "
                f"{sum(len(v) for v in _user_watchlists.values())} user monitors"
            )
        else:
            log.info("scanner: no prior 0G Storage state — starting fresh")
    except Exception as e:
        log.warning(f"scanner: state restore failed — {e} — starting fresh")

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
