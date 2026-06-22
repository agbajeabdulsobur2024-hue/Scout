"""
scanner.py — Scout's proactive intelligence scanner.

Runs as a background thread. Every N minutes it:
  1. Scans MEXC for crime (coordinated pumps/dumps)
  2. Scans Binance watchlist using the Intelligence Engine
  3. Ranks opportunities by score
  4. If something fires, asks 0G Compute to explain it
  5. Sends a Telegram alert unprompted

The Intelligence Engine runs FIRST. 0G Compute explains FACTS.
Not the other way around.
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger("scout")

MEXC_SCAN_INTERVAL      = int(os.environ.get("MEXC_SCAN_INTERVAL",  "300"))   # 5 min
STRUCTURE_SCAN_INTERVAL = int(os.environ.get("STRUCTURE_SCAN_INTERVAL", "600")) # 10 min
ALERT_COOLDOWN          = int(os.environ.get("ALERT_COOLDOWN", "1800"))  # 30 min

# Crypto USDT perps to watch for structure
STRUCTURE_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "AVAXUSDT",
    "LINKUSDT", "ADAUSDT", "DOTUSDT", "MATICUSDT", "XRPUSDT",
]

# Min score for a proactive alert (filters noise)
PROACTIVE_ALERT_SCORE = 60

_alert_cooldowns: dict = {}
_send_fn = None
_chat_id = None
_running = False

_user_watchlists: dict = {}

SWEEP_COOLDOWN = int(os.environ.get("SWEEP_COOLDOWN", "3600"))
BOS_COOLDOWN   = int(os.environ.get("BOS_COOLDOWN",   "14400"))
CRIME_COOLDOWN = int(os.environ.get("CRIME_COOLDOWN", "1800"))


def add_user_monitor(chat_id: int, symbol: str, tf: str = "15m",
                     conditions: list = None, note: str = "",
                     entry_tf: str = "15m", confirm_tf: str = "1h",
                     bias_tf: str = "4h") -> str:
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"

    entry = {
        "symbol":     symbol,
        "conditions": conditions or ["sweep", "bos"],
        "note":       note,
        "entry_tf":   entry_tf,
        "confirm_tf": confirm_tf,
        "bias_tf":    bias_tf,
    }
    if chat_id not in _user_watchlists:
        _user_watchlists[chat_id] = []

    existing = [w["symbol"] for w in _user_watchlists[chat_id]]
    if symbol in existing:
        return f"{symbol} is already on your watchlist."

    _user_watchlists[chat_id].append(entry)
    log.info(f"scanner: {chat_id} added {symbol} to watchlist")
    threading.Thread(target=_persist_state, daemon=True).start()

    tf_desc = f"Entry: {entry_tf.upper()} | Confirm: {confirm_tf.upper()} | Bias: {bias_tf.upper()}"
    return (
        f"✅ Now monitoring <b>{symbol}</b>\n"
        f"TF stack: {tf_desc}\n"
        f"Watching for: {', '.join(entry['conditions'])}"
        + (f"\n📌 {note}" if note else "")
    )


def remove_user_monitor(chat_id: int, symbol: str) -> str:
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
    if chat_id in _user_watchlists:
        count = len(_user_watchlists[chat_id])
        _user_watchlists[chat_id] = []
        return f"✅ Cleared {count} monitor(s). Your watchlist is now empty."
    return "Your watchlist was already empty."


def list_user_monitors(chat_id: int) -> str:
    watches = _user_watchlists.get(chat_id, [])
    if not watches:
        return "Your watchlist is empty. Say 'monitor BTCUSDT on 1H for sweeps' to add one."
    lines = ["<b>Your active monitors:</b>\n"]
    for w in watches:
        conds = ", ".join(w["conditions"])
        lines.append(f"• <b>{w['symbol']}</b> — {w.get('entry_tf','1h').upper()} — {conds}")
        if w.get("note"):
            lines.append(f"  {w['note']}")
    return "\n".join(lines)


def _is_cooled_down(key: str, cooldown: int = None) -> bool:
    if cooldown is None:
        cooldown = ALERT_COOLDOWN
    last = _alert_cooldowns.get(key, 0)
    return time.time() - last > cooldown


def _mark_alerted(key: str):
    _alert_cooldowns[key] = time.time()


def _persist_state():
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
    """Ask 0G Compute to explain a suspicious MEXC move using structured intelligence."""
    try:
        from app.reasoning import explain_crime_move
        return explain_crime_move(crime)
    except Exception as e:
        log.debug(f"_explain_crime failed: {e}")
        return ""


def _explain_intel_alert(symbol: str, intel: dict) -> str:
    """
    Ask 0G Compute to explain a proactive alert based on full intelligence.
    This is the key improvement — 0G gets FACTS, not raw candles.
    """
    try:
        from app.zg_compute import ask
        from app.intelligence import format_intelligence_for_model

        context = format_intelligence_for_model(intel)
        question = (
            "A high-scoring SMC setup just formed. Based on this intelligence:\n"
            "1. What exactly triggered this alert?\n"
            "2. Is this a quality setup or noise?\n"
            "3. What confirms or invalidates it?\n"
            "Keep it under 100 words."
        )

        from app.reasoning import SYSTEM_PROMPT
        return ask([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"{context}\n\n{question}"},
        ], max_tokens=160)
    except Exception as e:
        log.debug(f"_explain_intel_alert failed: {e}")
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
                        funding_line = f"⚠️ Funding: {rate_pct} (longs paying — reversal risk in {mins}min)"
                    elif direction == "PUMP" and rate < -0.0005:
                        funding_line = f"🔥 Funding: {rate_pct} (shorts paying — fuel for continuation, {mins}min)"
                    elif direction == "DUMP" and rate < -0.001:
                        funding_line = f"⚠️ Funding: {rate_pct} (shorts paying — squeeze risk in {mins}min)"
                    elif direction == "DUMP" and rate > 0.0005:
                        funding_line = f"🔥 Funding: {rate_pct} (longs paying — fuel for dump, {mins}min)"
                    else:
                        funding_line = f"Funding: {rate_pct} (settle {mins}min)"
            except Exception as _fe:
                log.debug(f"funding fetch failed for {sym}: {_fe}")

            explanation = _explain_crime(crime)

            msg_lines = [
                f"{severity} — MEXC CRIME DETECTED",
                f"",
                f"Symbol: {sym.replace('_USDT', '')}/USDT",
                f"Move: {chg:+.2f}%  ({direction})",
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
            log.info(f"scanner: crime alert sent for {sym} ({chg:+.2f}%)")

    except Exception as e:
        log.error(f"scanner: MEXC crime scan error — {e}")


def run_structure_scan():
    """
    Intelligence-driven structure scan.
    Builds full intelligence for each symbol, scores it, alerts if score >= threshold.
    """
    log.info("scanner: running intelligence-driven structure scan...")
    try:
        from app.market_data import get_klines
        from app.mexc_data import get_funding_rate as mexc_funding
        from app.intelligence import build_intelligence

        for symbol in STRUCTURE_WATCHLIST:
            try:
                c_m15   = get_klines(symbol, "15m", 96)
                c_h1    = get_klines(symbol, "1h",  50)
                c_h4    = get_klines(symbol, "4h",  50)
                c_daily = get_klines(symbol, "1d",  30)

                if not c_h1:
                    continue

                funding_rate = 0.0
                try:
                    fd = mexc_funding(symbol)
                    if fd.get("ok"):
                        funding_rate = fd["funding_rate"]
                except Exception:
                    pass

                # Build full intelligence
                intel = build_intelligence(
                    symbol        = symbol,
                    candles_m15   = c_m15,
                    candles_h1    = c_h1,
                    candles_h4    = c_h4,
                    candles_daily = c_daily,
                    funding_rate  = funding_rate,
                )

                score     = intel.get("score", 0)
                sym_short = symbol.replace("USDT", "")
                sweep     = intel.get("sweep", {})
                disp      = intel.get("displacement", {})
                bos       = intel.get("bos", {})
                bias      = intel.get("htf_bias", "Neutral")
                regime    = intel.get("regime", "Unknown")
                session   = intel.get("session", {})
                ob        = intel.get("order_block", {})
                fvg       = intel.get("fvg", {})

                # ── Sweep alert (high score + recent sweep) ────────────────
                if sweep.get("detected") and sweep.get("age_candles", 99) <= 3:
                    sweep_key = f"sweep_{symbol}_{round(sweep.get('level', 0), 2)}"
                    if _is_cooled_down(sweep_key, SWEEP_COOLDOWN):
                        explanation = _explain_intel_alert(symbol, intel) if score >= PROACTIVE_ALERT_SCORE else ""

                        msg_lines = [
                            f"🎯 M15/H1 SWEEP — {sym_short}",
                            f"",
                            f"{sweep.get('description', '')}",
                            f"Score: {score}/100  |  Regime: {regime}",
                            f"HTF Bias: {bias}",
                        ]

                        if disp.get("confirmed"):
                            msg_lines.append(f"✅ {disp.get('description', 'Displacement confirmed')}")
                        else:
                            msg_lines.append("⏳ No displacement yet — wait for confirmation")

                        if bos.get("broken"):
                            msg_lines.append(f"⚡ BOS: {bos.get('description', '')}")

                        if ob.get("found"):
                            msg_lines.append(f"📦 OB: {ob['low']:.4f}–{ob['high']:.4f}")

                        if fvg.get("found"):
                            msg_lines.append(f"📊 FVG: {fvg['low']:.4f}–{fvg['high']:.4f}")

                        if session.get("in_killzone"):
                            msg_lines.append(f"⏰ {session.get('description', '')}")

                        intel_fund = intel.get("funding", {})
                        if intel_fund.get("note"):
                            msg_lines.append(f"💰 {intel_fund['note']}")

                        if explanation:
                            msg_lines += ["", explanation]

                        msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                        _send_alert("\n".join(msg_lines), alert_type="sweep", symbol=symbol)
                        _mark_alerted(sweep_key)
                        _persist_state()
                        log.info(f"scanner: sweep alert — {symbol} (score {score})")

                # ── BOS alert ────────────────────────────────────────────────
                if bos.get("broken") and not sweep.get("detected"):
                    bos_key = f"bos_{symbol}_{round(bos.get('level', 0), 2)}_{bos.get('bias','')}"
                    if _is_cooled_down(bos_key, BOS_COOLDOWN):
                        msg_lines = [
                            f"⚡ H1 BOS — {sym_short}",
                            f"",
                            f"{bos.get('description', '')}",
                            f"Regime: {regime}  |  Score: {score}/100",
                            f"HTF Bias: {bias}",
                            f"👀 Watch M15 for sweep + displacement entry",
                        ]
                        indu = intel.get("inducements", [])
                        if indu:
                            z = indu[0]
                            msg_lines.append(f"Next inducement: {z['price']:.4f} ({z['distance_pct']:+.1f}%)")
                        msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                        _send_alert("\n".join(msg_lines), alert_type="bos", symbol=symbol)
                        _mark_alerted(bos_key)
                        _persist_state()
                        log.info(f"scanner: BOS alert — {symbol}")

            except Exception as e:
                log.debug(f"scanner: structure scan error for {symbol} — {e}")
                continue

    except Exception as e:
        log.error(f"scanner: structure scan error — {e}")


def run_opportunity_ranking():
    """
    Periodically rank all watchlist symbols by opportunity score.
    If the top symbol exceeds the proactive threshold, send an alert.
    This is Phase 7 — opportunity ranking.
    """
    log.info("scanner: running opportunity ranking...")
    try:
        from app.market_data import get_klines
        from app.mexc_data import get_funding_rate as mexc_funding
        from app.intelligence import rank_opportunities

        candles_map = {}
        funding_map = {}

        for sym in STRUCTURE_WATCHLIST:
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

        top = rank_opportunities(STRUCTURE_WATCHLIST, candles_map, funding_map, top_n=3)
        if not top:
            return

        best = top[0]
        score = best["score"]

        if score < PROACTIVE_ALERT_SCORE:
            log.info(f"scanner: top opportunity {best['symbol']} score {score} below threshold")
            return

        key = f"ranking_{best['symbol']}_{score // 10 * 10}"
        if not _is_cooled_down(key, 3600):  # max once per hour per score band
            return

        # Build ranking message
        lines = ["🏆 <b>OPPORTUNITY RANKING UPDATE</b>\n"]
        for i, intel in enumerate(top, 1):
            sym    = intel["symbol"].replace("USDT", "")
            sc     = intel["score"]
            bias   = intel["htf_bias"]
            regime = intel["regime"]
            bar    = "🟢" if sc >= 75 else "🟡" if sc >= 50 else "🔴"
            sigs   = []
            if intel.get("sweep", {}).get("detected"):
                sigs.append("sweep")
            if intel.get("displacement", {}).get("confirmed"):
                sigs.append("disp")
            if intel.get("bos", {}).get("broken"):
                sigs.append("BOS")
            sig_str = "+".join(sigs) if sigs else "monitoring"
            lines.append(f"{bar} {i}. <b>{sym}</b>  {sc}/100  ({bias} | {regime}) — {sig_str}")

        lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
        _send_alert("\n".join(lines), alert_type="ranking", symbol=best["symbol"])
        _mark_alerted(key)
        _persist_state()
        log.info(f"scanner: opportunity ranking alert sent — top: {best['symbol']} {score}/100")

    except Exception as e:
        log.error(f"scanner: opportunity ranking error — {e}")


def _mexc_loop():
    while _running:
        run_mexc_crime_scan()
        time.sleep(MEXC_SCAN_INTERVAL)


def _structure_loop():
    time.sleep(60)
    scan_count = 0
    while _running:
        run_structure_scan()
        _run_user_watchlist_scan()
        scan_count += 1
        # Run opportunity ranking every 3rd structure scan (~30 min)
        if scan_count % 3 == 0:
            run_opportunity_ranking()
        time.sleep(STRUCTURE_SCAN_INTERVAL)


def _run_user_watchlist_scan():
    """Scan each user's personal watchlist using the Intelligence Engine."""
    if not _user_watchlists:
        return
    try:
        from app.market_data import get_klines
        from app.mexc_data import get_funding_rate as mexc_funding
        from app.intelligence import build_intelligence

        for chat_id, watches in _user_watchlists.items():
            for watch in watches:
                symbol     = watch["symbol"]
                conds      = watch.get("conditions", ["sweep", "bos"])
                entry_tf   = watch.get("entry_tf",   "15m")
                confirm_tf = watch.get("confirm_tf", "1h")
                bias_tf    = watch.get("bias_tf",    "4h")

                try:
                    c_entry   = get_klines(symbol, entry_tf,   96)
                    c_confirm = get_klines(symbol, confirm_tf, 50)
                    c_bias    = get_klines(symbol, bias_tf,    50)
                    c_daily   = get_klines(symbol, "1d",       30)

                    if not c_entry:
                        continue

                    funding_rate = 0.0
                    try:
                        fd = mexc_funding(symbol)
                        if fd.get("ok"):
                            funding_rate = fd["funding_rate"]
                    except Exception:
                        pass

                    intel     = build_intelligence(
                        symbol        = symbol,
                        candles_m15   = c_entry if entry_tf in ("15m", "5m") else [],
                        candles_h1    = c_confirm,
                        candles_h4    = c_bias,
                        candles_daily = c_daily,
                        funding_rate  = funding_rate,
                    )

                    score     = intel.get("score", 0)
                    sym_short = symbol.replace("USDT", "").replace("_USDT", "")
                    sweep     = intel.get("sweep", {})
                    bos       = intel.get("bos", {})
                    disp      = intel.get("displacement", {})
                    bias      = intel.get("htf_bias", "Neutral")
                    ob        = intel.get("order_block", {})

                    if "sweep" in conds and sweep.get("detected") and sweep.get("age_candles", 99) <= 3:
                        key = f"user_{chat_id}_sweep_{symbol}_{round(sweep.get('level', 0), 2)}"
                        if _is_cooled_down(key, SWEEP_COOLDOWN):
                            explanation = _explain_intel_alert(symbol, intel) if score >= 50 else ""
                            tf_label = f"{entry_tf.upper()} entry | {confirm_tf.upper()} | {bias_tf.upper()} bias"
                            msg_lines = [
                                f"🎯 <b>YOUR WATCHLIST — {sym_short} SWEEP</b>",
                                f"",
                                f"{sweep.get('description', '')}",
                                f"Score: {score}/100  |  Bias: <b>{bias}</b>",
                                f"TF stack: {tf_label}",
                            ]
                            if disp.get("confirmed"):
                                msg_lines.append(f"✅ {disp.get('description', '')}")
                                if ob.get("found"):
                                    msg_lines.append(f"📦 OB: {ob['low']:.4f}–{ob['high']:.4f}")
                            else:
                                msg_lines.append("⏳ No displacement yet")
                            if explanation:
                                msg_lines += ["", explanation]
                            if watch.get("note"):
                                msg_lines.append(f"\n📌 {watch['note']}")
                            msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                            if _send_fn:
                                _send_fn(chat_id, "\n".join(msg_lines))
                            _mark_alerted(key)
                            _persist_state()

                    if "bos" in conds and bos.get("broken"):
                        bos_key = f"user_{chat_id}_bos_{symbol}_{round(bos.get('level', 0), 2)}"
                        if _is_cooled_down(bos_key, BOS_COOLDOWN):
                            msg_lines = [
                                f"⚡ <b>YOUR WATCHLIST — {sym_short} BOS</b>",
                                f"",
                                f"{bos.get('description', '')}",
                                f"Score: {score}/100  |  Bias: <b>{bias}</b>",
                                f"👀 Watch {entry_tf.upper()} for entry sweep",
                            ]
                            indu = intel.get("inducements", [])
                            if indu:
                                z = indu[0]
                                msg_lines.append(f"Next inducement: {z['price']:.4f} ({z['distance_pct']:+.1f}%)")
                            if watch.get("note"):
                                msg_lines.append(f"\n📌 {watch['note']}")
                            msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                            if _send_fn:
                                _send_fn(chat_id, "\n".join(msg_lines))
                            _mark_alerted(bos_key)
                            _persist_state()

                    # Funding monitors
                    funding_conds = [c for c in conds if c.startswith("funding")]
                    if funding_conds:
                        try:
                            from app.mexc_data import get_funding_rate
                            fd = get_funding_rate(symbol)
                            if fd.get("ok"):
                                rate    = fd["funding_rate"]
                                next_ms = fd.get("next_settle_time", 0)
                                now_ms  = int(time.time() * 1000)
                                mins    = max(0, (next_ms - now_ms) // 60000) if next_ms else 999
                                fire     = False
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
                                            f"{'Bearish pressure at settlement' if rate > 0 else 'Short squeeze risk'}"
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
    global _send_fn, _chat_id, _running, _alert_cooldowns, _user_watchlists

    _send_fn = send_fn
    _chat_id = chat_id

    try:
        from app.zg_storage import load_state
        prior = load_state()
        if prior:
            _alert_cooldowns = prior.get("cooldowns",   {})
            _user_watchlists = prior.get("watchlists",  {})
            log.info(
                f"scanner: restored from 0G Storage — "
                f"{len(_alert_cooldowns)} cooldowns, "
                f"{sum(len(v) for v in _user_watchlists.values())} user monitors"
            )
        else:
            log.info("scanner: no prior 0G Storage state — starting fresh")
    except Exception as e:
        log.warning(f"scanner: state restore failed — {e}")

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
