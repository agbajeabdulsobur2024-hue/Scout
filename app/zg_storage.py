"""
zg_storage.py — 0G Storage client for Scout.

Calls the zg-sidecar's /storage endpoints which wrap @0glabs/0g-ts-sdk.
Used to persist scanner state (alert cooldowns, user watchlists, alert log)
to 0G's decentralized storage network so Scout's memory survives restarts.

Architecture:
  - Root hash = permanent address of each upload on 0G Storage
  - Scout stores the latest root hash in a local pointer file
  - On startup: read pointer → fetch from 0G → restore state
  - On state change: upload to 0G → update pointer

This gives Scout:
  1. Decentralized, tamper-proof market intelligence log
  2. Restart-proof scanner state (cooldowns, watchlists survive)
  3. "While you were offline" catchup — what fired since last session

The pointer file is tiny (66-char hex string) — survives Render restarts
within the same deployment. 0G Storage holds the actual data.
"""

import os
import json
import logging
import requests

log = logging.getLogger("scout")

ZG_SIDECAR_URL   = os.environ.get("ZG_SIDECAR_URL", "http://localhost:8787").rstrip("/")
POINTER_FILE     = os.environ.get("ZG_STATE_POINTER", "/tmp/scout_state_root.txt")
STORAGE_ENABLED  = True   # gracefully degrades if sidecar isn't running


def upload(data: dict) -> str | None:
    """
    Upload a JSON-serialisable dict to 0G Storage.
    Returns the root hash (permanent 0G address) or None on failure.
    Fire-and-forget safe — never raises.
    """
    try:
        resp = requests.post(
            f"{ZG_SIDECAR_URL}/storage/upload",
            json=data,
            timeout=60,
        )
        if resp.status_code == 200:
            root = resp.json().get("root")
            log.info(f"zg_storage: uploaded state → root={root}")
            return root
        else:
            log.warning(f"zg_storage: upload failed {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        log.debug(f"zg_storage: upload error (sidecar down?) — {e}")
    return None


def download(root: str) -> dict | None:
    """
    Download data from 0G Storage by root hash.
    Returns parsed dict or None on failure.
    """
    try:
        resp = requests.get(
            f"{ZG_SIDECAR_URL}/storage/download/{root}",
            timeout=60,
        )
        if resp.status_code == 200:
            return resp.json().get("data")
        else:
            log.warning(f"zg_storage: download failed {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        log.debug(f"zg_storage: download error (sidecar down?) — {e}")
    return None


def save_pointer(root: str) -> None:
    """Save the latest root hash to the local pointer file."""
    try:
        with open(POINTER_FILE, "w") as f:
            f.write(root)
    except Exception as e:
        log.debug(f"zg_storage: pointer save failed — {e}")


def load_pointer() -> str | None:
    """Read the latest root hash from the local pointer file."""
    try:
        with open(POINTER_FILE) as f:
            root = f.read().strip()
            return root if root.startswith("0x") else None
    except Exception:
        return None


def save_state(state: dict) -> bool:
    """
    Persist scanner state to 0G Storage and update the local pointer.
    State includes: alert_cooldowns, user_watchlists, alert_log.
    Returns True if saved successfully.
    """
    root = upload(state)
    if root:
        save_pointer(root)
        return True
    return False


def load_state() -> dict | None:
    """
    Restore scanner state from 0G Storage on startup.
    Returns the state dict or None if no prior state exists.
    """
    root = load_pointer()
    if not root:
        log.info("zg_storage: no prior state pointer found — starting fresh")
        return None
    log.info(f"zg_storage: restoring state from 0G Storage root={root}")
    state = download(root)
    if state:
        log.info("zg_storage: state restored successfully")
    else:
        log.warning("zg_storage: state download failed — starting fresh")
    return state


def append_alert_log(entry: dict) -> None:
    """
    Append one alert to the persistent log stored on 0G Storage.
    The log is Scout's verifiable, tamper-proof intelligence history.
    Called every time a sweep, BOS, or crime alert fires.
    Non-blocking — runs in a background thread.
    """
    import threading

    def _append():
        try:
            # Load current log, append, re-upload
            root  = load_pointer()
            state = download(root) if root else {}
            if not isinstance(state, dict):
                state = {}
            log_entries = state.get("alert_log", [])
            log_entries.append(entry)
            # Keep last 500 entries to stay under size limits
            state["alert_log"] = log_entries[-500:]
            save_state(state)
        except Exception as e:
            log.debug(f"zg_storage: append_alert_log failed — {e}")

    t = threading.Thread(target=_append, daemon=True, name="zg-storage-log")
    t.start()
