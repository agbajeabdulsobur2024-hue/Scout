"""
main.py — Scout entrypoint.

Run with: python main.py
Requires .env at repo root with ZG_SERVICE_URL, ZG_API_SECRET, TELEGRAM_BOT_TOKEN.
See SETUP_0G.md for full setup walkthrough.
"""

import logging
import sys
from dotenv import load_dotenv

load_dotenv()  # loads .env from current directory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("scout")


def main():
    from app import zg_compute, telegram_bot

    log.info("Scout: checking 0G Compute connection...")
    health = zg_compute.health_check()
    if not health.get("ok"):
        log.error(f"0G Compute health check failed: {health}")
        log.error("Make sure ZG_API_SECRET and ZG_SERVICE_URL are set in .env")
        sys.exit(1)
    log.info(f"0G Compute OK — model={health.get('model')}")

    telegram_bot.run_polling_loop()


if __name__ == "__main__":
    main()
