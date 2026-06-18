# Setting up 0G Compute (one-time, do this in GitHub Codespaces)

This is a one-time provisioning step. Once it's done you'll have two values
(`ZG_SERVICE_URL` and `ZG_API_SECRET`) that go in `.env` — after that, the
running app never touches Node, a wallet, or the CLI again. All runtime
calls are plain Python `requests` against an OpenAI-compatible REST endpoint.

## 1. Open a Codespace on this repo, then in the terminal:

```bash
node -v        # confirm Node 20+ (Codespaces default image usually has this)
npm install -g @0glabs/0g-serving-broker
```

## 2. Get a testnet wallet funded with OG tokens

You need an EVM-compatible wallet (just a private key — you can generate
one with any wallet tool, or `npx ethers-cli wallet create` style tooling,
or even a throwaway MetaMask wallet). **Do not reuse a wallet that holds
real funds — this is testnet only, but treat the key like a real secret.**

1. Get the wallet's address and private key.
2. Go to **faucet.0g.ai**, paste the address, claim testnet OG tokens.
3. In the Codespace terminal:
   ```bash
   export PRIVATE_KEY=0x...your testnet private key...
   ```

## 3. Provision your compute account

```bash
0g-compute-cli setup-network
0g-compute-cli login
0g-compute-cli deposit --amount 3
0g-compute-cli inference list-providers
```

The last command lists available inference providers — note one address.
The Qwen 2.5 7B Instruct testnet provider used in this build is:

```
0xa48f01287233509FD694a22Bf840225062E67836
```

(Double-check this is still listed and active when you run `list-providers`
— provider addresses can change. If it's gone, pick any listed provider and
update `ZG_MODEL` in `.env` to match the model it serves.)

## 4. Fund that provider and get your API secret

```bash
export PROVIDER=0xa48f01287233509FD694a22Bf840225062E67836

0g-compute-cli transfer-fund --provider $PROVIDER --amount 1
0g-compute-cli inference acknowledge-provider --provider $PROVIDER
0g-compute-cli inference get-secret --provider $PROVIDER
```

That last command prints something like:

```
Service URL: https://...
API Secret: app-sk-...
```

## 5. Put both values in `.env`

```
ZG_SERVICE_URL=<the service URL from step 4>
ZG_API_SECRET=<the app-sk-... secret from step 4>
ZG_MODEL=qwen/qwen-2.5-7b-instruct
```

## 6. Verify it works

```bash
pip install -r requirements.txt
python -c "from app import zg_compute; print(zg_compute.health_check())"
```

You should see `{'ok': True, 'model': 'qwen/qwen-2.5-7b-instruct', 'raw': 'ok'}`.
If `ok` is `False`, the error message will say why — most common issues are
an unfunded provider (re-run `transfer-fund`) or an expired/wrong secret
(re-run `get-secret`).

## Checking your balance anytime

```bash
0g-compute-cli get-account
```

or view the dashboard at **compute-marketplace.0g.ai**.
