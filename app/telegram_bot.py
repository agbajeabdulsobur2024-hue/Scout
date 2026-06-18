"""
telegram_bot.py — thin chat interface for Scout. Uses Telegram's raw HTTP
API directly (no extra SDK dependency) with a simple long-poll loop. All
actual intelligence lives in reasoning.py / zg_compute.py — this file only
moves text in and out.
"""

import os
import time
import logging
import requests
from app import reasoning

log = logging.getLogger("scout")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_BASE  = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Tiny in-memory per-chat context — fine for a demo, not meant to survive
# a restart. Swapping this for 0G Storage is the planned next step.
_chat_context: dict = {}


def send_message(chat_id: int, text: str) -> None:
    try:
        requests.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},
            timeout=15,
        )
    except Exception as e:
        log.error(f"send_message failed: {e}")


def handle_update(update: dict) -> None:
    msg = update.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")
    if not chat_id or not text:
        return

    if text.startswith("/start"):
        send_message(chat_id,
            "I'm Scout — an AI market intelligence companion running on 0G Compute.\n\n"
            "Ask me things like:\n"
            "• Why is BTC moving right now?\n"
            "• What are the best setups right now?\n"
            "• Explain ETH\n\n"
            "Every answer comes from a live model call through 0G's decentralized "
            "inference network, reasoning over fresh market data — not a canned response."
        )
        return
    if text.startswith("/help"):
        send_message(chat_id, "Just ask in plain language — try 'why is SOL moving' or 'best setups right now'.")
        return

    log.info(f"chat {chat_id}: {text}")
    history = _chat_context.setdefault(chat_id, [])
    reply = reasoning.route_message(text, recent_context=history)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    _chat_context[chat_id] = history[-10:]  # keep last few turns only

    send_message(chat_id, reply)


def run_polling_loop() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
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
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as e:
                    log.error(f"handle_update error: {e}")
        except requests.exceptions.RequestException as e:
            log.warning(f"polling error (will retry): {e}")
            time.sleep(3)
