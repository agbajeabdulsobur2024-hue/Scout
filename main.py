"""
main.py — Scout entrypoint.

Run with: python main.py
Requires .env at repo root with ZG_SERVICE_URL, ZG_API_SECRET, TELEGRAM_BOT_TOKEN.
See SETUP_0G.md for full setup walkthrough.

A minimal HTTP server runs alongside the Telegram polling loop on PORT
(default 8080). This serves two purposes:
  1. Koyeb's health checks hit GET /health, keeping the free instance awake
  2. Judges/judges can verify Scout is live at any time
"""

import logging
import sys
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("scout")

PORT = int(os.environ.get("PORT", "8080"))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            body = b"Scout is alive"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress HTTP access logs — keep Railway/Koyeb logs clean


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    log.info(f"Scout: health server on port {PORT}")
    server.serve_forever()


def main():
    from app import zg_compute, telegram_bot

    # ── Start health server in background thread ──────────────────────────
    t = threading.Thread(target=start_health_server, daemon=True, name="health-server")
    t.start()

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
