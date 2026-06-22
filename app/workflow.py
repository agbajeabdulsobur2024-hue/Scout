"""
workflow.py — Multi-stage SMC workflow engine for Scout.

Priorities 5, 6, 7 of the Scout Master Roadmap:

  - User-defined custom SMC workflows (sequence of conditions)
  - Multi-stage setup engine: Bias → Confirmation → Entry → Alert
  - Persistent workflow memory per user

A workflow defines a sequence of SMC events that must all fire in order.
Scout evaluates them each scan cycle. It alerts ONLY when all stages pass.
It stays silent on partial progress unless asked.

Storage format (in-memory, persisted to 0G Storage):
  {
    "name": "syn_short_setup",
    "symbol": "SYNUSDT",
    "bias_tf": "4h",
    "bias": {"market_structure": "bearish"},
    "confirmations": [
        {"event": "choch", "direction": "bearish"},
        {"event": "bos",   "direction": "bearish"}
    ],
    "entry": [
        {"event": "sweep",              "direction": "bullish"},
        {"event": "displacement",       "direction": "bearish"},
        {"event": "order_block_retest", "direction": "bearish"}
    ],
    "mode": "strict",        # strict | assisted
    "progress": {},          # tracks which stages are complete
    "created_at": "...",
    "chat_id": 12345
  }
"""

import re
import logging
from datetime import datetime, timezone

log = logging.getLogger("scout")

# ── In-memory storage (keyed by chat_id → list of workflows) ─────────────
_workflows: dict[int, list] = {}

# ── Event aliases ─────────────────────────────────────────────────────────
_EVENT_ALIASES = {
    "bos":               "bos",
    "break of structure": "bos",
    "break":             "bos",
    "structure break":   "bos",
    "choch":             "choch",
    "choch":             "choch",
    "change of character": "choch",
    "character change":  "choch",
    "sweep":             "sweep",
    "liquidity sweep":   "sweep",
    "sweep highs":       "sweep",
    "sweep lows":        "sweep",
    "hunt":              "sweep",
    "displacement":      "displacement",
    "disp":              "displacement",
    "impulse":           "displacement",
    "ob":                "order_block_retest",
    "order block":       "order_block_retest",
    "ob retest":         "order_block_retest",
    "order block retest": "order_block_retest",
    "fvg":               "fvg_fill",
    "fair value gap":    "fvg_fill",
    "fvg fill":          "fvg_fill",
}

_DIRECTION_WORDS = {
    "bullish": "bullish",
    "bearish": "bearish",
    "long":    "bullish",
    "short":   "bearish",
    "up":      "bullish",
    "down":    "bearish",
    "bull":    "bullish",
    "bear":    "bearish",
}


# ── Workflow parsing ──────────────────────────────────────────────────────

def parse_workflow_from_text(text: str, chat_id: int) -> dict | None:
    """
    Parse a multi-condition workflow from natural language.

    Supports two formats:
      (a) Numbered list steps
      (b) Single-line compound: "Monitor SYN for bearish CHoCH then bearish BOS then sweep"

    Returns a workflow dict or None if unparseable.
    """
    text_lower = text.lower()

    # Extract symbol
    symbol = _extract_symbol(text)
    if not symbol:
        return None

    # Extract bias timeframe
    bias_tf = _extract_tf(text_lower) or "4h"

    # Extract overall direction from words like "bearish SYN" or "I want shorts on"
    overall_direction = _extract_direction(text_lower)

    # ── Parse numbered steps ───────────────────────────────────────────────
    steps = _parse_numbered_steps(text)
    if not steps:
        # Try comma/then-separated inline
        steps = _parse_inline_steps(text_lower)

    if not steps:
        return None

    # ── Classify steps into confirmations vs entry ─────────────────────────
    CONFIRMATION_EVENTS = {"choch", "bos"}
    ENTRY_EVENTS = {"sweep", "displacement", "order_block_retest", "fvg_fill"}

    confirmations = []
    entry         = []

    for step in steps:
        event     = step.get("event")
        direction = step.get("direction") or overall_direction or "bearish"
        if event in CONFIRMATION_EVENTS:
            confirmations.append({"event": event, "direction": direction})
        elif event in ENTRY_EVENTS:
            entry.append({"event": event, "direction": direction})
        else:
            # Unknown — put in entry
            entry.append({"event": event, "direction": direction})

    if not confirmations and not entry:
        return None

    # ── Detect mode ────────────────────────────────────────────────────────
    mode = "assisted" if any(
        w in text_lower for w in [
            "anything else", "if you see anything", "also watch",
            "keep an eye", "broader", "anything important"
        ]
    ) else "strict"

    name = f"{symbol.lower().replace('usdt','')}_{overall_direction or 'watch'}_workflow"

    return {
        "name":          name,
        "symbol":        symbol,
        "bias_tf":       bias_tf,
        "bias":          {"market_structure": overall_direction or "bearish"},
        "confirmations": confirmations,
        "entry":         entry,
        "mode":          mode,
        "progress":      {},
        "created_at":    datetime.now(timezone.utc).isoformat(),
        "chat_id":       chat_id,
        "alerted":       False,
    }


def _extract_symbol(text: str) -> str:
    STOPWORDS = {
        "monitor", "watch", "alert", "track", "me", "and", "for", "when",
        "the", "get", "all", "now", "this", "that", "with", "from", "any",
        "new", "goes", "can", "you", "then", "also", "wait", "tell", "let",
        "know", "if", "see", "on", "h4", "1h", "4h", "15m", "step", "first",
        "second", "third", "bearish", "bullish", "short", "long",
    }
    for word in re.findall(r'\b[A-Za-z]{2,10}\b', text):
        if word.lower() not in STOPWORDS:
            sym = word.upper()
            if not sym.endswith("USDT"):
                sym = sym + "USDT"
            return sym
    return ""


def _extract_tf(text_lower: str) -> str:
    m = re.search(r'\b(4h|1h|15m|5m|1d|daily|hourly)\b', text_lower)
    if m:
        raw = m.group(1)
        return {"daily": "1d", "hourly": "1h"}.get(raw, raw)
    return ""


def _extract_direction(text_lower: str) -> str:
    for word, direction in _DIRECTION_WORDS.items():
        if re.search(r'\b' + re.escape(word) + r'\b', text_lower):
            return direction
    return ""


def _parse_numbered_steps(text: str) -> list:
    """Extract steps from numbered list format: 1. bearish CHoCH  2. BOS  etc."""
    pattern = re.compile(r'\d+[\.\)]\s*(.+?)(?=\n\d+[\.\)]|\Z)', re.DOTALL)
    raw_steps = pattern.findall(text)
    if not raw_steps:
        return []
    steps = []
    for raw in raw_steps:
        raw = raw.strip()
        event     = _match_event(raw.lower())
        direction = _extract_direction(raw.lower())
        if event:
            steps.append({"event": event, "direction": direction})
    return steps


def _parse_inline_steps(text_lower: str) -> list:
    """Parse comma/then-separated: 'bearish choch then bearish bos then sweep'"""
    parts = re.split(r'\bthen\b|,', text_lower)
    steps = []
    for part in parts:
        part  = part.strip()
        event = _match_event(part)
        if event:
            direction = _extract_direction(part)
            steps.append({"event": event, "direction": direction})
    return steps


def _match_event(text: str) -> str:
    for alias, canonical in _EVENT_ALIASES.items():
        if alias in text:
            return canonical
    return ""


# ── Workflow CRUD ─────────────────────────────────────────────────────────

def add_workflow(chat_id: int, workflow: dict) -> str:
    if chat_id not in _workflows:
        _workflows[chat_id] = []

    # Deduplicate by name/symbol combo
    existing_names = {w["name"] for w in _workflows[chat_id]}
    if workflow["name"] in existing_names:
        workflow["name"] = workflow["name"] + "_2"

    _workflows[chat_id].append(workflow)
    _persist()

    sym   = workflow["symbol"].replace("USDT", "")
    mode  = workflow["mode"].upper()
    confs = workflow["confirmations"]
    entry = workflow["entry"]

    lines = [
        f"✅ <b>Workflow saved — {sym}</b>  [{mode} MODE]",
        f"",
        f"Bias TF: {workflow['bias_tf'].upper()}",
        f"",
    ]
    if confs:
        lines.append("Confirmation stage:")
        for c in confs:
            lines.append(f"  • {c['direction'].capitalize()} {c['event'].upper()}")
    if entry:
        lines.append("Entry stage:")
        for e in entry:
            lines.append(f"  • {e['direction'].capitalize()} {e['event'].upper()}")

    lines.append("")
    lines.append("Scout will alert you only when ALL stages complete.")
    if mode == "STRICT":
        lines.append("Strict mode: no unsolicited alerts for other events.")
    else:
        lines.append("Assisted mode: I'll also flag other notable events on this symbol.")

    return "\n".join(lines)


def remove_workflow(chat_id: int, name_or_symbol: str) -> str:
    if chat_id not in _workflows:
        return "No workflows saved."
    before = len(_workflows[chat_id])
    sym    = name_or_symbol.upper().replace("USDT", "")
    _workflows[chat_id] = [
        w for w in _workflows[chat_id]
        if sym not in w["symbol"].upper() and sym not in w["name"].upper()
    ]
    removed = before - len(_workflows[chat_id])
    _persist()
    return f"✅ Removed {removed} workflow(s) for {sym}." if removed else f"No workflow found for {sym}."


def list_workflows(chat_id: int) -> str:
    flows = _workflows.get(chat_id, [])
    if not flows:
        return (
            "No active workflows.\n\n"
            "To define one, describe the full setup sequence:\n"
            "<i>Monitor SYN. H4 bearish. Wait for: 1. Bearish CHoCH 2. Bearish BOS 3. Sweep highs 4. Bearish displacement 5. OB retest</i>"
        )
    lines = ["<b>Your active workflows:</b>\n"]
    for w in flows:
        sym  = w["symbol"].replace("USDT", "")
        prog = w.get("progress", {})
        confs_done = sum(1 for c in w["confirmations"] if prog.get(f"conf_{c['event']}_{c['direction']}"))
        entry_done = sum(1 for e in w["entry"] if prog.get(f"entry_{e['event']}_{e['direction']}"))
        total = len(w["confirmations"]) + len(w["entry"])
        done  = confs_done + entry_done
        bar   = "🟢" if done == total else "🟡" if done > 0 else "⚪"
        lines.append(f"{bar} <b>{sym}</b> — {w['name']} ({done}/{total} stages complete)")
    return "\n".join(lines)


def clear_workflows(chat_id: int) -> str:
    count = len(_workflows.get(chat_id, []))
    _workflows[chat_id] = []
    _persist()
    return f"✅ Cleared {count} workflow(s)."


def get_workflows(chat_id: int) -> list:
    return _workflows.get(chat_id, [])


def get_all_workflows() -> dict:
    return _workflows


def load_workflows(data: dict):
    """Restore from 0G Storage."""
    global _workflows
    for k, v in data.items():
        try:
            _workflows[int(k)] = v
        except (ValueError, TypeError):
            pass


# ── Workflow evaluation ───────────────────────────────────────────────────

def evaluate_workflow(workflow: dict, intel: dict) -> dict:
    """
    Evaluate one workflow against current intelligence.

    Returns:
      {
        "complete": bool,          # all stages done
        "newly_complete": bool,    # just finished in this scan
        "progress": dict,          # which stages are done
        "stage_report": str,       # human-readable progress
        "stage": str               # "confirmation" | "entry" | "done"
      }
    """
    prog = dict(workflow.get("progress", {}))
    bias = intel.get("htf_bias", "Neutral").lower()

    bos      = intel.get("bos", {})
    sweep    = intel.get("sweep", {})
    disp     = intel.get("displacement", {})
    ob       = intel.get("order_block", {})
    fvg      = intel.get("fvg", {})

    def _bos_matches(direction: str) -> bool:
        if not bos.get("broken"):
            return False
        bos_bias = bos.get("bias", "")
        return bos_bias == direction

    def _choch_matches(direction: str) -> bool:
        # CHoCH = BOS in the OPPOSITE direction to prior trend
        # We detect it as a BOS where bias previously was opposite
        # For now: treat CHoCH as BOS in that direction (structure.py doesn't distinguish yet)
        return _bos_matches(direction)

    def _sweep_matches(direction: str) -> bool:
        if not sweep.get("detected"):
            return False
        sw_dir = sweep.get("direction", "")
        return sw_dir == direction

    def _displacement_matches(direction: str) -> bool:
        if not disp.get("confirmed"):
            return False
        # Displacement direction inferred from sweep direction or bos bias
        sw_dir = sweep.get("direction", "")
        bos_bias = bos.get("bias", "")
        disp_dir = sw_dir or bos_bias
        return disp_dir == direction

    def _ob_matches(direction: str) -> bool:
        if not ob.get("found"):
            return False
        ob_type = ob.get("type", "")
        if direction == "bearish":
            return "bearish" in ob_type
        return "bullish" in ob_type

    def _fvg_matches(direction: str) -> bool:
        if not fvg.get("found"):
            return False
        fvg_type = fvg.get("type", "")
        return direction in fvg_type

    EVENT_CHECKERS = {
        "bos":               _bos_matches,
        "choch":             _choch_matches,
        "sweep":             _sweep_matches,
        "displacement":      _displacement_matches,
        "order_block_retest": _ob_matches,
        "fvg_fill":          _fvg_matches,
    }

    # ── Evaluate confirmation stages ──────────────────────────────────────
    for c in workflow.get("confirmations", []):
        key = f"conf_{c['event']}_{c['direction']}"
        if not prog.get(key):
            checker = EVENT_CHECKERS.get(c["event"])
            if checker and checker(c["direction"]):
                prog[key] = True

    # ── Evaluate entry stages ─────────────────────────────────────────────
    # Entry stages only unlock after all confirmation stages are done
    confs_done = all(
        prog.get(f"conf_{c['event']}_{c['direction']}")
        for c in workflow.get("confirmations", [])
    )

    if confs_done:
        for e in workflow.get("entry", []):
            key = f"entry_{e['event']}_{e['direction']}"
            if not prog.get(key):
                checker = EVENT_CHECKERS.get(e["event"])
                if checker and checker(e["direction"]):
                    prog[key] = True

    # ── Check completion ──────────────────────────────────────────────────
    entry_done = all(
        prog.get(f"entry_{e['event']}_{e['direction']}")
        for e in workflow.get("entry", [])
    ) if workflow.get("entry") else True

    was_alerted   = workflow.get("alerted", False)
    newly_complete = confs_done and entry_done and not was_alerted
    complete       = confs_done and entry_done

    # ── Stage report ──────────────────────────────────────────────────────
    report_lines = []
    for c in workflow.get("confirmations", []):
        key  = f"conf_{c['event']}_{c['direction']}"
        tick = "✓" if prog.get(key) else "⏳"
        report_lines.append(f"{tick} {c['direction'].capitalize()} {c['event'].upper()}")
    for e in workflow.get("entry", []):
        key  = f"entry_{e['event']}_{e['direction']}"
        tick = "✓" if prog.get(key) else "⏳"
        report_lines.append(f"{tick} {e['direction'].capitalize()} {e['event'].upper()}")

    if complete:
        current_stage = "done"
    elif confs_done:
        current_stage = "entry"
    else:
        current_stage = "confirmation"

    return {
        "complete":       complete,
        "newly_complete": newly_complete,
        "progress":       prog,
        "stage_report":   "\n".join(report_lines),
        "stage":          current_stage,
        "confs_done":     confs_done,
    }


def format_workflow_alert(workflow: dict, result: dict, intel: dict,
                          plan: dict = None) -> str:
    """Format the final alert message when a workflow completes."""
    sym    = workflow["symbol"].replace("USDT", "").replace("_USDT", "")
    bias   = workflow.get("bias", {}).get("market_structure", "").upper()

    lines = [
        f"🚨 <b>SETUP COMPLETE — {sym} {bias}</b>",
        f"",
        f"Workflow: <b>{workflow['name']}</b>",
        f"",
        f"Stage progress:",
        result["stage_report"],
    ]

    if plan and not plan.get("error"):
        from app.trade_plan import format_trade_plan
        lines.append("")
        lines.append(format_trade_plan(plan))
    else:
        lines.append("")
        lines.append(f"HTF Bias: {intel.get('htf_bias','Neutral')}")
        lines.append(f"Score:    {intel.get('score', 0)}/100")

    return "\n".join(lines)


# ── Persistence ───────────────────────────────────────────────────────────

def _persist():
    try:
        from app.zg_storage import save_state
        # Merge with existing state
        save_state({"workflows": {str(k): v for k, v in _workflows.items()}})
    except Exception as e:
        log.debug(f"workflow _persist failed: {e}")
