# Leafguard — 3-minute demo script

Rendered video: [`demo/pq-vault-demo.mp4`](../demo/pq-vault-demo.mp4) (2:56, 1920×1080), built by
`demo/build_demo.py` from the recorded devnet receipts and research results (slides rendered with headless
Chrome, narration with macOS `say`, assembled with ffmpeg). The table below is the live-presentation version.

Setup before recording: devnet running (public chain-1337, or `infra/local-devnet.sh up` +
`infra/sidecar-local.sh`), vault deployed, `python3.12 -m pqv.server <network>` on http://127.0.0.1:8141.
Every number shown on screen comes from the running system or from `research/results/`.

| Time | Screen | Say |
|---|---|---|
| 0:00–0:20 | Title / README header | "A quantum computer breaks every ECDSA key on Ethereum. EIP-8141 frame transactions let an account verify *any* signature in its own code. Leafguard is a 2-of-3 guardian wallet where every approval is a hash-based JARDIN signature — no ECDSA anywhere." |
| 0:20–0:45 | UI: Vault + Guardians panels | "Three guardians, each with a stateless SPHINCS-style master key and a stateful compact slot: 128 few-time FORS keys under one Merkle root. The bar is how many leaves each guardian has burned; next to it, the high-water mark the chain has recorded." |
| 0:45–1:15 | UI: Send 0.001 ETH with guardians 0+1 → receipt row | "Guardian 1 signs, then guardian 0 signs over guardian 1's exact bytes. One frame transaction: the VERIFY frame checks guardian 0's 2.7 KB JARDIN signature in about 85k gas — under the 100k validation cap — and the body frame checks guardian 1. It goes through the ERC-8421 alternative mempool: the vault reads a shared on-chain leaf ledger during validation, which the public mempool refuses and the alt mempool admits because the vault is staked." Show tx hash, block, both frames `0x1`. |
| 1:15–1:35 | UI: Send with "stateless" second approval | "The second approval can also be the stateless path — 6.5 KB, no state at all — for a guardian whose device isn't sure of its own state." |
| 1:35–2:00 | UI: "Lost device → recover" on guardian 2, then "Rotate slot" on guardian 0 | "Guardian 2 lost their phone: guardians 0 and 1 install new keys, and every old slot of guardian 2 is revoked by an epoch bump. Rotation gives a guardian a fresh slot the same way." |
| 2:00–2:35 | UI: "Restore stale backup" on guardian 0 → JSON; then Research table | "Now the research question: what if a device is restored from an old backup? A JARDIN-style signer picks a leaf it already used — security on that leaf drops from 128 to ~97 bits, and with repeated rollbacks to 78 or lower. The vault's VERIFY frame rejects it on chain: StaleLeaf. Leafguard's own signer starts above the chain's mark, so it never reuses a leaf in the first place. Across 20 seeds: stale backups, lost state and counter rollback go from hundreds of accepted reused signatures to zero. Clones can't be prevented this way — two live copies can still sign one fresh leaf — so anyone can submit both signatures as an equivocation proof, which revokes the slot everywhere: exposure drops from 20 blocks to 1." |
| 2:35–3:00 | REPORT.md §9 | "What it doesn't do: it trusts the host for the chain view, in-flight transactions are a residual window, and the deployed alt mempool still caps validation at 100k, so the second signature is checked after approval. Code, tests, receipts and the full report are open source." |

Command-line fallback (same flow, prints receipts):

```sh
python3.12 -m pqv.devnet <network> transfer 0x<to> 1000000000000000 --guardians 0,1
python3.12 -m pqv.devnet <network> transfer 0x<to> 1000000000000000 --guardians 1,2 --second stateless
python3.12 -m pqv.devnet <network> recover 2
python3.12 -m pqv.devnet <network> rotate 0
python3.12 -m pqv.receipts <network>
```
