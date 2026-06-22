# Scout — Personal SMC Trading Assistant, built on 0G

Scout is a personal Smart Money Concepts (SMC) trading assistant that monitors
MEXC perpetuals 24/7, follows your exact instructions, tracks custom multi-stage
setups, and alerts you only when a meaningful trading condition has actually occurred.

Built for **The Zero Cup 2026**, starting June 18, 2026.

---

## What Scout is

Most bots notify you when something happened. Scout understands what you're
watching for, monitors exact conditions, builds actionable trade plans, and
stays completely silent until those conditions are met.

**Monitoring request:**
> Monitor SYN for bearish BOS.

Scout stores the symbol, the event, and the direction. It only fires on a
bearish BOS. Bullish BOS — ignored. Everything else — ignored.

**Analysis request:**
> Give me setups.

Now Scout scans the market, ranks opportunities by confluence score, and
returns full trade plans — entry zone, stop loss, TP1, TP2, risk/reward,
and confluence evidence. No invented levels. Every number derives from
actual market structure.

---

## How it works

```
User message (Telegram)
        ↓
telegram_bot.py      — intent routing, direction-aware monitor parsing
        ↓
intelligence.py      — builds structured market facts from multi-TF candles
structure.py         — SMC event detection (sweep, BOS, CHoCH, OB, FVG, displacement)
        ↓
trade_plan.py        — deterministic entry/SL/TP generation from market structure
workflow.py          — multi-stage setup engine (Bias → Confirmation → Entry → Alert)
        ↓
reasoning.py         — asks 0G Compute to explain facts and produce setup analysis
        ↓
scanner.py           — background loops: majors, MEXC movers, user watchlists, workflows
        ↓
zg_compute.py        — the only place this app talks to a model, via 0G Compute
zg_storage.py        — alert log, watchlist state, workflow progress on 0G Storage
```

---

## Core design principles

### Monitoring ≠ Analysis

These are separate paths. A monitor request stores conditions and stays silent
until they fire. An analysis request scans the market and returns a full setup.
Scout never conflates them.

### Direction-aware (strict mode by default)

Every monitor stores the direction with the condition:

```json
{
  "symbol": "SYNUSDT",
  "event": "bos",
  "direction": "bearish",
  "strict": true
}
```

A bearish monitor only fires on a bearish event. Mismatches are silently ignored.
Assisted mode unlocks only when the user explicitly grants it.

### No invented levels

`trade_plan.py` derives every entry/SL/TP from detected OB zones, FVG midpoints,
sweep wicks, and ATR. The model explains what the data shows — it never
invents price levels.

---

## Features

### Major asset engine
BTC, ETH, SOL, TON, BNB, XRP, SUI always monitored in the background.
- 4H = Bias
- 1H = Confirmation
- 15M = Entry

Each classified as: Bullish Trend / Bearish Trend / Continuation / Trend Change / Range / No Trade.
Alerts fire on Trend Change and high-score Continuations.

### Direction-aware monitoring
```
Monitor SYN for bearish BOS
→ Watching: SYNUSDT [BEARISH] [STRICT]
  Conditions: bearish bos
```
Only fires on bearish BOS. Silent on everything else.

### Custom SMC workflows
Define complete multi-stage setup sequences:
```
Monitor SYN. H4 bearish. Wait for:
1. Bearish CHoCH
2. Bearish BOS
3. Sweep highs
4. Bearish displacement
5. OB retest
```
Scout tracks each stage across scan cycles. Alerts only when all complete.

### Full trade plans
Every setup includes:
```
🔴 SYN SHORT

Bias:    Bearish  |  Regime: Expansion
Entry:   0.2740 – 0.2765
SL:      0.2688
TP1:     0.2880  (RR 1:2.1)
TP2:     0.3020  (RR 1:3.4)

Confluence:
  ✓ Bearish BOS confirmed
  ✓ Liquidity sweep — equal highs swept
  ✓ Displacement confirmed
  ✓ Order Block at 0.2738–0.2766

Confidence: High  (82/100)
```

### MEXC gainers/losers scanner
Monitors continuously for +5/10/15% and -5/10/15% moves.
Classifies each as:
- Gainers: `TREND_CONTINUATION` or `POSSIBLE_EXHAUSTION`
- Losers: `CAPITULATION` or `REVERSAL_CANDIDATE`

### Auto setup ranking
`/setups` scans the full watchlist, scores by confluence, and returns
ranked trade plans for the top opportunities.

---

## Commands

| Command | Description |
|---|---|
| `/setups` | Ranked SMC setups with full trade plans |
| `/majors` | BTC/ETH/SOL/TON/BNB/XRP/SUI snapshot |
| `/bias SOL` | Full SMC analysis for any symbol |
| `/scan` | Run structure scan across watchlist now |
| `/crimes` | MEXC top 20 gainers + losers with classification |
| `/movers` | Top 5 MEXC movers quick view |
| `/funding BTC` | Funding rate intelligence |
| `/watchlist` | Your active monitors |
| `/workflows` | Your active custom workflows |
| `/history` | Alert log from 0G Storage |

---

## Natural language

**Simple monitor (strict):**
```
Monitor SYN for bearish BOS
Watch ETH 4H for bullish sweep
Stop monitoring BTC
```

**Assisted mode (add explicit permission):**
```
Monitor SOL for bearish BOS and tell me if you see anything else important
```

**Multi-stage workflow:**
```
Monitor SYN. H4 bearish. Wait for:
1. Bearish CHoCH
2. Bearish BOS
3. Sweep highs
4. OB retest
```

**Analysis:**
```
What's happening with BTC?
Give me setups right now
Best opportunities
```

---

## Why 0G is load-bearing here

Every explanation, setup analysis, and mover commentary is generated by a
real-time call through 0G Compute (`app/zg_compute.py`). There is no other
LLM in this codebase, no cached answers, no fallback to a hosted API.

The intelligence engine (`intelligence.py`, `structure.py`) extracts
structured facts from market data using deterministic logic — sweep
detection, BOS classification, OB identification, ATR scoring. 0G Compute
then explains those facts and produces the narrative. Remove 0G and Scout
has no reasoning layer.

State (watchlists, workflows, alert history) is persisted to 0G Storage
so nothing is lost between restarts.

---

## Repo layout

```
app/
  intelligence.py    multi-TF SMC intelligence builder (sweep, BOS, OB, FVG, regime)
  structure.py       SMC event detection engine
  trade_plan.py      deterministic entry/SL/TP generator from market structure
  workflow.py        multi-stage setup engine with progress tracking
  scanner.py         background loops: majors, MEXC movers, watchlists, workflows
  reasoning.py       0G Compute prompting — analysis, setups, explanations
  telegram_bot.py    command handling, direction-aware monitor parsing
  market_data.py     multi-TF candle fetching, funding rates
  mexc_data.py       MEXC perpetuals: tickers, funding, crime scanner
  zg_compute.py      0G Compute client — the only model access point
  zg_storage.py      0G Storage — state persistence
zg_sidecar/          Node service wrapping the 0G Compute broker SDK
main.py              entrypoint + startup health check
SETUP_0G.md          0G Compute setup walkthrough
ARCHITECTURE.md      full architecture diagram
```

---

## Setup

1. Set up the 0G Compute sidecar — follow `SETUP_0G.md`
   ```
   cd zg_sidecar && npm install && npm start
   ```
2. Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Create a Telegram bot via [@BotFather](https://t.me/BotFather)
4. Copy `.env.example` to `.env` and fill in your tokens
5. Run:
   ```
   python main.py
   ```

---

## Deployment (Google Cloud / any Linux server)

```bash
git pull
sudo systemctl restart scout
```

Or direct:
```bash
git pull
python main.py
```
