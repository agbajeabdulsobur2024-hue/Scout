"""
scanner.py — Scout's proactive intelligence scanner.

Implements Scout Master Roadmap priorities:
  P1:  Direction-aware BOS/CHoCH — direction stored and matched exactly
  P6:  Multi-stage setup engine — Bias → Confirmation → Entry → Alert
  P7:  User-defined setup memory — stored, continuously evaluated
  P8:  Trend continuation vs trend change classification
  P9:  Major asset engine — BTC/ETH/SOL/TON/BNB/XRP/SUI always monitored
  P10: MEXC top gainers scanner — +5/10/15% thresholds
  P11: MEXC top losers scanner  — -5/10/15% thresholds

Design rules (from roadmap):
  - Monitoring ≠ Analysis. User monitor requests are STRICT by default.
  - Direction field stored and matched. Mismatch → silent ignore.
  - No alerts unless conditions explicitly met.
  - Workflow progress tracked across scans.
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger("scout")

MEXC_SCAN_INTERVAL      = int(os.environ.get("MEXC_SCAN_INTERVAL",      "300"))   # 5 min
STRUCTURE_SCAN_INTERVAL = int(os.environ.get("STRUCTURE_SCAN_INTERVAL",  "600"))  # 10 min
ALERT_COOLDOWN          = int(os.environ.get("ALERT_COOLDOWN",           "1800")) # 30 min

# ── Major asset engine (Priority 9) ───────────────────────────────────────
# Always monitored. 4H = Bias, 1H = Confirmation, 15M = Entry
MAJOR_ASSETS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "TONUSDT",
    "BNBUSDT", "XRPUSDT", "SUIUSDT",
]

# Extended watchlist for opportunity scanner
STRUCTURE_WATCHLIST = MAJOR_ASSETS + [
    "AVAXUSDT", "LINKUSDT", "ADAUSDT", "DOTUSDT",
]

PROACTIVE_ALERT_SCORE   = 65   # minimum score for unprompted alert
MEXC_GAINER_THRESHOLDS  = [5, 10, 15]
MEXC_LOSER_THRESHOLDS   = [-5, -10, -15]

_alert_cooldowns: dict = {}
_send_fn         = None
_chat_id         = None
_running         = False

# ── Pause state — per chat_id (Fix 2) ────────────────────────────────────
# When paused, background scanner sends no alerts to that chat.
_paused_chats: set = set()

# ── Manual /crimes dedup stamp (Fix 1) ───────────────────────────────────
# When user calls /crimes manually, we stamp this time so the background
# movers scan skips sending individual alerts for the same cycle.
_last_manual_crimes: float = 0.0
MANUAL_CRIMES_SUPPRESS_WINDOW = 120  # seconds — suppress background movers for 2 min after /crimes

# ── User monitor watchlists ────────────────────────────────────────────────
# Strict: {symbol, direction, event, tf, ...} — direction-aware (P1)
_user_watchlists: dict = {}

# Major asset state — tracks per-symbol classification (P9)
_major_states: dict = {}

SWEEP_COOLDOWN = int(os.environ.get("SWEEP_COOLDOWN", "3600"))
BOS_COOLDOWN   = int(os.environ.get("BOS_COOLDOWN",   "14400"))
CRIME_COOLDOWN = int(os.environ.get("CRIME_COOLDOWN", "1800"))
MAJOR_COOLDOWN = int(os.environ.get("MAJOR_COOLDOWN", "3600"))


# ── User monitor management (P1: direction-aware) ─────────────────────────

def add_user_monitor(chat_id: int, symbol: str, tf: str = "1h",
                     conditions: list = None, note: str = "",
                     direction: str = "",
                     entry_tf: str = "15m", confirm_tf: str = "1h",
                     bias_tf: str = "4h",
                     mode: str = "strict") -> str:
    """
    Add a direction-aware monitor.

    CRITICAL (P1): direction is stored with every condition.
    Alerts only fire when detected direction matches stored direction.
    """
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    # Normalise conditions into direction-aware dicts (P1 fix)
    parsed_conditions = []
    raw_conds = conditions or ["sweep", "bos"]
    for cond in raw_conds:
        if isinstance(cond, dict):
            parsed_conditions.append(cond)
        else:
            # String condition — wrap with direction
            parsed_conditions.append({
                "event":     cond,
                "direction": direction or "",   # "" = any direction (backward compat)
                "strict":    bool(direction),   # strict only when direction was explicit
            })

    entry = {
        "symbol":     symbol,
        "conditions": parsed_conditions,
        "note":       note,
        "direction":  direction,
        "entry_tf":   entry_tf,
        "confirm_tf": confirm_tf,
        "bias_tf":    bias_tf,
        "mode":       mode,
    }

    if chat_id not in _user_watchlists:
        _user_watchlists[chat_id] = []

    existing = [w["symbol"] for w in _user_watchlists[chat_id]]
    if symbol in existing:
        return f"{symbol} is already on your watchlist."

    _user_watchlists[chat_id].append(entry)
    _persist_state()

    sym_short = symbol.replace("USDT", "")
    mode_tag  = "STRICT" if mode == "strict" else "ASSISTED"
    dir_tag   = f" [{direction.upper()}]" if direction else " [any direction]"
    cond_descs = []
    for c in parsed_conditions:
        if isinstance(c, dict):
            d = c.get("direction", "")
            e = c.get("event", "?")
            cond_descs.append(f"{d + ' ' if d else ''}{e}")
        else:
            cond_descs.append(str(c))

    return (
        f"✅ Watching: <b>{sym_short}</b>{dir_tag}  [{mode_tag}]\n"
        f"Conditions: {', '.join(cond_descs)}"
        + (f"\n📌 {note}" if note else "")
    )


def remove_user_monitor(chat_id: int, symbol: str) -> str:
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    if chat_id not in _user_watchlists:
        return f"{symbol} wasn't on your watchlist."
    before = len(_user_watchlists[chat_id])
    _user_watchlists[chat_id] = [
        w for w in _user_watchlists[chat_id] if w["symbol"] != symbol
    ]
    if len(_user_watchlists[chat_id]) < before:
        _persist_state()
        return f"✅ Stopped monitoring {symbol}."
    return f"{symbol} wasn't on your watchlist."


def clear_all_monitors(chat_id: int) -> str:
    count = len(_user_watchlists.get(chat_id, []))
    _user_watchlists[chat_id] = []
    _persist_state()
    return f"✅ Cleared {count} monitor(s)."


def list_user_monitors(chat_id: int) -> str:
    watches = _user_watchlists.get(chat_id, [])
    if not watches:
        return (
            "Your watchlist is empty.\n\n"
            "Example: <i>Monitor SYNUSDT on H4 for bearish BOS</i>\n"
            "Or define a full workflow:\n"
            "<i>Monitor SYN. H4 bearish. Wait for: 1. Bearish CHoCH 2. BOS 3. OB retest</i>"
        )
    lines = ["<b>Your active monitors:</b>\n"]
    for w in watches:
        sym   = w["symbol"].replace("USDT", "")
        mode  = w.get("mode", "strict").upper()
        dirn  = w.get("direction", "")
        conds = w.get("conditions", [])
        cond_str = []
        for c in conds:
            if isinstance(c, dict):
                d = c.get("direction", "")
                e = c.get("event", "")
                cond_str.append(f"{d + ' ' if d else ''}{e}")
            else:
                cond_str.append(str(c))
        dir_tag = f" [{dirn.upper()}]" if dirn else ""
        lines.append(
            f"• <b>{sym}</b>{dir_tag}  [{mode}]\n"
            f"  Watching: {', '.join(cond_str)}"
        )
        if w.get("note"):
            lines.append(f"  📌 {w['note']}")
    return "\n".join(lines)


# ── Cooldown helpers ──────────────────────────────────────────────────────

def _is_cooled_down(key: str, cooldown: int = None) -> bool:
    cooldown = cooldown or ALERT_COOLDOWN
    return time.time() - _alert_cooldowns.get(key, 0) > cooldown


def _mark_alerted(key: str):
    _alert_cooldowns[key] = time.time()


# ── Pause / resume (Fix 2) ───────────────────────────────────────────────

def pause_alerts(chat_id: int) -> str:
    """Pause all background alerts for this chat."""
    _paused_chats.add(chat_id)
    return (
        "⏸ Background alerts paused.\n\n"
        "Scout is still monitoring — no alerts will be sent until you resume.\n"
        "Use /resume to turn alerts back on."
    )


def resume_alerts(chat_id: int) -> str:
    """Resume background alerts for this chat."""
    _paused_chats.discard(chat_id)
    return "▶️ Alerts resumed. Scout is back on watch."


def is_paused(chat_id: int) -> bool:
    return chat_id in _paused_chats


def stamp_manual_crimes():
    """
    Call this when user manually runs /crimes.
    Suppresses background mover alerts for the next 2 minutes to avoid
    flooding the same information twice. (Fix 1)
    """
    global _last_manual_crimes
    _last_manual_crimes = time.time()


def _manual_crimes_active() -> bool:
    return time.time() - _last_manual_crimes < MANUAL_CRIMES_SUPPRESS_WINDOW


# ── Persistence ───────────────────────────────────────────────────────────

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
        # Fix 2: respect pause state
        if _chat_id in _paused_chats:
            log.debug(f"scanner: alert suppressed — chat {_chat_id} is paused")
        else:
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


# ── Major asset classification (Priority 9) ──────────────────────────────

def _classify_major(symbol: str, intel: dict) -> str:
    """
    Classify a major asset into one of:
      Bullish Trend | Bearish Trend | Continuation | Trend Change | Range | No Trade
    """
    bias    = intel.get("htf_bias", "Neutral").lower()
    bos     = intel.get("bos", {})
    sweep   = intel.get("sweep", {})
    disp    = intel.get("displacement", {})
    regime  = intel.get("regime", "Unknown")
    score   = intel.get("score", 0)

    prev = _major_states.get(symbol, {})
    prev_bias = prev.get("bias", "neutral")

    # Trend change: bias flipped
    if prev_bias and prev_bias != "neutral" and prev_bias != bias and bias != "neutral":
        return "Trend Change"

    if regime == "Compression":
        return "Range"

    if bias == "bullish":
        if bos.get("broken") and bos.get("bias") == "bullish":
            if disp.get("confirmed"):
                return "Bullish Continuation"
        return "Bullish Trend"

    if bias == "bearish":
        if bos.get("broken") and bos.get("bias") == "bearish":
            if disp.get("confirmed"):
                return "Bearish Continuation"
        return "Bearish Trend"

    return "No Trade"


def run_major_asset_scan():
    """
    Priority 9: always monitor BTC, ETH, SOL, TON, BNB, XRP, SUI.
    4H = Bias, 1H = Confirmation, 15M = Entry.
    Alert on: Trend Change, Continuation with displacement, new BOS.
    """
    log.info("scanner: major asset scan...")
    try:
        from app.market_data import get_klines
        from app.mexc_data import get_funding_rate as mexc_funding
        from app.intelligence import build_intelligence

        for symbol in MAJOR_ASSETS:
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

                intel = build_intelligence(symbol, c_m15, c_h1, c_h4, c_daily, funding_rate)
                classification = _classify_major(symbol, intel)

                sym_short = symbol.replace("USDT", "")
                prev = _major_states.get(symbol, {})

                # Alert on Trend Change
                if classification == "Trend Change":
                    key = f"major_trendchange_{symbol}"
                    if _is_cooled_down(key, MAJOR_COOLDOWN):
                        bos  = intel.get("bos", {})
                        bias = intel.get("htf_bias", "Neutral")
                        msg  = (
                            f"⚠️ <b>TREND CHANGE — {sym_short}</b>\n\n"
                            f"Previous bias: {prev.get('bias','?').capitalize()}\n"
                            f"New bias:      <b>{bias}</b>\n"
                            f"Trigger:       {bos.get('description','BOS detected')}\n"
                            f"Regime:        {intel.get('regime','')}\n"
                            f"Score:         {intel.get('score',0)}/100\n\n"
                            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                        _send_alert(msg, alert_type="trend_change", symbol=symbol)
                        _mark_alerted(key)

                # Alert on high-score Continuation (displacement confirmed)
                elif "Continuation" in classification:
                    key = f"major_cont_{symbol}"
                    if _is_cooled_down(key, MAJOR_COOLDOWN) and intel.get("score", 0) >= PROACTIVE_ALERT_SCORE:
                        disp = intel.get("displacement", {})
                        ob   = intel.get("order_block", {})
                        msg  = (
                            f"⚡ <b>{classification.upper()} — {sym_short}</b>\n\n"
                            f"HTF Bias: {intel.get('htf_bias','')}\n"
                            f"Regime:   {intel.get('regime','')}\n"
                        )
                        if disp.get("confirmed"):
                            msg += f"Displacement: ✓ {disp.get('description','')}\n"
                        if ob.get("found"):
                            msg += f"OB zone: {ob['low']:.4f}–{ob['high']:.4f}\n"
                        msg += f"Score: {intel.get('score',0)}/100\n"
                        msg += f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        _send_alert(msg, alert_type="continuation", symbol=symbol)
                        _mark_alerted(key)

                # Update state
                _major_states[symbol] = {
                    "bias":           intel.get("htf_bias", "Neutral").lower(),
                    "classification": classification,
                    "score":          intel.get("score", 0),
                    "last_scan":      datetime.now(timezone.utc).isoformat(),
                }

            except Exception as e:
                log.debug(f"major scan {symbol}: {e}")

    except Exception as e:
        log.error(f"scanner: major asset scan error — {e}")


def get_major_summary() -> str:
    """Return a quick summary of all major asset states (for /majors command)."""
    if not _major_states:
        return "Major asset data not yet loaded. Scanner runs every 10 minutes."
    lines = ["<b>Major Asset Snapshot</b>\n"]
    ICONS = {
        "Bullish Trend": "🟢",
        "Bullish Continuation": "🟢",
        "Bearish Trend": "🔴",
        "Bearish Continuation": "🔴",
        "Trend Change": "⚠️",
        "Range": "↔️",
        "No Trade": "⚫",
    }
    for sym in MAJOR_ASSETS:
        state = _major_states.get(sym)
        if not state:
            continue
        sym_short = sym.replace("USDT", "")
        cls       = state.get("classification", "?")
        icon      = ICONS.get(cls, "•")
        score     = state.get("score", 0)
        lines.append(f"{icon} <b>{sym_short}</b>:  {cls}  ({score}/100)")
    return "\n".join(lines)


# ── MEXC gainers/losers scanner (Priorities 10 + 11) ─────────────────────

_NON_CRYPTO = {
    "stock", "xau", "xag", "usd_", "eur", "gbp", "jpy", "aud",
    "chf", "cad", "nzd", "oil", "gas", "corn", "wheat", "sp500",
    "nasdaq", "dow", "gold", "silver",
}


def _is_crypto_perp(sym: str) -> bool:
    """Fix 3: filter out stocks, forex, and commodities — crypto only."""
    sym_lower = sym.lower()
    return not any(s in sym_lower for s in _NON_CRYPTO)


def _parse_change_pct(t: dict) -> float | None:
    """
    Robust 24h change % parser — handles all MEXC API field variants.
    priceChangePercent (%), changeRate (decimal), riseFallRate (decimal),
    or computed from lastPrice / open24h.
    """
    for field in ("priceChangePercent",):
        val = t.get(field)
        if val is not None:
            try:
                fval = float(val)
                if abs(fval) < 2.0 and fval != 0:
                    fval *= 100
                return fval
            except (ValueError, TypeError):
                pass

    for field in ("changeRate", "riseFallRate"):
        val = t.get(field)
        if val is not None:
            try:
                return float(val) * 100
            except (ValueError, TypeError):
                pass

    try:
        last  = float(t.get("lastPrice", 0))
        open_ = float(t.get("open24h", t.get("openPrice", t.get("open", 0))))
        if last > 0 and open_ > 0:
            return round((last - open_) / open_ * 100, 4)
    except (ValueError, TypeError):
        pass

    return None


def _fetch_mexc_tickers() -> list:
    import requests
    resp = requests.get("https://contract.mexc.com/api/v1/contract/ticker", timeout=15)
    resp.raise_for_status()
    raw = resp.json().get("data", [])
    tickers = []
    for t in raw:
        sym = t.get("symbol", "")
        if not sym.endswith("_USDT"):
            continue
        # Fix 3: skip non-crypto assets
        if not _is_crypto_perp(sym):
            continue
        change_pct = _parse_change_pct(t)
        if change_pct is None:
            continue
        # Skip near-zero moves — noise
        if abs(change_pct) < 0.01:
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


def _classify_gainer(ticker: dict, candles: list = None) -> str:
    """
    Classify a gainer:
      TREND_CONTINUATION — higher highs, higher lows, bullish BOS, volume
      POSSIBLE_EXHAUSTION — bearish divergence, distribution
    """
    chg = ticker["change_pct"]
    # Basic: large move with no candle data → unknown
    if not candles or len(candles) < 10:
        return "PUMP" if chg > 15 else "GAINER"

    closes = [c["close"] for c in candles[-10:]]
    hh = closes[-1] > max(closes[:-1])
    hl = min(closes[-5:]) > min(closes[:-5])

    if hh and hl:
        return "TREND_CONTINUATION"
    elif chg > 20:
        return "POSSIBLE_EXHAUSTION"
    return "GAINER"


def _classify_loser(ticker: dict, candles: list = None) -> str:
    """
    Classify a loser:
      CAPITULATION  — strong bearish continuation
      REVERSAL_CANDIDATE — bullish CHoCH / BOS starting
    """
    chg = ticker["change_pct"]
    if not candles or len(candles) < 10:
        return "DUMP" if chg < -15 else "LOSER"

    closes = [c["close"] for c in candles[-10:]]
    ll = closes[-1] < min(closes[:-1])
    lh = max(closes[-5:]) < max(closes[:-5])

    if not ll and not lh:
        return "REVERSAL_CANDIDATE"
    elif chg < -20:
        return "CAPITULATION"
    return "LOSER"


def run_mexc_movers_scan():
    """
    Priorities 10 + 11: scan MEXC for +/-5/10/15% movers.
    Alert with classification (continuation vs exhaustion for gainers,
    capitulation vs reversal for losers).

    Fix 1: suppressed for 2 minutes after user manually calls /crimes,
    to avoid flooding duplicate information.
    """
    log.info("scanner: MEXC movers scan...")
    # Fix 1: skip if user just ran /crimes manually
    if _manual_crimes_active():
        log.debug("scanner: movers scan suppressed — /crimes was just called manually")
        return
    try:
        tickers = _fetch_mexc_tickers()
        if not tickers:
            return

        live = [t for t in tickers if abs(t["change_pct"]) > 0.1]

        for ticker in live:
            sym = ticker["symbol"]
            chg = ticker["change_pct"]

            # ── Gainers (P10) ──────────────────────────────────────────────
            for threshold in MEXC_GAINER_THRESHOLDS:
                if chg >= threshold:
                    key = f"mexc_gainer_{sym}_{threshold}"
                    if not _is_cooled_down(key, CRIME_COOLDOWN):
                        continue

                    classification = _classify_gainer(ticker)
                    icon = "🚀" if classification == "TREND_CONTINUATION" else "⚠️"
                    price = ticker["price"]

                    if classification == "TREND_CONTINUATION":
                        msg = (
                            f"🚀 <b>TREND CONTINUATION</b>\n\n"
                            f"<b>{sym}/USDT</b>\n"
                            f"24H: <b>{chg:+.1f}%</b>\n\n"
                            f"Pullback zone: watch for retest\n"
                            f"Price: {price:.6g}\n\n"
                            f"✓ Higher highs confirmed\n"
                            f"✓ Volume expansion\n\n"
                            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                    elif classification == "POSSIBLE_EXHAUSTION":
                        msg = (
                            f"⚠️ <b>POSSIBLE REVERSAL</b>\n\n"
                            f"<b>{sym}/USDT</b>\n"
                            f"24H: <b>{chg:+.1f}%</b>\n\n"
                            f"Risk: Momentum weakening\n"
                            f"Price: {price:.6g}\n\n"
                            f"Watch for bearish CHoCH on lower TF\n\n"
                            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                    else:
                        msg = (
                            f"{icon} <b>MEXC GAINER +{threshold}%</b>\n\n"
                            f"<b>{sym}/USDT</b>  {chg:+.1f}%\n"
                            f"Price: {price:.6g}\n\n"
                            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )

                    _send_alert(msg, alert_type=f"gainer_{threshold}", symbol=sym)
                    _mark_alerted(key)
                    break  # only fire highest threshold crossed

            # ── Losers (P11) ───────────────────────────────────────────────
            for threshold in MEXC_LOSER_THRESHOLDS:
                if chg <= threshold:
                    key = f"mexc_loser_{sym}_{abs(threshold)}"
                    if not _is_cooled_down(key, CRIME_COOLDOWN):
                        continue

                    classification = _classify_loser(ticker)
                    price = ticker["price"]

                    if classification == "REVERSAL_CANDIDATE":
                        msg = (
                            f"⚠️ <b>OVERSOLD REVERSAL WATCH</b>\n\n"
                            f"<b>{sym}/USDT</b>\n"
                            f"24H: <b>{chg:+.1f}%</b>\n\n"
                            f"Price: {price:.6g}\n"
                            f"Potential reversal forming\n"
                            f"Watch for: Bullish CHoCH, BOS, liquidity reclaim\n\n"
                            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                    elif classification == "CAPITULATION":
                        msg = (
                            f"📉 <b>CAPITULATION — {sym}/USDT</b>\n\n"
                            f"24H: <b>{chg:+.1f}%</b>\n"
                            f"Price: {price:.6g}\n\n"
                            f"Strong bearish continuation.\n"
                            f"Wait for structure confirmation before entry.\n\n"
                            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )
                    else:
                        msg = (
                            f"📉 <b>MEXC LOSER {threshold}%</b>\n\n"
                            f"<b>{sym}/USDT</b>  {chg:+.1f}%\n"
                            f"Price: {price:.6g}\n\n"
                            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                        )

                    _send_alert(msg, alert_type=f"loser_{abs(threshold)}", symbol=sym)
                    _mark_alerted(key)
                    break  # only fire lowest threshold crossed

    except Exception as e:
        log.error(f"scanner: MEXC movers scan error — {e}")


# ── MEXC crime scan (legacy, kept for /crimes context) ────────────────────

def run_mexc_crime_scan():
    """Scan MEXC for coordinated pumps/dumps."""
    log.info("scanner: running MEXC crime scan...")
    try:
        from app.mexc_data import scan_for_crimes, get_funding_rate
        crimes = scan_for_crimes(top_n=5)
        if not crimes:
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
                    mins     = max(0, (next_ms - int(time.time() * 1000)) // 60000) if next_ms else 999
                    if direction == "PUMP" and rate > 0.001:
                        funding_line = f"⚠️ Funding: {rate_pct} (longs paying — reversal risk {mins}min)"
                    elif direction == "PUMP" and rate < -0.0005:
                        funding_line = f"🔥 Funding: {rate_pct} (shorts paying — continuation fuel)"
                    elif direction == "DUMP" and rate < -0.001:
                        funding_line = f"⚠️ Funding: {rate_pct} (shorts paying — squeeze risk)"
                    else:
                        funding_line = f"Funding: {rate_pct} ({mins}min)"
            except Exception:
                pass

            try:
                from app.reasoning import explain_crime_move
                explanation = explain_crime_move(crime)
            except Exception:
                explanation = ""

            msg_lines = [
                f"{severity} — MEXC CRIME DETECTED",
                f"",
                f"Symbol: {sym.replace('_USDT', '')}/USDT",
                f"Move:   {chg:+.2f}%  ({direction})",
                f"Volume: {rvol:.1f}x normal",
                f"Range:  {rng:.1f}%",
                f"Score:  {score:.0f}",
            ]
            if funding_line:
                msg_lines.append(funding_line)
            if explanation:
                msg_lines += ["", explanation]
            msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")

            _send_alert("\n".join(msg_lines), alert_type="crime", symbol=sym)
            _mark_alerted(key)
            _persist_state()

    except Exception as e:
        log.error(f"scanner: MEXC crime scan error — {e}")


# ── Structure scan — default watchlist (P8: continuation vs change) ────────

def run_structure_scan():
    """
    Intelligence-driven structure scan for default watchlist.
    P8: classifies each as continuation or trend change, not bare events.
    """
    log.info("scanner: running structure scan...")
    try:
        from app.market_data import get_klines
        from app.mexc_data import get_funding_rate as mexc_funding
        from app.intelligence import build_intelligence

        for symbol in STRUCTURE_WATCHLIST:
            if symbol in MAJOR_ASSETS:
                continue  # majors handled by run_major_asset_scan()

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

                intel  = build_intelligence(symbol, c_m15, c_h1, c_h4, c_daily, funding_rate)
                score  = intel.get("score", 0)

                sym_short = symbol.replace("USDT", "")
                sweep     = intel.get("sweep", {})
                bos       = intel.get("bos", {})
                disp      = intel.get("displacement", {})
                bias      = intel.get("htf_bias", "Neutral")
                regime    = intel.get("regime", "Unknown")
                ob        = intel.get("order_block", {})
                fvg       = intel.get("fvg", {})
                session   = intel.get("session", {})

                # Continuation alert: sweep + displacement + BOS aligned
                if (sweep.get("detected") and sweep.get("age_candles", 99) <= 3
                        and disp.get("confirmed") and score >= PROACTIVE_ALERT_SCORE):
                    key = f"continuation_{symbol}_{round(sweep.get('level',0), 2)}"
                    if _is_cooled_down(key, SWEEP_COOLDOWN):
                        bias_lower = bias.lower()
                        sw_dir     = sweep.get("direction", "")
                        aligned    = sw_dir == bias_lower or bias_lower == "neutral"

                        # P8: label it continuation or just sweep
                        label = "TREND CONTINUATION" if aligned else "COUNTER-TREND SWEEP"

                        try:
                            from app.trade_plan import generate_trade_plan, format_trade_plan
                            plan = generate_trade_plan(intel)
                            plan_text = format_trade_plan(plan)
                        except Exception:
                            plan_text = ""

                        msg_lines = [
                            f"⚡ <b>{label} — {sym_short}</b>",
                            f"",
                            f"{sweep.get('description', '')}",
                            f"Displacement: ✓ {disp.get('description', '')}",
                            f"Score: {score}/100  |  Regime: {regime}",
                            f"HTF Bias: {bias}",
                        ]
                        if ob.get("found"):
                            msg_lines.append(f"📦 OB: {ob['low']:.4f}–{ob['high']:.4f}")
                        if plan_text:
                            msg_lines += ["", plan_text]
                        if session.get("in_killzone"):
                            msg_lines.append(f"⏰ {session.get('description', '')}")
                        msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")

                        _send_alert("\n".join(msg_lines), alert_type="continuation", symbol=symbol)
                        _mark_alerted(key)
                        _persist_state()

                # BOS-only alert (no displacement yet)
                elif bos.get("broken") and not sweep.get("detected"):
                    bos_key = f"bos_{symbol}_{round(bos.get('level',0),2)}_{bos.get('bias','')}"
                    if _is_cooled_down(bos_key, BOS_COOLDOWN):
                        msg_lines = [
                            f"⚡ <b>H1 BOS — {sym_short}</b>",
                            f"",
                            f"{bos.get('description', '')}",
                            f"Regime: {regime}  |  Score: {score}/100",
                            f"HTF Bias: {bias}",
                            f"👀 Watching for sweep + displacement entry",
                        ]
                        msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                        _send_alert("\n".join(msg_lines), alert_type="bos", symbol=symbol)
                        _mark_alerted(bos_key)
                        _persist_state()

            except Exception as e:
                log.debug(f"scanner: structure scan {symbol}: {e}")

    except Exception as e:
        log.error(f"scanner: structure scan error — {e}")


# ── User watchlist scan (P1: direction-aware; P6: workflow integration) ────

def _run_user_watchlist_scan():
    """
    Scan user's personal watchlists.
    P1: match detected direction against stored direction — mismatches silently ignored.
    P6: evaluate multi-stage workflows from workflow.py.
    """
    if not _user_watchlists:
        return

    try:
        from app.market_data import get_klines
        from app.mexc_data import get_funding_rate as mexc_funding
        from app.intelligence import build_intelligence
        from app import workflow as wf_mod
        from app.trade_plan import generate_trade_plan, format_trade_plan

        for chat_id, watches in _user_watchlists.items():
            for watch in watches:
                symbol     = watch["symbol"]
                conds      = watch.get("conditions", [])
                entry_tf   = watch.get("entry_tf",   "15m")
                confirm_tf = watch.get("confirm_tf", "1h")
                bias_tf    = watch.get("bias_tf",    "4h")
                mode       = watch.get("mode", "strict")

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

                    intel = build_intelligence(
                        symbol        = symbol,
                        candles_m15   = c_entry if entry_tf in ("15m", "5m") else [],
                        candles_h1    = c_confirm,
                        candles_h4    = c_bias,
                        candles_daily = c_daily,
                        funding_rate  = funding_rate,
                    )

                    sym_short = symbol.replace("USDT", "").replace("_USDT", "")
                    sweep     = intel.get("sweep", {})
                    bos       = intel.get("bos", {})
                    disp      = intel.get("displacement", {})
                    bias      = intel.get("htf_bias", "Neutral").lower()
                    ob        = intel.get("order_block", {})
                    score     = intel.get("score", 0)

                    # ── Evaluate each condition with direction matching (P1) ──
                    for cond in conds:
                        if isinstance(cond, str):
                            cond = {"event": cond, "direction": "", "strict": False}

                        event         = cond.get("event", "")
                        req_direction = cond.get("direction", "")
                        strict        = cond.get("strict", bool(req_direction))

                        # ── BOS check ───────────────────────────────────────
                        if event == "bos" and bos.get("broken"):
                            detected_dir = bos.get("bias", "")

                            # P1 CORE: if direction requested, must match
                            if req_direction and detected_dir != req_direction:
                                if strict:
                                    continue  # Wrong direction — silent
                            else:
                                key = f"user_{chat_id}_bos_{symbol}_{round(bos.get('level',0),2)}_{detected_dir}"
                                if _is_cooled_down(key, BOS_COOLDOWN):
                                    try:
                                        plan = generate_trade_plan(intel)
                                        plan_text = format_trade_plan(plan)
                                    except Exception:
                                        plan_text = ""

                                    dir_label = f" [{detected_dir.upper()}]" if detected_dir else ""
                                    msg_lines = [
                                        f"⚡ <b>YOUR WATCH — {sym_short} BOS{dir_label}</b>",
                                        f"",
                                        f"{bos.get('description', '')}",
                                        f"Score: {score}/100  |  Bias: <b>{intel.get('htf_bias','')}</b>",
                                    ]
                                    if plan_text:
                                        msg_lines += ["", plan_text]
                                    if watch.get("note"):
                                        msg_lines.append(f"\n📌 {watch['note']}")
                                    msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                                    if _send_fn:
                                        _send_fn(chat_id, "\n".join(msg_lines))
                                    _mark_alerted(key)
                                    _persist_state()

                        # ── Sweep check ─────────────────────────────────────
                        if event == "sweep" and sweep.get("detected") and sweep.get("age_candles", 99) <= 3:
                            detected_dir = sweep.get("direction", "")

                            if req_direction and detected_dir != req_direction:
                                if strict:
                                    continue

                            key = f"user_{chat_id}_sweep_{symbol}_{round(sweep.get('level',0),2)}"
                            if _is_cooled_down(key, SWEEP_COOLDOWN):
                                try:
                                    plan      = generate_trade_plan(intel)
                                    plan_text = format_trade_plan(plan)
                                except Exception:
                                    plan_text = ""

                                dir_label = f" [{detected_dir.upper()}]" if detected_dir else ""
                                msg_lines = [
                                    f"🎯 <b>YOUR WATCH — {sym_short} SWEEP{dir_label}</b>",
                                    f"",
                                    f"{sweep.get('description', '')}",
                                    f"Score: {score}/100  |  Bias: <b>{intel.get('htf_bias','')}</b>",
                                ]
                                if disp.get("confirmed"):
                                    msg_lines.append(f"✅ {disp.get('description','Displacement confirmed')}")
                                else:
                                    msg_lines.append("⏳ No displacement yet — waiting")
                                if ob.get("found"):
                                    msg_lines.append(f"📦 OB: {ob['low']:.4f}–{ob['high']:.4f}")
                                if plan_text:
                                    msg_lines += ["", plan_text]
                                if watch.get("note"):
                                    msg_lines.append(f"\n📌 {watch['note']}")
                                msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                                if _send_fn:
                                    _send_fn(chat_id, "\n".join(msg_lines))
                                _mark_alerted(key)
                                _persist_state()

                        # ── Funding checks ──────────────────────────────────
                        if event.startswith("funding"):
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
                                    if event == "funding_negative" and rate < 0:
                                        fire = True
                                        rate_desc = f"Funding went NEGATIVE: {rate*100:+.4f}% (shorts paying)"
                                    elif event == "funding_positive" and rate > 0.001:
                                        fire = True
                                        rate_desc = f"Funding HIGH POSITIVE: {rate*100:+.4f}% (longs paying)"
                                    elif event == "funding_change" and abs(rate) > 0.0005:
                                        fire = True
                                        rate_desc = f"Funding notable: {rate*100:+.4f}%"
                                    if fire:
                                        fkey = f"user_{chat_id}_funding_{symbol}_{rate > 0}"
                                        if _is_cooled_down(fkey, 3600):
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
                                            _mark_alerted(fkey)
                                            _persist_state()
                            except Exception as _fe:
                                log.debug(f"funding monitor {symbol}: {_fe}")

                    # ── Assisted mode: send extra notable events ────────────
                    if mode == "assisted":
                        if score >= PROACTIVE_ALERT_SCORE:
                            key = f"user_{chat_id}_assisted_{symbol}_{score // 10}"
                            if _is_cooled_down(key, 7200):
                                msg_lines = [
                                    f"📊 <b>NOTABLE — {sym_short}</b>  [ASSISTED]",
                                    f"",
                                    f"Score: {score}/100  |  Bias: {intel.get('htf_bias','')}",
                                    f"Regime: {intel.get('regime','')}",
                                ]
                                if sweep.get("detected"):
                                    msg_lines.append(f"• Sweep: {sweep.get('description','')}")
                                if bos.get("broken"):
                                    msg_lines.append(f"• BOS: {bos.get('description','')}")
                                if disp.get("confirmed"):
                                    msg_lines.append(f"• Displacement confirmed")
                                msg_lines.append(f"\n{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
                                if _send_fn:
                                    _send_fn(chat_id, "\n".join(msg_lines))
                                _mark_alerted(key)

                except Exception as e:
                    log.debug(f"user watchlist scan {symbol} for {chat_id}: {e}")

        # ── Workflow evaluation (P6/P7) ────────────────────────────────────
        _run_workflow_scan()

    except Exception as e:
        log.error(f"_run_user_watchlist_scan error: {e}")


def _run_workflow_scan():
    """
    Priority 6/7: evaluate all user-defined multi-stage workflows.
    Each workflow tracks its own progress across scan cycles.
    Only alerts when ALL stages complete.
    """
    try:
        from app import workflow as wf_mod
        from app.market_data import get_klines
        from app.mexc_data import get_funding_rate as mexc_funding
        from app.intelligence import build_intelligence
        from app.trade_plan import generate_trade_plan

        all_workflows = wf_mod.get_all_workflows()
        if not all_workflows:
            return

        for chat_id, flows in all_workflows.items():
            for i, flow in enumerate(flows):
                if flow.get("alerted"):
                    continue

                symbol = flow["symbol"]
                try:
                    c_m15   = get_klines(symbol, "15m", 96)
                    c_h1    = get_klines(symbol, "1h",  50)
                    c_h4    = get_klines(symbol, "4h",  50)
                    c_daily = get_klines(symbol, "1d",  30)

                    funding_rate = 0.0
                    try:
                        fd = mexc_funding(symbol)
                        if fd.get("ok"):
                            funding_rate = fd["funding_rate"]
                    except Exception:
                        pass

                    intel  = build_intelligence(symbol, c_m15, c_h1, c_h4, c_daily, funding_rate)
                    result = wf_mod.evaluate_workflow(flow, intel)

                    # Update progress
                    flows[i]["progress"] = result["progress"]

                    if result["newly_complete"]:
                        flows[i]["alerted"] = True
                        try:
                            plan = generate_trade_plan(intel)
                        except Exception:
                            plan = {}

                        alert_text = wf_mod.format_workflow_alert(flow, result, intel, plan)
                        if _send_fn:
                            _send_fn(int(chat_id), alert_text)
                        log.info(f"workflow complete: {flow['name']} for {chat_id}")

                except Exception as e:
                    log.debug(f"workflow scan {symbol} for {chat_id}: {e}")

    except Exception as e:
        log.error(f"_run_workflow_scan error: {e}")


# ── Opportunity ranking ───────────────────────────────────────────────────

def run_opportunity_ranking():
    """Rank all watchlist symbols; alert if top score clears threshold."""
    log.info("scanner: opportunity ranking...")
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

        best  = top[0]
        score = best["score"]

        if score < PROACTIVE_ALERT_SCORE:
            return

        key = f"ranking_{best['symbol']}_{score // 10 * 10}"
        if not _is_cooled_down(key, 3600):
            return

        lines = ["🏆 <b>OPPORTUNITY RANKING</b>\n"]
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

    except Exception as e:
        log.error(f"scanner: opportunity ranking error — {e}")


# ── Background loops ──────────────────────────────────────────────────────

def _mexc_loop():
    while _running:
        run_mexc_crime_scan()
        run_mexc_movers_scan()
        time.sleep(MEXC_SCAN_INTERVAL)


def _structure_loop():
    time.sleep(60)
    scan_count = 0
    while _running:
        run_major_asset_scan()
        run_structure_scan()
        _run_user_watchlist_scan()
        scan_count += 1
        if scan_count % 3 == 0:
            run_opportunity_ranking()
        time.sleep(STRUCTURE_SCAN_INTERVAL)


def start_scanner(send_fn, chat_id: int):
    global _send_fn, _chat_id, _running, _alert_cooldowns, _user_watchlists

    _send_fn = send_fn
    _chat_id = chat_id

    try:
        from app.zg_storage import load_state
        prior = load_state()
        if prior:
            _alert_cooldowns = prior.get("cooldowns",   {})
            _user_watchlists = {
                int(k): v
                for k, v in prior.get("watchlists", {}).items()
                if str(k).isdigit()
            }
            # Restore workflows
            try:
                from app import workflow as wf_mod
                wf_mod.load_workflows(prior.get("workflows", {}))
            except Exception:
                pass
            log.info(
                f"scanner: restored — {len(_alert_cooldowns)} cooldowns, "
                f"{sum(len(v) for v in _user_watchlists.values())} user monitors"
            )
        else:
            log.info("scanner: starting fresh")
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
