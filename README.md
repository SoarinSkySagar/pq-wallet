# PQ-Vault

**A 2-of-3 post-quantum guardian wallet for EIP-8141 frame transactions, plus research on keeping *stateful*
hash-based signatures safe through crashes, stale backups, cloned devices and rollback.**

Built for ETHGlobal Mumbai 2026. Devnet only — research prototype, not audited, never use real funds.

- **No ECDSA in the wallet.** Every approval is a JARDIN hash-based signature (SPHINCS-/FORS family,
  keccak-based) carried in an EIP-8141 `ARBITRARY` signature entry and checked against the protocol's
  canonical signature hash `TXPARAM(0x08)`. The only ECDSA key in the repo is a throwaway devnet
  deployer that publishes contracts.
- **No custom cryptography.** Signing and verification are the upstream JARDIN reference code
  ([`nconsigny/JARDIN`](https://github.com/nconsigny/JARDIN), pinned as `third_party/JARDIN`, unmodified):
  `JardinForsPlainVerifier` / `JardinSpxVerifier` on-chain, the JARDIN Python signers off-chain.
- **Official workshop tooling.** Frame-transaction layout, opcode helpers and RPC flow follow the
  ETHGlobal Mumbai alt-mempool workshop (`forshtat/mempool-workshop`, `@forshtat/hegota-support`,
  `forshtat/mempool-workshop-sidecar`, `forshtat/ethrex@eip8141-tracer`).

## What it does

| Feature | How |
|---|---|
| Guardian authorization (2-of-3) | Three frames, the ERC-8421 `[only_verify, pay]` prefix plus one body frame. Frame 0 `VERIFY` (scope execution) → `validate()` verifies guardian A; frame 1 `VERIFY` (scope payment) → `validate()` verifies guardian B (compact FORS *or* stateless SPX); both pin the frame list and cap the fee. Frame 2 `SENDER` → `execute()` consumes both leaves and runs the batch atomically. Both signatures are verified before payment is approved: a missing, corrupted, stale or duplicate approval makes the transaction invalid and costs the vault nothing. Both guardians sign the same message, independently and in any order. |
| Replay protection | Protocol nonce + `TXPARAM(0x08)` (chain id, sender, nonce, fees, all frames) + domain tag; distinct guardians; and per-leaf replay protection through the **LeafLedger** (a stateful leaf can never be accepted twice). |
| Key rotation / slot refresh | A compact slot holds 128 one-time leaves. `registerSlot` / `revokeSlot` give a guardian a fresh slot (new randomness), approved by the other two. The driver does it **automatically**: before a guardian approves, a slot with ≤ 8 leaves left (or revoked) is rotated first; a guardian with no compact leaf left approves with its stateless SPX key, so neither transfers nor rotations get stuck (`--rotate-at`, `rotate <g> [--stateless a]`). |
| Key-loss recovery | `replaceGuardian`: the two remaining guardians install new master + slot keys; bumping the guardian epoch revokes every old slot. |
| Stateful-signature safety (research result) | LeafLedger high-water marks checked in `VERIFY`, consumed in `SENDER`, chain-anchored signer, in-flight window, and permissionless **equivocation reports** that revoke a slot that signed two messages with one leaf. |
| ERC-8421 alt mempool | `VERIFY` reads the shared LeafLedger — storage not associated with the sender, which the EIP-8141 canonical mempool refuses and the alt mempool admits for a **staked** sender; the vault stakes itself in its constructor. |
| Validation budget (100k cap) | Each VERIFY frame costs ~84k on chain (compact; ~292k stateless), so the prefix (~170k, 200,200 declared) exceeds EIP-8141's combined `MAX_VERIFY_GAS` (100k) — two upstream JARDIN verifications alone are ~109k. ERC-8421 `[BUDGET-010]` gives each **staked** entity its own 1,000,000 budget, and both frames belong to the staked vault. The stock workshop sidecar image does not implement it; `infra/sidecar-budget010/` adds it (patch + 7 tests + image build), and `infra/local-net.sh` runs it. |

## Layout

```
contracts/          Foundry project: PQVault, PQVaultFactory, LeafLedger, PQVaultFrameOps.yul (+ tests)
pqv/                Python toolkit: frame-tx codec, JARDIN key wrapper, leaf-state stores, devnet driver, UI server
app/                Wallet website: balance, send with two guardians (shown frame by frame), guardian key health
                    as 128-leaf grids, renew / replace guardians, activity. No external scripts, fonts or trackers
mobile/             PQ Wallet phone app (React Native / Expo): a remote for the personal wallets, keys stay on the computer
research/           Simulator, crash experiment, results/, REPORT.md
infra/              local-net.sh: one-command local network (Kurtosis ethrex eip8141-tracer + Lighthouse + skandha);
                    sidecar-budget010/ (ERC-8421 per-entity budgets for the skandha sidecar)
third_party/        JARDIN and SPHINCS- (git submodules, pinned)
```

## Quick start

Requirements: Foundry, Python ≥ 3.10 linked against OpenSSL ≥ 3.2 (for native `KECCAK-256`; e.g. Homebrew
`python3.12`, or a uv-managed CPython on Linux), `git submodule update --init`. No pip packages are needed.
Vault transactions need an ERC-8421 mempool with per-entity budgets ([BUDGET-010]); `infra/local-net.sh` brings
up a local network with one (Kurtosis, Docker, x86_64).

**Demo in one command:** `./demo.sh` brings up (or resumes, after a Docker restart) the local network and the
[BUDGET-010] mempool, starts the alice / bob / carol wallets with phone access, creates any missing accounts, tops
up low balances, starts the explorer, and prints every URL and pairing code. Re-runnable; `./demo.sh status`,
`./demo.sh stop [--chain]`, `./demo.sh up --fresh` (new chain, ~10 min), `--no-lan`, `--wallets "a b c"`.

```sh
# contracts + tests
(cd contracts && forge build && forge test)
python3.12 -m unittest discover -s tests -t .

# research
python3.12 research/run_experiments.py --seeds 20     # -> research/results/experiments.{json,md}
python3.12 research/crash_test.py --kills 150         # -> research/results/crash_test.json

# local network: Kurtosis ethrex (eip8141-tracer) + Lighthouse + skandha [BUDGET-010] sidecar, funded deployer,
# infra contracts; waits for Hegota (~10 min). `up stock` runs the stock sidecar instead (it rejects the vault).
infra/local-net.sh up
python3.12 -m pqv.devnet local new-guardians
python3.12 -m pqv.devnet local deploy-vault --fund 2 --stake 0.02                    # staked 2-of-3 vault
python3.12 -m pqv.devnet local transfer 0x<to> 1000000000000000 --guardians 0,1 [--stateless 1]
python3.12 -m pqv.devnet local rotate 0 && python3.12 -m pqv.devnet local recover 2
python3.12 -m pqv.devnet local attack-stale-restore
python3.12 -m pqv.server local       # wallet website on http://127.0.0.1:8141 (signing stays in this process)
infra/local-net.sh status | down
```

### Block explorer for frame transactions

```sh
python3.12 -m pqv.explorer            # http://127.0.0.1:8300
```

Standard explorers do not understand EIP-8141 frame transactions. This one lists blocks and transactions and opens
each frame transaction frame by frame: a strip of the frames sized by gas, then for each frame its mode (VERIFY /
SENDER), the approval it grants, its target, the decoded call (including the vault's batch), gas used of its limit,
status, and the logs it emitted (`LeafConsumed`, `Executed`, EIP-7708 ETH transfers…). Signature entries are decoded
too: which guardian (by wallet name for shared vaults), compact one-time leaf or stateless SPX, and which leaf of its
slot. Addresses are named from the local devnet state (contracts, vaults, wallet accounts); search accepts a block
number, hash, address or name.

### Personal wallets for a demo (MetaMask-style)

```sh
infra/wallets.sh start alice bob carol   # -> http://127.0.0.1:8201, :8202, :8203 (one browser window each)
infra/wallets.sh status | stop
```

Each wallet is one person's account: a 2-of-3 PQ-Vault smart account whose three post-quantum keys stay on this
computer (`.devnet/local/wallets/<name>/`). The first visit offers **Create wallet** (keys, account and 1 test ETH,
about 20 s). Then: balance, **Send** (pick another wallet or paste an address, review with the network fee, confirm),
**Receive**, **Add test ETH**, activity (sent and received, read from the chain), key health, and network settings
(node RPC and alt-mempool URLs). Each account gets its own colour and avatar, so side-by-side windows stay distinct.

**Shared multisig vaults.** From the Vaults tab a wallet creates a 2-of-3 PQ multisig vault with two other wallets
as co-guardians. They accept the invitation in their own windows (each gets a dedicated post-quantum key for the
vault; only public keys are shared), then the vault is created on chain. Any guardian proposes a payment, which
approves it with their key; when a second guardian approves in their wallet, the payment is sent as a three-frame
transaction (VERIFY guardian A, VERIFY guardian B, SENDER). Anyone can deposit by sending to the vault. The wallets
exchange invitations, public keys and signatures through `.devnet/local/vaults/` (a local stand-in for a relay;
it never holds a secret key).

**On a phone (installable app).** The wallet page is a mobile web app (manifest, service worker, home-screen icon,
bottom tab bar). Start the wallets with phone access and pair a phone:

```sh
infra/wallets.sh start --lan alice bob carol   # phones: https://<lan-ip>:9201, :9202, :9203
infra/wallets.sh pair alice                    # link, pairing code and a QR code in the terminal
```

The same QR code and code are on the wallet's **Phone** screen on the computer. Once per phone, install the devnet
CA (`https://<lan-ip>:9201/ca.crt`, file `.devnet/tls/ca.crt`) so the phone trusts the wallet's HTTPS certificate,
then scan the QR code and choose *Install app* / *Add to Home Screen*. The phone is only a screen: the keys and all
signing stay in the wallet process on the computer, and every API call from the LAN must carry that wallet's pairing
code (20 wrong codes lock the LAN API for a minute). Android over USB needs no certificate or code:
`adb reverse tcp:8201 tcp:8201`, then open `http://localhost:8201` on the phone. The loopback listener on
`127.0.0.1` is unchanged. Icons: `python3 infra/make_app_icons.py`.

**Mobile app (React Native / Expo, from any network).** `mobile/` is a native app for the same wallets: balance,
send with the fee and why, receive (QR), keys, shared vaults (accept, propose, approve), settings for the node RPC
and alt-mempool URLs. It runs in Expo Go, so no Android Studio or Xcode is needed. The chain and the wallets keep
running on this computer; ngrok exposes a wallet to the phone:

```sh
./demo.sh                      # network, mempool, wallets, explorer
./demo.sh tunnel alice --app   # ngrok: alice's wallet + the app's dev server; prints a pairing QR code
                               # (--url <your-ngrok-domain> for a URL that survives restarts; --explorer; several names)
./demo.sh app --ngrok          # Expo dev server (port 8085, no Expo login): open its exp:// link in Expo Go
                               # same Wi-Fi: ./demo.sh app; Android over USB: ./demo.sh app --usb
```

In the app, choose **Scan QR code** and scan the pairing QR (it is also on the wallet's **Phone** screen while the
tunnel runs), or type the URL and the code. The keys stay on this computer: JARDIN signing exists only in Python, and
reimplementing it on the phone would be custom cryptography. Requests that arrive through a tunnel need the pairing
code even though ngrok delivers them to `127.0.0.1`: the wallet treats a request as remote if it carries forwarding
headers or a Host other than localhost. Settings in the app change the wallet URL (a restarted free ngrok tunnel
gets a new one) and the node RPC / mempool URLs that the wallet computer uses. See [mobile/README.md](mobile/README.md).

Full local-devnet bring-up (including the native arm64 ethrex build for Apple Silicon): see
[research/REPORT.md §10](research/REPORT.md#10-reproduce).

## Results at a glance

- **Three-frame 2-of-3 vault, local network** (`infra/local-net.sh`, ethrex `9aa3645d` + Lighthouse + skandha with
  [BUDGET-010]): compact+compact and compact+stateless transfers, key rotation, key-loss recovery, a transfer by the
  rotated + recovered guardians and the live attack, all status 0x1. VERIFY frames 83,842 / 75,875 gas (compact),
  SENDER 59,809; a transfer is billed 379,513 gas (575,353 with two first-use ledger slots). ethrex's canonical
  mempool and the stock sidecar reject the same transaction (combined 100k cap); the [BUDGET-010] sidecar rejects an
  *unstaked* copy. Receipts: [docs/RECEIPTS-local.md](docs/RECEIPTS-local.md); write-up: REPORT §8.1.
- **Original two-frame design on the public chain-1337 hackathon devnet** (historical; guardian A in VERIFY,
  guardian B in SENDER): 2-of-3 transfers, rotation, recovery, a UI transfer and the live attack, e.g.
  [`0x1a38358a…9cc1`](https://explorer.altmempool.wtf/tx/0x1a38358af26665a862b8a1b27eb9817565f08119d1aea3678d9c5bddaf6c9cc1)
  (block 366). Receipts: [docs/RECEIPTS.md](docs/RECEIPTS.md). The public gateway is no longer a target (REPORT §8.1).
- **Alt mempool is required, measured:** the canonical mempool rejects the vault (`StorageReadNonSender`); the alt
  mempool rejects an unstaked copy (`unstaked payer accessed … LeafLedger slot`) and admits the staked vault.
- **Live attack:** a JARDIN-style vault with no on-chain leaf state included two transactions signed with the
  same few-time leaf after a stale backup restore; the slot was then revoked by a permissionless equivocation
  proof; PQ-Vault refused the same restored signer with `StaleLeaf`.
- **Research:** under stale restores, rollbacks and lost state the baseline reaches γ = 9 (35.6-bit forgery cost)
  and hundreds of double-accepted signatures; PQ-Vault keeps γ = 1 with zero double acceptance. Clones remain
  γ = n, bounded to ~1 block of exposure by equivocation revocation. A persist-after-sign signer under fault
  injection reached γ = 100. Details and limitations: [research/REPORT.md](research/REPORT.md).

See [research/REPORT.md](research/REPORT.md) for the research write-up, [docs/RECEIPTS-local.md](docs/RECEIPTS-local.md)
for the current receipts, and [demo/pq-vault-demo.mp4](demo/pq-vault-demo.mp4) (2:56) for the demo video of the
original design (narration: [demo/NARRATION.md](demo/NARRATION.md); rebuilt by `python3.12 demo/build_demo.py`).

## CROPS

- **C**ensorship resistance: transactions go to the permissionless ERC-8421 alt mempool; admission depends on
  published rules and an on-chain stake, not on a relayer's discretion.
- **O**pen source: MIT (this repo); dependencies are MIT/Apache upstream code, pinned.
- **P**rivacy: the UI makes no third-party requests and has no analytics; keys never leave the local process.
- **S**ecurity (lead property): post-quantum hash-based authorization with no ECDSA fallback, and on-chain
  protection against the failure mode stateful hash-based signatures are known for (state reuse).

## License

MIT for code in this repository. `third_party/` keeps its upstream licenses (JARDIN Solidity files are
MIT-tagged; SPHINCS- is MIT). `contracts/src/external/NaiveStakingRegistry.sol` and
`infra/kurtosis/eip8141-single-node.yaml` are copied from forshtat/mempool-workshop-sidecar (MIT).
