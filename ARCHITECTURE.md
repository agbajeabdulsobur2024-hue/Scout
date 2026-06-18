# Scout — Architecture

## Current state (built during the tournament window)

```
┌─────────────┐
│  Telegram   │  thin I/O — no reasoning happens here
└──────┬──────┘
       │ free-text question
       ▼
┌─────────────────┐
│  reasoning.py    │  routes question → symbol lookup or "best opportunities"
└──────┬───────────┘
       │
       ▼
┌─────────────────┐      ┌──────────────────────────┐
│  market_data.py  │ ───▶ │ Binance public REST API  │  live price/volume/funding
└──────┬───────────┘      └──────────────────────────┘
       │ snapshot + deterministic signal_strength score
       ▼
┌─────────────────┐      ┌──────────────────────────┐
│  reasoning.py    │ ───▶ │  zg_compute.py            │  Python — plain HTTP call
│  (builds prompt) │      │  (calls the sidecar)      │
└──────────────────┘      └──────────┬───────────────┘
                                      │ POST /chat
                                      ▼
                           ┌──────────────────────────┐
                           │  zg-sidecar (Node)        │  wallet, broker SDK,
                           │  → 0G Compute broker      │  fresh billing headers
                           │  → Qwen 2.5 7B Instruct   │  per request — the ONLY
                           └──────────┬───────────────┘  model call in this app
       ▲                              │
       └────────── explanation text ──┘
```

Every box above exists and runs. There is no hidden fallback to a normal
LLM API — if the zg-sidecar (`zg-sidecar/`, see below) isn't running or
isn't healthy, `main.py` refuses to start (see the startup health
check), rather than silently degrading to something that isn't 0G.

## Why there's a small Node sidecar

`zg_compute.py` doesn't talk to 0G directly — it calls a small local
Node service (`zg-sidecar/server.js`) over plain HTTP. This isn't
incidental complexity: 0G Compute's current SDK generates billing
headers that are content-specific and single-use, regenerated on every
request via the wallet/broker, not a static reusable API key. Something
has to run that broker logic per request, and the broker SDK is
TypeScript/JS-only. The sidecar is that something — it's the smallest
possible amount of Node needed to keep the rest of the app, and all the
actual reasoning logic, in Python. See `SETUP_0G.md` for the full story
of how this came together, including the dead ends (a CLI-based flow
that didn't match the SDK's real behavior) along the way.

## Why this shape, specifically

The deterministic scoring function exists so that "why did this rank
highly" always has a traceable, non-hallucinated answer — the model's job
is explaining real numbers, not inventing them. That split (cheap
deterministic signal extraction + model does the interpretation) is also
what keeps the integration genuinely load-bearing: if you deleted
`zg_compute.py`, the app could compute a score but couldn't say anything
about it. The reasoning is the product; 0G Compute is where it happens.

## Planned: 0G Storage as real memory (stretch goal)

```
Conversation / explanation
        ↓
zg_storage.py (planned)
        ↓
0G Storage (write)
        │
        ▼  (later, on a follow-up question)
0G Storage (read) ───▶ reasoning.py ───▶ "here's what I told you before, and why"
```

This is explicitly **not built yet**. The honest reason: 0G Storage has no
Python SDK (TypeScript/Rust/Go only), so this requires either a small Node
sidecar service or doing the wallet-signing flow directly — more moving
parts than the Compute integration, on a tight clock. If it ships, it'll
be because the Compute integration (the part judged in this round) was
solid first. We'd rather submit one thing that's real than two things,
one of which is half-finished.

## What's deliberately *not* in this repo

No proprietary trading logic, no liquidity-sweep/SMC detection, no
multi-month pre-existing codebase. `market_data.py`'s signals are simple
on purpose — volume spike, range position, 24h move magnitude — written
fresh for this build. The interesting part of this project is the
reasoning layer running on 0G, not a bigger rules engine underneath it.
