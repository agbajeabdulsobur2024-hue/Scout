"""
zg_compute.py — 0G Compute Network client.

Calls the 0G Compute proxy endpoint directly with a static API key.
The key is generated from compute-marketplace.0g.ai (Build → Generate API Key).

This is the ONLY place in Scout that talks to an LLM. Every answer
goes through 0G Compute's decentralized inference network.

Required env vars (in .env at repo root):
    ZG_SERVICE_URL  — e.g. https://compute-network-6.integratenetwork.work/v1/proxy
    ZG_API_SECRET   — your app-sk-... key
    ZG_MODEL        — defaults to qwen/qwen2.5-omni-7b
"""

import os
import logging
import requests

log = logging.getLogger("scout")

ZG_SERVICE_URL = os.environ.get(
    "ZG_SERVICE_URL",
    "https://compute-network-6.integratenetwork.work/v1/proxy"
).rstrip("/")

ZG_API_SECRET = os.environ.get("ZG_API_SECRET", "")
ZG_MODEL      = os.environ.get("ZG_MODEL", "qwen/qwen2.5-omni-7b")

COMPUTE_ENABLED = bool(ZG_API_SECRET)

if not COMPUTE_ENABLED:
    log.warning(
        "ZG_API_SECRET is not set. Put your app-sk-... key in .env. "
        "Get it from compute-marketplace.0g.ai → Build → Generate API Key."
    )


class ZGComputeError(Exception):
    pass


def ask(messages: list, temperature: float = 0.3, max_tokens: int = 500) -> str:
    """
    Send a chat-completions request through 0G Compute.
    Returns the model's text response, or raises ZGComputeError.
    """
    if not COMPUTE_ENABLED:
        raise ZGComputeError("ZG_API_SECRET is not set.")

    # qwen2.5-omni-7b requires modalities field and min max_tokens of 10
    max_tokens = max(10, max_tokens)

    url     = f"{ZG_SERVICE_URL}/chat/completions"
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {ZG_API_SECRET}",
    }
    payload = {
        "model":       ZG_MODEL,
        "messages":    messages,
        "modalities":  ["text"],
        "temperature": temperature,
        "max_tokens":  max_tokens,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.exceptions.RequestException as e:
        log.error(f"0G Compute request failed: {e}")
        raise ZGComputeError(f"0G Compute request failed: {e}") from e
    except (KeyError, IndexError) as e:
        log.error(f"0G Compute unexpected response: {resp.text[:200]}")
        raise ZGComputeError(f"0G Compute unexpected response: {e}") from e


def health_check() -> dict:
    """Confirms the endpoint and API key both work."""
    if not COMPUTE_ENABLED:
        return {"ok": False, "reason": "ZG_API_SECRET not set"}
    try:
        reply = ask(
            [{"role": "user", "content": "Reply with exactly the word: ok"}],
            temperature=0.0,
            max_tokens=10,
        )
        return {"ok": "ok" in reply.lower(), "model": ZG_MODEL, "raw": reply}
    except ZGComputeError as e:
        return {"ok": False, "reason": str(e)}
