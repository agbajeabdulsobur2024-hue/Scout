"""
telegram_bot.py — Scout's Telegram interface.

Handles both:
  1. Inbound chat messages (user asks Scout something)
  2. Outbound proactive alerts (scanner fires an alert unprompted)

All intelligence lives in reasoning.py / zg_compute.py.
Scanner lives in scanner.py.
This file only moves text in and out of Telegram.
"""

import os
import time
import logging
import requests
from app import reasoning

log = logging.getLogger("scout")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_BASE  = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Per-chat context for multi-turn conversation
_chat_context: dict = {}

# The chat ID that gets proactive scanner alerts
# Set on first /start, or via SCOUT_ALERT_CHAT_ID env var
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


def handle_update(update: dict) -> None:
    global _alert_chat_id

    msg     = update.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    text    = msg.get("text", "").strip()
    if not chat_id or not text:
        return

    # Register first chatter as the alert recipient
    if not _alert_chat_id:
        _alert_chat_id = chat_id
        log.info(f"telegram_bot: alert chat_id set to {chat_id}")

    # ── Commands ──────────────────────────────────────────────────────────
    if text.startswith("/start"):
        send_message(chat_id,
            "I'm <b>Scout</b> — an AI market intelligence companion running on "
            "<b>0G Compute</b>'s decentralized inference network.\n\n"
            "I watch markets 24/7 and alert you when something real happens:\n"
            "• Liquidity sweeps on H1/H4\n"
            "• Break of structure events\n"
            "• Coordinated pumps/dumps on MEXC\n\n"
            "You can also ask me things:\n"
            "• <i>Why is BTC moving right now?</i>\n"
            "• <i>What are the best setups?</i>\n"
            "• <i>Show me MEXC crimes</i>\n"
            "• <i>Explain ETH structure</i>\n"
            "• <i>What's the bias on SOL?</i>\n\n"
            "Every answer is reasoned by a model on 0G Compute — not canned."
        )
        return

    if text.startswith("/help"):
        send_message(chat_id,
            "<b>Scout Commands</b>\n\n"
            "/start — introduction\n"
            "/scan — run structure scan now\n"
            "/crimes — run MEXC crime scan now\n"
            "/movers — top MEXC gainers and losers\n"
            "/bias BTC — HTF bias for a symbol\n\n"
            "Or just ask in plain language — Scout understands natural questions."
        )
        return

    if text.startswith("/crimes"):
        send_message(chat_id, "Running MEXC crime scan... give me a moment.")
        from app.scanner import run_mexc_crime_scan
        run_mexc_crime_scan()
        return

    if text.startswith("/scan"):
        send_message(chat_id, "Running structure scan across watchlist... this takes ~30 seconds.")
        from app.scanner import run_structure_scan
        run_structure_scan()
        return

    if text.startswith("/movers"):
        from app.mexc_data import get_top_movers
        movers = get_top_movers(top_n=5)
        lines  = ["<b>MEXC Top Movers</b>\n"]
        lines.append("📈 <b>Gainers</b>")
        for m in movers["gainers"]:
            lines.append(f"  {m['symbol'].replace('_USDT','')}  <b>{m['change_pct']:+.1f}%</b>  @ {m['price']:.4f}")
        lines.append("\n📉 <b>Losers</b>")
        for m in movers["losers"]:
            lines.append(f"  {m['symbol'].replace('_USDT','')}  <b>{m['change_pct']:+.1f}%</b>  @ {m['price']:.4f}")
        send_message(chat_id, "\n".join(lines))
        return

    if text.startswith("/bias"):
        parts  = text.split()
        symbol = (parts[1].upper() + "USDT").replace("USDTUSDT", "USDT") if len(parts) > 1 else "BTCUSDT"
        send_message(chat_id, f"Analysing {symbol} structure...")
        reply = reasoning.explain_structure(symbol)
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

    # Start the background scanner once we know where to send alerts
    # (scanner.py will use send_message as its send_fn)
    from app import scanner

    log.info("Scout: starting Telegram polling loop")
    offset = 0

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
                    # Start scanner after first message so we have a chat_id
                    if _alert_chat_id and not scanner._running:
                        scanner.start_scanner(
                            send_fn=send_message,
                            chat_id=_alert_chat_id,
                        )
                except Exception as e:
                    log.error(f"handle_update error: {e}")

        except requests.exceptions.RequestException as e:
            log.warning(f"polling error (will retry): {e}")
            time.sleep(3)
