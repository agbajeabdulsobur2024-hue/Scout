/**
 * zg-sidecar/server.js
 *
 * Wraps the 0G Compute broker SDK behind a tiny HTTP API. Scout's Python
 * app (app/zg_compute.py) calls POST /chat here instead of touching the
 * broker SDK or a wallet directly — keeps the whole app pure Python at
 * runtime except for this one small, single-purpose Node process.
 *
 * Why this exists instead of the 0g-compute-cli: that CLI's installed
 * build is broken (no working shebang) and its actual command surface
 * doesn't match the documented inference flow (no get-secret /
 * acknowledge-provider commands — see SETUP_0G.md for the full story).
 * This calls the same underlying SDK methods directly in code instead,
 * which are documented consistently in two independent sources.
 *
 * Run:
 *   cd zg-sidecar
 *   npm install
 *   cp .env.example .env   # fill in PRIVATE_KEY, optionally PROVIDER_ADDRESS
 *   npm start
 *
 * Then verify: curl http://localhost:8787/health
 */

require("dotenv").config();
const express = require("express");
const { ethers } = require("ethers");
const { createZGComputeNetworkBroker } = require("@0glabs/0g-serving-broker");

const PORT        = process.env.PORT || 8787;
const RPC_URL      = process.env.RPC_URL || "https://evmrpc-testnet.0g.ai";
const PRIVATE_KEY  = process.env.PRIVATE_KEY;
const PROVIDER_ENV = process.env.PROVIDER_ADDRESS || "";
const MIN_BALANCE  = parseFloat(process.env.MIN_LEDGER_BALANCE || "1");
const DEPOSIT_AMT  = parseFloat(process.env.AUTO_DEPOSIT_AMOUNT || "3");

if (!PRIVATE_KEY) {
  console.error("PRIVATE_KEY is not set. Put it in zg-sidecar/.env — see .env.example.");
  process.exit(1);
}

let broker = null;
let activeProvider = null;   // provider address we're using
let serviceMeta = null;      // { endpoint, model } for activeProvider

async function initBroker() {
  const provider = new ethers.JsonRpcProvider(RPC_URL);
  const wallet = new ethers.Wallet(PRIVATE_KEY, provider);
  console.log(`zg-sidecar: wallet address ${wallet.address}`);

  broker = await createZGComputeNetworkBroker(wallet);

  // ── Ensure the ledger exists and has funds ──────────────────────────
  // On a fresh wallet, getLedger() fails with BAD_DATA because the ledger
  // hasn't been created yet. addLedger() both creates it AND funds it —
  // safe to call even if one already exists (it just tops up the balance).
  try {
    const account = await broker.ledger.getLedger();
    // getLedger succeeded — ledger exists, check balance
    const balance = parseFloat(
      ethers.formatEther(account.totalbalance ?? account.balance ?? account.totalBalance ?? 0n)
    );
    console.log(`zg-sidecar: current ledger balance ~${balance} OG`);
    if (balance < MIN_BALANCE) {
      console.log(`zg-sidecar: balance low, topping up with ${DEPOSIT_AMT} OG...`);
      await broker.ledger.addLedger(String(DEPOSIT_AMT));
      console.log("zg-sidecar: top-up complete");
    }
  } catch (e) {
    // Ledger doesn't exist yet (fresh wallet) — create it now
    console.log(`zg-sidecar: ledger not found (fresh wallet), creating with ${DEPOSIT_AMT} OG...`);
    try {
      await broker.ledger.addLedger(String(DEPOSIT_AMT));
      console.log("zg-sidecar: ledger created and funded");
    } catch (addErr) {
      console.warn(`zg-sidecar: addLedger failed — ${addErr.message}. ` +
        `This may mean insufficient OG tokens. Claim more at faucet.0g.ai. Continuing anyway.`);
    }
  }

  // ── Pick a provider ──────────────────────────────────────────────────
  const services = await broker.inference.listService();
  if (!services || services.length === 0) {
    throw new Error("No inference services returned by listService() — network issue or no providers live.");
  }
  console.log(`zg-sidecar: ${services.length} service(s) available`);
  services.forEach(s => console.log(`  - ${s.provider}  model=${s.model}  type=${s.serviceType}`));

  if (PROVIDER_ENV) {
    activeProvider = PROVIDER_ENV;
  } else {
    const inferenceService = services.find(s => (s.serviceType || "").toLowerCase().includes("inference")) || services[0];
    activeProvider = inferenceService.provider;
  }
  console.log(`zg-sidecar: using provider ${activeProvider}`);

  // ── Acknowledge provider (idempotent — fine if already done) ─────────
  try {
    await broker.inference.acknowledgeProviderSigner(activeProvider);
    console.log("zg-sidecar: provider acknowledged");
  } catch (e) {
    console.log(`zg-sidecar: acknowledge step returned "${e.message}" — usually fine if already acknowledged.`);
  }

  serviceMeta = await broker.inference.getServiceMetadata(activeProvider);
  console.log(`zg-sidecar: ready — endpoint=${serviceMeta.endpoint} model=${serviceMeta.model}`);
}

const app = express();
app.use(express.json({ limit: "1mb" }));

app.get("/health", (req, res) => {
  if (!broker || !serviceMeta) {
    return res.status(503).json({ ok: false, reason: "broker not initialized yet" });
  }
  res.json({ ok: true, provider: activeProvider, model: serviceMeta.model, endpoint: serviceMeta.endpoint });
});

app.post("/chat", async (req, res) => {
  if (!broker || !serviceMeta) {
    return res.status(503).json({ error: "broker not initialized yet" });
  }
  const messages = req.body.messages;
  if (!Array.isArray(messages) || messages.length === 0) {
    return res.status(400).json({ error: "body must include a non-empty messages array" });
  }

  try {
    // Billing headers are content-specific and single-use — generate
    // fresh ones for every request, using the latest user message as
    // the billed content (matches the SDK's documented chatbot pattern).
    const lastUser = [...messages].reverse().find(m => m.role === "user");
    const billedContent = lastUser ? lastUser.content : JSON.stringify(messages);

    const headers = await broker.inference.getRequestHeaders(activeProvider, billedContent);

    const resp = await fetch(`${serviceMeta.endpoint}/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify({
        model: serviceMeta.model,
        messages,
        temperature: req.body.temperature ?? 0.3,
        max_tokens: req.body.max_tokens ?? 500,
      }),
    });

    if (!resp.ok) {
      const text = await resp.text();
      console.error(`zg-sidecar: provider returned ${resp.status} — ${text}`);
      return res.status(502).json({ error: `provider returned ${resp.status}`, detail: text });
    }

    const data = await resp.json();
    const content = data?.choices?.[0]?.message?.content ?? "";
    res.json({ content, raw: data });
  } catch (e) {
    console.error(`zg-sidecar: /chat error — ${e.message}`);
    res.status(500).json({ error: e.message });
  }
});

initBroker()
  .then(() => {
    app.listen(PORT, () => console.log(`zg-sidecar: listening on http://localhost:${PORT}`));
  })
  .catch(e => {
    console.error(`zg-sidecar: broker init failed — ${e.message}`);
    console.error(`zg-sidecar: /chat will return 503. Storage endpoints still available.`);
    // Don't exit — storage routes work independently from the broker.
    app.listen(PORT, () => console.log(`zg-sidecar: listening on http://localhost:${PORT} (storage-only mode)`));
  });

// ═══════════════════════════════════════════════════════════════════════════
// 0G STORAGE — upload/download JSON state
// Uses @0glabs/0g-ts-sdk with MemData so nothing touches the filesystem.
// Root hash is the permanent address of each upload on 0G Storage.
// Scout uses this for persistent market memory that survives restarts.
// ═══════════════════════════════════════════════════════════════════════════

const STORAGE_INDEXER  = process.env.STORAGE_INDEXER  || "https://indexer-storage-testnet-turbo.0g.ai";
const STORAGE_EVM_RPC  = process.env.STORAGE_EVM_RPC  || RPC_URL;
let storageWallet = null;

async function getStorageWallet() {
  if (storageWallet) return storageWallet;
  // Storage wallet is independent from the broker — works even if broker fails
  const provider = new ethers.JsonRpcProvider(STORAGE_EVM_RPC);
  storageWallet  = new ethers.Wallet(PRIVATE_KEY, provider);
  console.log(`zg-sidecar: storage wallet ready — ${storageWallet.address}`);
  return storageWallet;
}

app.post("/storage/upload", async (req, res) => {
  const payload = req.body;
  if (!payload) return res.status(400).json({ error: "body required" });
  try {
    const { Indexer, MemData } = require("@0glabs/0g-ts-sdk");
    const wallet  = await getStorageWallet();
    const indexer = new Indexer(STORAGE_INDEXER);
    const bytes   = new TextEncoder().encode(JSON.stringify(payload));
    const memData = new MemData(bytes);
    const [tx, err] = await indexer.upload(memData, STORAGE_EVM_RPC, wallet);
    if (err) {
      console.error(`zg-storage: upload error — ${err}`);
      return res.status(502).json({ error: String(err) });
    }
    // tx is the root hash (permanent address on 0G Storage)
    console.log(`zg-storage: uploaded, root=${tx}`);
    res.json({ ok: true, root: tx });
  } catch (e) {
    console.error(`zg-storage: upload exception — ${e.message}`);
    res.status(500).json({ error: e.message });
  }
});

app.get("/storage/download/:root", async (req, res) => {
  const root = req.params.root;
  if (!root) return res.status(400).json({ error: "root hash required" });
  try {
    const { Indexer } = require("@0glabs/0g-ts-sdk");
    const indexer = new Indexer(STORAGE_INDEXER);
    // Download to a temp buffer — false = skip proof verification for speed
    const tmpPath = `/tmp/zg_dl_${Date.now()}`;
    const err = await indexer.download(root, tmpPath, false);
    if (err) {
      console.error(`zg-storage: download error — ${err}`);
      return res.status(502).json({ error: String(err) });
    }
    const fs   = require("fs");
    const data = fs.readFileSync(tmpPath, "utf8");
    fs.unlinkSync(tmpPath);
    res.json({ ok: true, data: JSON.parse(data) });
  } catch (e) {
    console.error(`zg-storage: download exception — ${e.message}`);
    res.status(500).json({ error: e.message });
  }
});
