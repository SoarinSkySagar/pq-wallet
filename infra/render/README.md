# Deploying the Leafguard web service on Render

What runs where:

| | Where |
|---|---|
| Chain (ethrex + Lighthouse) and the ERC-8421 alt mempool ([BUDGET-010] sidecar) | your server: `./start.sh --local` |
| Wallets (alice, bob, carol: keys + signing), explorer, gateway | Render, this Docker image (`infra/render/Dockerfile`) |

The service serves the explorer at `/` and alice's wallet at `/wallet` (pairing code required; the phone app
uses the service URL + the code). Bob's and carol's wallets run inside the container too (contacts, shared vaults).

## 1. On the chain server

```sh
./start.sh --local                                 # chain, mempool, contracts (and local wallets)
infra/render/export-env.sh <server public IP>      # prints ETHREX_RPC, SIDECAR_RPC, DEPLOYMENT_JSON, DEPLOYER_KEY
```

Open the two ports it names (node RPC, alt mempool) in the server's firewall so Render can reach them.

## 2. On Render

1. New → **Blueprint** → this repository (Render reads `render.yaml`; git submodules are needed: `third_party/JARDIN`).
2. Paste the four values from step 1; optionally set `ALICE_PAIR_CODE` (e.g. `K7QM-P3XA-WN9D`).
3. Deploy. The log ends with `READY … pairing code …` about 30 seconds after start.

Without the disk in `render.yaml` (free plan) every restart creates new accounts, funded by the deployer.
A fresh chain on the server (new contracts) needs the new `DEPLOYMENT_JSON` and `DEPLOYER_KEY` from step 1.

## Locally

```sh
docker build -f infra/render/Dockerfile -t leafguard-web .
infra/render/export-env.sh host.docker.internal > leafguard.env
docker run -p 10000:10000 --add-host host.docker.internal:host-gateway --env-file leafguard.env leafguard-web
```

Devnet only, never real funds: `DEPLOYER_KEY` is the throwaway devnet deployer.
