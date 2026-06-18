"""
zg_compute.py — 0G Compute Network client.

This is the ONLY place in Scout that talks to an LLM. Every explanation,
every recommendation, every chat answer is generated here, through 0G's
decentralized inference marketplace — not a normal hosted LLM API. That's
deliberate: 0G Compute has to be load-bearing, not a bolt-on.

How this actually talks to 0G: NOT a static API key against a proxy URL —
that approach (from one version of 0G's quickstart docs) doesn't match
how the current SDK works. Billing headers in the broker SDK are
generated fresh per request and are single-use, which means something
has to run the wallet/broker logic for every call. That something is a
small Node service (zg-sidecar/) running alongside this app — see
SETUP_0G.md for why and how it's run. This module just calls that
sidecar over plain HTTP; no wallet, no signing, no Node *in this file*.

Setup (one-time, see SETUP_0G.md for the full walkthrough):
    cd zg-sidecar && npm install
    cp .env.example .env   # fill in PRIVATE_KEY (testnet wallet, funded via faucet.0g.ai)
    npm start              # leave this running
    # then set ZG_SIDECAR_URL below (defaults to http://localhost:8787)
"""

import os
import logging
import requests

log = logging.getLogger("scout")

ZG_SIDECAR_URL = os.environ.get("ZG_SIDECAR_URL", "http://localhost:8787").rstrip("/")

COMPUTE_ENABLED = True  # the sidecar's own /health call is the real check


class ZGComputeError(Exception):
    pass


def ask(messages: list, temperature: float = 0.3, max_tokens: int = 500) -> str:
    """
    Send a chat-completions request through the zg-sidecar, which handles
    the 0G Compute broker/billing logic on the Node side.

    messages: standard OpenAI-style [{"role": "system"|"user"|"assistant", "content": "..."}]
    Returns the model's text response, or raises ZGComputeError.
    """
    url = f"{ZG_SIDECAR_URL}/chat"
    payload = {
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }

    try:
        resp = requests.post(url, json=payload, timeout=60)
        if resp.status_code != 200:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise ZGComputeError(f"zg-sidecar returned {resp.status_code}: {detail}")
        data = resp.json()
        return (data.get("content") or "").strip()
    except requests.exceptions.RequestException as e:
        log.error(f"zg-sidecar request failed: {e}")
        raise ZGComputeError(
            f"Couldn't reach the zg-sidecar at {ZG_SIDECAR_URL} — is it running? ({e})"
        ) from e


def health_check() -> dict:
    """
    Calls the sidecar's own /health endpoint, which reports whether the
    broker actually initialized (wallet, ledger, provider acknowledgment)
    — not just whether the Node process is up.
    """
    try:
        resp = requests.get(f"{ZG_SIDECAR_URL}/health", timeout=10)
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            return {"ok": True, "model": data.get("model"), "provider": data.get("provider")}
        return {"ok": False, "reason": data}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "reason": f"Couldn't reach the zg-sidecar at {ZG_SIDECAR_URL} — is it running? ({e})"}
