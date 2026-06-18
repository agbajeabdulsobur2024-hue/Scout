"""
main.py — Scout entrypoint.

Run with: python main.py
Requires TELEGRAM_BOT_TOKEN set, and the zg-sidecar running (see SETUP_0G.md).
"""

import logging
import sys

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
        log.error("Scout cannot reason about anything without a working 0G Compute "
                   "connection. Make sure the zg-sidecar is running (see SETUP_0G.md) "
                   "and restart.")
        sys.exit(1)
    log.info(f"0G Compute OK — model={health.get('model')}")

    telegram_bot.run_polling_loop()


if __name__ == "__main__":
    main()
