# Leafguard demo narration

**0:00 — title**  
Leafguard. A two-of-three post-quantum guardian wallet for Ethereum, built on EIP-8141 frame transactions and the ERC-8421 alternative mempool. Every approval is a hash-based JARDIN signature. There is no ECDSA anywhere in the wallet.

**0:15 — problem**  
A large quantum computer breaks every elliptic-curve key. EIP-8141 lets an account verify any signature in its own validation frame, so hash-based signatures work today. The cheap ones, though, are stateful: a few-time key that signs two different messages with the same leaf leaks its secrets. That is what this project studies.

**0:34 — architecture**  
Each transaction has two frames. The verify frame checks guardian A's JARDIN signature against the protocol's signature hash, rejects any leaf the chain has already seen, caps the fee, and binds the approval to the exact body frame. On chain it costs about eighty-two thousand gas, under the hundred-thousand validation cap. The sender frame then checks guardian B, who may use the stateless path, records both leaves, and runs the batch.

**0:58 — ui**  
Here is the wallet interface on the public devnet: three guardians, how many leaves each device has burned, and the high-water mark the chain has recorded for each one, with every receipt below.

**1:08 — receipts**  
These are real receipts from the public hackathon devnet: two-of-three transfers, a stateless second approval, key rotation, recovery of a lost guardian by the other two, and a transfer signed by the recovered guardian.

**1:20 — altmempool**  
Why the alternative mempool? The vault reads a shared leaf ledger during validation. The canonical mempool rejects that storage access. The alt mempool rejects an unstaked copy of the vault, and admits the staked one.

**1:31 — curve**  
Now the research question. These few-time leaves lose security fast when reused: one hundred twenty-eight bits once, ninety-seven after one reuse, under sixty after five. So what happens when a device crashes, is restored from an old backup, is cloned, or has its counter rolled back?

**1:48 — attacks**  
We attacked controlled test wallets with real JARDIN signatures. Restoring the same backup eight times drives reuse to 9 and the forgery cost to about 36 bits. Killing a persist-after-sign signer at the moment of release put 100 signatures on a single leaf. Burn-before-sign stops crashes, but not stale backups, rollbacks or clones.

**2:07 — mitigation**  
The mitigation has four layers: the on-chain leaf ledger, a signer anchored to the chain's mark, a one-transaction in-flight window, and permissionless equivocation proofs that revoke a slot everywhere. Stale backups and rollbacks go from hundreds of double-accepted signatures to zero. Clones can't be prevented, but exposure drops from 20 blocks to one.

**2:25 — live-attack**  
And live on the public devnet: a vault without leaf tracking accepted two transactions signed with the same leaf after a stale restore. A watcher's equivocation proof revoked that slot, and Leafguard rejected the same restored signer with StaleLeaf.

**2:38 — limits**  
Limits: the signer trusts its host for the chain view, in-flight transactions are a residual window, and the deployed alt mempool still caps validation at one hundred thousand gas, so the second signature is checked after approval. Code, tests, receipts and the full report are open source.


Total: 2:54