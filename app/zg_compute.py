"""
zg_compute.py — 0G Compute Network client.

This is the ONLY place in Scout that talks to an LLM. Every explanation,
every recommendation, every chat answer is generated here, through 0G's
decentralized inference marketplace — not a normal hosted LLM API. That's
deliberate: 0G Compute has to be load-bearing, not a bolt-on.

Setup (one-time, run once in a terminal — see SETUP_0G.md for the full
walkthrough):

    npm install -g @0glabs/0g-serving-broker
    npm install -g openai          # not required at runtime, just for the CLI's own use
    export PRIVATE_KEY=...          # testnet wallet, funded via faucet.0g.ai
    0g-compute-cli setup-network
    0g-compute-cli login
    0g-compute-cli deposit --amount 3
    0g-compute-cli inference list-providers
    0g-compute-cli transfer-fund --provider <PROVIDER_ADDRESS> --amount 1
    0g-compute-cli inference acknowledge-provider --provider <PROVIDER_ADDRESS>
    0g-compute-cli inference get-secret --provider <PROVIDER_ADDRESS>
    # -> prints ZG_SERVICE_URL and an app-sk-... secret. Put both in .env.

At runtime, this module just calls that service URL with the secret as a
bearer token, OpenAI chat-completions shape. No wallet, no signing, no
Node — pure Python `requests`.
"""

import os
import logging
import requests

log = logging.getLogger("scout")

ZG_SERVICE_URL = os.environ.get("ZG_SERVICE_URL", "").rstrip("/")
ZG_API_SECRET  = os.environ.get("ZG_API_SECRET", "")
ZG_MODEL       = os.environ.get("ZG_MODEL", "qwen/qwen-2.5-7b-instruct")

COMPUTE_ENABLED = bool(ZG_SERVICE_URL and ZG_API_SECRET)

if not COMPUTE_ENABLED:
    log.warning(
        "0G Compute not configured — set ZG_SERVICE_URL and ZG_API_SECRET. "
        "See SETUP_0G.md. Scout cannot reason about anything without this."
    )


class ZGComputeError(Exception):
    pass


def ask(messages: list, temperature: float = 0.3, max_tokens: int = 500) -> str:
    """
    Send a chat-completions request through 0G Compute.

    messages: standard OpenAI-style [{"role": "system"|"user"|"assistant", "content": "..."}]
    Returns the model's text response, or raises ZGComputeError.
    """
    if not COMPUTE_ENABLED:
        raise ZGComputeError(
            "0G Compute is not configured (missing ZG_SERVICE_URL / ZG_API_SECRET)."
        )

    url = f"{ZG_SERVICE_URL}/v1/proxy/chat/completions"
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {ZG_API_SECRET}",
    }
    payload = {
        "model":       ZG_MODEL,
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.exceptions.RequestException as e:
        log.error(f"0G Compute request failed: {e}")
        raise ZGComputeError(f"0G Compute request failed: {e}") from e
    except (KeyError, IndexError) as e:
        log.error(f"0G Compute returned an unexpected shape: {data if 'data' in dir() else ''}")
        raise ZGComputeError(f"0G Compute returned an unexpected response shape: {e}") from e


def health_check() -> dict:
    """
    Quick sanity call — used by /health and at startup so a broken 0G
    connection fails loudly instead of silently degrading every answer.
    """
    if not COMPUTE_ENABLED:
        return {"ok": False, "reason": "ZG_SERVICE_URL / ZG_API_SECRET not set"}
    try:
        reply = ask(
            [{"role": "user", "content": "Reply with exactly the word: ok"}],
            temperature=0.0,
            max_tokens=5,
        )
        return {"ok": "ok" in reply.lower(), "model": ZG_MODEL, "raw": reply}
    except ZGComputeError as e:
        return {"ok": False, "reason": str(e)}
