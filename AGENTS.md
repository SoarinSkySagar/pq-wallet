# AGENTS.md — handoff for agents (and humans) picking up Leafguard

Read this first. It is the state of the project as of 2026-11-06, what works, how to run it, where the
non-obvious traps are, and what is left. User-facing docs: `README.md`; research: `research/REPORT.md`;
devnet evidence: `docs/RECEIPTS.md`.

## 1. What this project is

**Leafguard**: a 2-of-3 post-quantum guardian wallet on **EIP-8141 frame transactions** submitted through the
**ERC-8421 alternative mempool**, using **JARDIN** hash-based signatures (SPHINCS- family). Built for ETHGlobal
Mumbai 2026. Plus **research** on whether *stateful* hash-based signing survives crashes, stale backups, cloned
devices, interrupted signing and state rollback — with attacks, a mitigation, and measurements.
Formerly **PQ-Vault**: user-facing text says Leafguard; code identifiers (`pqv/`, `PQVault*`, `.devnet/`, the
`pqvault/...` sidecar image, the `pqvault-devnet` enclave, the wallet page's pairing-code storage key) and the
domain tags `PQ-Vault/v2` / `PQ-Vault/slot/v1` keep the old name on purpose: changing a domain tag changes every
signature and derived key. `docs/RECEIPTS*.md` are generated and keep the name they were generated with.

Hard rules the user set (keep them):
- **No ECDSA in the wallet** (the only ECDSA key is the throwaway devnet *deployer* that publishes contracts).
- **No custom cryptography** — signing/verification is upstream JARDIN code, unmodified (`third_party/JARDIN`).
- **Never fabricate results**; every number in docs must come from code in this repo. **Don't claim novelty**
  without evidence ("not found in the sources we reviewed" is the strongest allowed phrasing).
- **Devnet only, never real funds.**

## 2. Current state

| Area | State |
|---|---|
| Contracts (`contracts/src`) | 2-of-3 `PQVault`, three frames only: VERIFY (guardian A) + VERIFY (guardian B) + SENDER (consume both leaves, run batch); `PQVaultFactory`, `LeafLedger`, `PQVaultFrameOps.yul` (introspection with `maxCopy`). Foundry: **26/26 pass** |
| Python toolkit (`pqv/`) | frame-tx codec (matches ethrex golden vector), JARDIN wrapper, leaf stores, three-frame devnet driver, UI server, receipts generator. **44/44 unittest pass** |
| Alt mempool | `infra/sidecar-budget010/`: ERC-8421 [BUDGET-010] per-entity budgets for the workshop sidecar (patch, 7 new tests, 393/393 executor tests pass, image build). Draft upstream PR text in its README, **not submitted** |
| Public hackathon devnet (chain 1337) | **No longer a target.** Historical receipts of the original two-frame design (`docs/RECEIPTS.md`). A predecessor of the current contract was deployed there 2026-11-06 (vault `0x9635f90861b370f6bd28fd98ddb1c1ae470b880f`; secrets only in `.devnet/public/` on Sagar's machine): its gateway admitted a three-frame tx but included nothing in 15 min (REPORT §8.1) |
| Local network (`infra/local-net.sh`) | **The target.** One command: Kurtosis ethrex eip8141-tracer + Lighthouse + skandha sidecar ([BUDGET-010] or stock). Current run: `docs/RECEIPTS-local.md` (transfers incl. SPX, rotation, recovery, attack; stock/unstaked rejections). Older runs (superseded code) are in git history and `.devnet/local/archive-*` |
| Research | `research/REPORT.md` complete (§1–10), results in `research/results/*.json|md` |
| Demo | `demo/pq-vault-demo.mp4` (2:55), `demo/NARRATION.md`, `docs/DEMO.md` |
| UI | `app/index.html` served by `python -m pqv.server <network>`; personal wallets `app/wallet/` (installable mobile app, phones over LAN via `infra/wallets.sh start --lan`); native phone app `mobile/` (Expo, over ngrok: `./demo.sh tunnel`, `./demo.sh app`) |

## 3. Repo map

```
contracts/src/PQVault.sol          2-of-3 vault: validate() [both VERIFY frames], execute() [SENDER frame], guardian mgmt
contracts/src/LeafLedger.sol       per-slot high-water mark (monotone nullifier) + reportEquivocation
contracts/src/PQVaultFactory.sol   CREATE2 factory; owns the ledger; useLedger=false = JARDIN baseline vault
contracts/src/PQVaultFrameOps.yul  EIP-8141 opcode helper (TXPARAM/SIGPARAM/SIGDATACOPY/FRAMEPARAM/FRAMEDATALOAD/APPROVE)
contracts/src/Verifiers.sol        imports upstream JardinForsPlainVerifier / JardinSpxVerifier
contracts/src/external/            NaiveStakingRegistry (copied, MIT) — only deployed on local devnets
contracts/test/                    PQVault.t.sol, mocks (MockFrameOps, CodeBlobFrameOps), fixtures + generator
pqv/frametx.py, rlp.py             EIP-8141 frame tx codec (chain-1337 / ethrex 9aa3645d layout)
pqv/jardin.py                      guardian keys over upstream JARDIN signers; entry encoding; vault_message (domain PQ-Vault/v2)
pqv/state.py                       DurableLeafStore (burn-before-sign), AnchoredLeafStore (chain-anchored)
pqv/devnet.py                      CLI: deploy-infra, new-guardians, deploy-vault, transfer, rotate, recover, attack-stale-restore
pqv/server.py, app/index.html      wallet website (127.0.0.1 only): /api/wallet, /api/plan, /api/send|rotate|recover
                                   run as background jobs polled at /api/jobs/<id>; keys never reach the browser
pqv/wallet.py, app/wallet/         personal MetaMask-style wallet, one process per person (infra/wallets.sh start a b c);
                                   state in .devnet/<net>/wallets/<name>/, shared contracts underneath (WalletNet);
                                   activity from EIP-7708 Transfer logs (0xfff…fffe); faucet/deploy via the deployer (file lock).
                                   The page is an installable mobile web app (manifest, sw.js, icons/ from infra/make_app_icons.py)
pqv/lan.py                         phone access (`wallets.sh start --lan`, `wallets.sh pair <name>`): 2nd listener on port+1000,
                                   HTTPS from a per-machine devnet CA (.devnet/tls/, openssl CLI, RSA), pairing code per wallet
                                   (wallets/<name>/pair-code, Bearer header on every LAN /api/ call); phones never hold keys
mobile/                            Leafguard phone app (React Native, Expo SDK 58, runs in Expo Go): remote for pqv/wallet.py
                                   over an ngrok tunnel; pairing code in expo-secure-store; App.tsx + src/{api,format,ui}.ts(x)
                                   + src/screens/. Keys never on the phone (JARDIN is Python-only; a JS port = custom crypto)
pqv/explorer.py, app/explorer/     block explorer (:8300) that decodes frame txs: frames (mode, scope, call, gas, logs),
                                   signature entries (guardian, compact/SPX, leaf), named addresses; background indexer
pqv/multisig.py                    shared 2-of-3 vaults between wallets: invite/join/deploy, propose/approve/submit; shared
                                   folder .devnet/<net>/vaults/<id>/ under flock; VERIFY limits sized for SPX so the tx is
                                   fixed before signing; a proposal expires when the vault nonce moves
pqv/receipts.py                    regenerates docs/RECEIPTS.md from the chain
pqv/_shim/Crypto/Hash/keccak.py    stdlib KECCAK-256 adapter so JARDIN's Python imports work without pycryptodome
research/                          policies.py, sim.py, run_experiments.py, sweeps.py, crash_test.py, mempool_admission.py, REPORT.md, results/
infra/                             local-devnet.sh (Kurtosis), build-ethrex-arm64.sh, sidecar-local.sh, kurtosis/*.yaml,
                                   sidecar-budget010/ (ERC-8421 per-entity budget patch + build.sh)
demo/build_demo.py                 rebuilds the video from recorded data (Chrome headless + `say` + ffmpeg)
third_party/JARDIN, SPHINCS-       git submodules, pinned (JARDIN has no LICENSE file → never copy it, keep as submodule)
```

## 4. How to run

Demo: `./demo.sh` (idempotent): network up or resumed (restarts the enclave's stopped el/cl/vc containers, same
chain), [BUDGET-010] sidecar, wallets with `--lan`, missing accounts created, explorer on :8300, URLs + pairing
codes printed. `./demo.sh status | stop [--chain] | up --fresh | up --no-lan | up --wallets "a b c"`.

```sh
git submodule update --init
(cd contracts && forge build && forge test)                    # 26 tests
python3.12 -m unittest discover -s tests -t .                  # 44 tests (Ubuntu: use the uv python3.13, see §6)
python3.12 research/run_experiments.py --seeds 20              # ~10 min
python3.12 research/sweeps.py --seeds 10
python3.12 research/crash_test.py --kills 100
infra/local-net.sh up [budget010|stock]                        # local ethrex + Lighthouse + skandha (~10 min)
python3.12 -m pqv.server local --port 8143                     # UI
python3.12 -m pqv.receipts local --out docs/RECEIPTS-local.md  # receipts of the current local run
```
Devnet flow (fresh machine): `infra/local-net.sh up` (network, funded deployer, `deploy-infra`, [BUDGET-010]
sidecar) → `new-guardians` → `deploy-vault --fund 2 --stake 0.02` → `transfer <to> <wei> --guardians 0,1 [--stateless 1]`
→ `rotate <g>` → `recover <g>` → `attack-stale-restore`. `infra/local-net.sh sidecar stock` swaps in the stock image
(it rejects the vault: combined 100k cap).
Local devnet bring-up: `research/REPORT.md` §10.

## 5. State that is NOT in git (by design)

`.devnet/` holds secrets and per-machine state: `deployer.json` (throwaway ECDSA deployer key),
`<network>/guardians.json` (guardian master secrets + slot randomness), `guardian-*-leaves.json` (signer
leaf counters — **never hand-edit; reusing a leaf is the bug this project studies**), `deployment.json`,
`receipts.json`, `network.json`, relayer key. A new machine starts with `new-guardians` and redeploys; public
addresses and receipts of the existing deployment are recorded in `docs/RECEIPTS.md`.

Public devnet deployment (2026-11-06): vault `0x9127b9f1297eda2f04916c82d4b41fa53bb179a5`, factory
`0x7ad28d46e98df621776558fc7bfa3269da875ac6`, ledger `0x15c9a7285c8c45881616a7986f1303199e1bde23`,
FrameOps `0xa359c5aab9277c8234733a9a0d4f6daa22d03dac`, staking registry `0xd51c04d7a0b01124e84de152f4e56d9eecb22d2f`.
Without the guardian secrets in `.devnet/public/` that vault cannot be operated; deploy a new one.

## 6. Traps that cost hours (read before debugging)

- **Python**: needs OpenSSL ≥ 3.2 for native `KECCAK-256`: Homebrew `python3.12` on macOS; on the Ubuntu box the system
  `python3.12` has OpenSSL 3.0 (no keccak) → use the uv-managed `~/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/bin/python3`
  (OpenSSL 3.5). `'KECCAK-256' in hashlib.algorithms_available` is False there but `hashlib.new("KECCAK-256")` works.
- **Frame-tx sig hash** elides the bytes of every *empty-msg* signature entry; frame data is covered. PQ signatures
  go in `ARBITRARY` entries. Since the three-frame redesign every entry is verified in a VERIFY frame, so all guardians
  sign the same `messageFor(sigHash)` (domain `PQ-Vault/v2`) in any order; a relayer that corrupts either entry makes
  the tx invalid (no fee). The original design's `firstMessageFor` chaining (REPORT G6) is gone.
- **Slot refresh** (`pqv/devnet.py`): before signing, `vault_call` rotates any compact approver with
  `remaining_leaves() <= min(--rotate-at (8), q_max/4)` (counts the local counter *and* the LeafLedger mark; a revoked
  slot is 0), approved by the other two; an approver with 0 left signs stateless (`effective_kinds`). Rotations run
  with refresh off (no recursion) and keep the slot height. Rotation is per vault (`vault_label`): other vaults that
  share the guardian set keep the old slot until rotated there too. `research/slot_refresh_e2e.py` exercises it with
  4-leaf slots (`new-guardians --slot-h 2`).
- **JARDIN is not thread-safe**: upstream's hash hot path uses shared module buffers (`jardin_primitives._BUF96/_BUF128`).
  `pqv/jardin.py` serialises every call (`_serialized`); never call the upstream signers directly from threaded code
  (both web servers are multi-threaded). Symptom when violated: a signature entry with a wrong slot root → `BadEntry(i, 5)`.
- **Debugging a silently dropped privileged tx**: ethrex logs the payload builder's skip reason only at debug level;
  enable at runtime with `cast rpc --rpc-url <ethrex> admin_setLogLevel '"info,ethrex_blockchain=debug"'` and grep
  `skipping IL tx`. A local reading copy of ethrex @ 9aa3645d can live in `vendor/` (gitignored).
- **Transaction shape** (PQVault.validate pins it): sigs = 2 ARBITRARY entries, entry 0's guardian < entry 1's;
  frame 0 VERIFY flags=2 (only_verify, target null) data `validate()`; frame 1 VERIFY flags=1 (pay, target vault)
  data `validate()`; frame 2 SENDER target vault, flags 0, `execute(calls)`. Frame i verifies entry i. ERC-8421
  PREFIX-010 allows at most these two VERIFY frames.
- **`ethrex_simulateFrameTransaction`**: `valid`/`canonicalMempoolViolation` is the *canonical* mempool verdict. Leafguard
  is expected to show `StorageReadNonSender` there; gate on `executionStatus == success`, not `valid`.
- **Validation budget**: a VERIFY frame costs ~84k on chain with a compact approval, ~292k with a stateless (SPX) one
  (Foundry: 87,756 / 82,110 / 296,854). The ~170k prefix exceeds EIP-8141's combined MAX_VERIFY_GAS (100k); two
  upstream JARDIN verifications alone are ~109k, so the fix is mempool-side.
  The deployed workshop sidecar enforces that combined 100k even for staked senders; ERC-8421 [BUDGET-010] (per-entity,
  1M staked) is implemented in `infra/sidecar-budget010/` (build.sh → `pqvault/mempool-sidecar:budget-010`; run with
  `SIDECAR_IMAGE=pqvault/mempool-sidecar:budget-010 infra/sidecar-local.sh …`). ethrex's own simulate RPC only *notes*
  the 100k overage in `canonicalMempoolViolation` and still traces, so ethrex needs no reconfiguration.
- **Calldata floor**: ethrex bills frame txs at max(standard, 13,150 + 64 gas × bytes of frame data + signatures)
  (EIP-7623/7976 floor, `transaction.rs:calldata_floor_gas`). Leafguard txs are floor-bound: execution-gas savings do
  not lower the bill; fewer bytes do.
- **Fresh contract accounts start at nonce 1.** The driver reads the nonce from the chain.
- **Public devnet gas**: contract creation costs far more gas than locally (factory ~18M) — the driver estimates gas; don't hardcode.
- **ethrex privileged relay** (local sidecar) gives each tx ONE payload build; the sidecar must re-relay
  (`maxSubmitAttempts: 40` in `infra/sidecar-local.sh`) or txs end `Cancelled` despite simulating fine.
- **Apple Silicon**: `ghcr.io/forshtat/ethrex:eip8141-tracer-latest` is amd64-only and segfaults (exit 139) under
  emulation → `infra/build-ethrex-arm64.sh`. Docker Desktop has only 3 GB RAM here; stop Kurtosis telemetry containers when building.
- **Foundry tests**: no forge-std (local `TestBase`/`Vm`); fixture JSON is read on demand (storing it costs ~80M gas);
  `vm.expectRevert` binds to the *next external call* (including view calls) — compute args first.
- **FrameOps** is deployed from the *unoptimized* solc build (`pqv/devnet.py:frameops_initcode`) so bytecode maps 1:1
  to the reviewed opcode/stack order; forge's optimized artifact is not used for deployment.
- **zsh**: never name a loop variable `path` (it clobbers `$PATH`); `c` is aliased in the user's shell, so don't name a
  helper function `c` in commands run through it.
- **Docker restart** stops every devnet container (exit 255): `./demo.sh up` resumes them; `infra/local-net.sh up`
  would build a new chain and archive the wallets' state.
- **`set -e` in demo.sh**: a function whose last statement is a false `[ … ] && cmd` returns 1 and silently ends the script.
- **Phones / PWA**: a phone installs the app only from a secure origin, so the LAN listener is HTTPS and the phone must
  trust `.devnet/tls/ca.crt` (localhost via `adb reverse` counts as secure without it). iOS home-screen apps do not share
  Safari's storage: pair from inside the installed app (type the code). The LAN API locks for 60 s after 20 wrong codes.
- **Tunnels (ngrok) reach the loopback listener**: requests arrive from 127.0.0.1, so `H._remote` treats any request
  with forwarding headers (X-Forwarded-For/-Host, Forwarded, X-Real-IP) or a Host other than localhost/127.0.0.1/[::1]
  as a phone and requires the pairing code (one shared `GATE`). `adb reverse` keeps Host localhost, so it stays local.
  The wallet finds its tunnel via ngrok's agent API (127.0.0.1:4040-4042) for `/api/pair`. Never add CORS headers to
  the wallet server: the missing CORS is what keeps other web pages away from a local wallet.
- **Payment stuck in "Sending" / "waiting for a block"** (sidecar status `Submitted`, ethrex builds the payload with
  `txs=1` but the block has 0 txs): ethrex builds each slot's payload twice (slot start, then ~1.75 s later on a second
  forkchoiceUpdated) and serves the *first* build; a privileged relay is only picked up by the next build. With the
  old 3 s bundleInterval the sidecar relayed every 6 s at a fixed phase, and a restart could lock that phase just after
  the first build, so every relay missed. `bundleInterval: 1000` (relays at three phases per slot) fixes it
  (`infra/sidecar-local.sh`, `.devnet/local/sidecar-config.json`). The sidecar keeps its pool across restarts, so a
  resubmit says "already known"; it is still relayed. Diagnose with
  `cast rpc admin_setLogLevel '"info,ethrex_blockchain=debug"'` and the `EIP-8037 block finalize ... txs=` lines.
- **Metro port**: 8081 belongs to the Kurtosis engine container, so the app's dev server runs on 8085 (`METRO_PORT`).
  `expo start --tunnel` needs an Expo login (it failed with a NetworkError to api.expo.dev); `./demo.sh app --ngrok`
  instead sets `EXPO_PACKAGER_PROXY_URL` to the user's ngrok http URL (Expo Go link `exp://<host>:80`) with `--offline`.
- **Mobile app**: Expo Go runs only the newest SDK, so keep `mobile/` on it (`npx expo install --fix`); use
  `npx expo install` for packages. Free ngrok: one agent per account (`./demo.sh tunnel a b` runs several tunnels in
  one agent), URL changes on restart unless `--url <static domain>`. Starting ngrok was blocked by the auto-mode
  classifier ("External Ingress Tunnel"): the user runs `./demo.sh tunnel`. Screens were checked with the web build in
  headless Chrome (`--disable-web-security` to stand in for React Native, which has no CORS).
- **Headless Chrome on this Ubuntu box**: AppArmor blocks Chrome's sandbox; the puppeteer Chrome in `~/.cache/puppeteer`
  runs with `--no-sandbox` (local pages only). Node 24's built-in WebSocket is enough to drive it over CDP.
- Tool permissions: installing/running external npm code (e.g. `yarn install` in vendored repos, `npx hyperframes`)
  was blocked by the auto-mode classifier; ask the user before retrying.

## 7. Open work (priority order)

1. **Withhold-and-rollback attack** (REPORT §9): a malicious host withholds the tx for leaf q, rolls storage back,
   gets leaf q signed again — current `anchored_w1` re-signs unboundedly and nothing is published. Add it to
   `research/sim.py` (host withholds + rollback), measure γ, then implement and measure the candidate fix:
   per-boot instance IDs from hardware RNG + on-chain leaf *leases* above all previous leases (authorised by the
   stateless key) + on-device light-client verification of the lease.
2. **Per-vault nullifier variant** (REPORT §7.0): ledger in the vault's own storage — likely passes the canonical
   mempool; loses cross-vault protection. Build + measure admission on the public devnet.
3. Get `[BUDGET-010]` into the deployed alt mempool: the patch + draft PR text are in `infra/sidecar-budget010/README.md`
   (not submitted — the user decides). Until then the stock image rejects the vault; use the [BUDGET-010] image.
4. (dropped) Public chain-1337 gateway: unreproducible rules and no inclusion observed (REPORT §8.1); use `infra/local-net.sh`.
5. Light-client verification of the LeafLedger mark (removes trust in the host/RPC).
6. Hardware secure-element testing; real power-loss testing (SIGKILL ≠ power loss).
7. Hackathon submission (repo link, video, CROPS section in README). The demo video still shows the old 2-of-3
   two-frame design; rebuild it (`demo/build_demo.py`, needs macOS `say`) from the local-net receipts.
8. Optional HyperFrames version of the demo video (needs the user to allow `npx hyperframes`).

## 8. Conventions

- Regenerate, don't hand-edit: `docs/RECEIPTS.md` (`pqv.receipts`), `research/results/*` (scripts), video (`demo/build_demo.py`).
- Every new claim in the report needs a script that produces it; record failed experiments in REPORT §9.
- Keep upstream code unmodified; attribute anything copied (MIT only).
- Commits: author Sagar Rana only, no AI co-author or session trailers; push only when the user asks.
