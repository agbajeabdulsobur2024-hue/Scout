# Setting up 0G Compute for Scout

**This replaces an earlier version of this file that walked through
`0g-compute-cli`.** That CLI's installed build turned out to be broken
(no working shebang) and its actual command set doesn't match the
documented inference flow — no `get-secret`, no `acknowledge-provider`.
The 0G docs themselves are mid-migration between two SDK generations,
and the current one generates fresh, single-use billing headers on
every request rather than a static reusable API key. That means
*something* has to run the wallet/broker logic per request — that's
what `zg-sidecar/` is. You run it once, leave it running, and never
touch a wallet or the CLI again after that.

## 1. Get a testnet wallet funded with OG tokens

(Skip if you already did this.) Create a throwaway EVM wallet (e.g. a
fresh MetaMask wallet) — **don't reuse one with real funds, this is
testnet only, but treat the private key as a real secret regardless.**
Get the address and private key, then go to **faucet.0g.ai**, paste the
address, claim testnet OG tokens.

## 2. Configure and start the sidecar

In the Codespace terminal:

```bash
cd zg-sidecar
npm install
cp .env.example .env
```

Open `.env` and fill in:

```
PRIVATE_KEY=0x...your testnet private key...
```

Leave `PROVIDER_ADDRESS` blank — the sidecar auto-picks the first
available inference provider on startup and prints which one it chose.

```bash
npm start
```

Watch the console output. You should see something like:

```
zg-sidecar: wallet address 0x...
zg-sidecar: current ledger balance ~0 OG
zg-sidecar: balance below 1, depositing 3 OG...
zg-sidecar: deposit complete
zg-sidecar: N service(s) available
  - 0x...  model=qwen/qwen-2.5-7b-instruct  type=inference
zg-sidecar: using provider 0x...
zg-sidecar: provider acknowledged
zg-sidecar: ready — endpoint=https://... model=qwen/qwen-2.5-7b-instruct
zg-sidecar: listening on http://localhost:8787
```

**If something errors here, paste the exact console output back into
the chat rather than guessing at a fix** — we've already hit several
cases where the SDK's real behavior doesn't match its own docs, so the
actual error message matters more than what any doc says should happen.
A couple of known possibilities:
- *Ledger balance check fails on a brand-new wallet* — the sidecar logs
  a warning and continues; if the deposit step also fails, the ledger
  may need creating once with a different method name in this SDK
  version (`broker.ledger.addLedger(...)` instead of `depositFund`) —
  tell me the exact error and I'll patch `server.js`.
- *No services returned* — means no inference providers are currently
  live on testnet, which is a 0G-side issue, not yours.

## 3. Verify it's reachable

Leave the sidecar running in that terminal. Open a **second** terminal
tab in the same Codespace (don't close the first one) and run:

```bash
curl http://localhost:8787/health
```

You should get back something like
`{"ok":true,"provider":"0x...","model":"qwen/...","endpoint":"https://..."}`.

## 4. Verify Scout's Python side can reach it

Still in that second terminal, from the repo root:

```bash
pip install -r requirements.txt
python -c "from app import zg_compute; print(zg_compute.health_check())"
```

You should see `{'ok': True, 'model': '...', 'provider': '0x...'}`. If
`ok` is `False`, the `reason` field will say why — most likely the
sidecar isn't running (check terminal 1) or `ZG_SIDECAR_URL` in `.env`
doesn't match the port the sidecar actually started on.

## Running both processes together later

For the actual demo, both processes need to be running: the sidecar
(`cd zg-sidecar && npm start`) and the bot (`python main.py`), each in
their own terminal tab. When we deploy this for real (not just local
Codespace testing), they'll need to run as two separate services —
we'll cross that bridge once local testing works.
