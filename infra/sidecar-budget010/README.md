# ERC-8421 `[BUDGET-010]` for the workshop alt-mempool sidecar

Leafguard (2-of-3) checks both guardians' post-quantum signatures inside the validation prefix, one per VERIFY
frame: about 84k gas each on chain with a compact approval (~292k with a stateless one; `docs/RECEIPTS-local.md`),
~170k in total. The workshop sidecar (`forshtat/mempool-workshop-sidecar` @ `2e22ff0a`, image digest
`sha256:c4cdc5c7…`) caps the **sum over the whole prefix** at EIP-8141's `MAX_VERIFY_GAS = 100,000`, so it rejects
the vault. Its own design
notes say it left out ERC-8421's per-entity budgets on purpose (`docs/superpowers/plans/2026-09-28-prevalidate-frame-shapes.md`:
"Do not implement `BUDGET-010`'s per-entity staked gas budget bump").

ERC-8421 (ethereum/ERCs PR #2028, `ERCS/erc-8421.md` @ `493ba8ac`) specifies:

> **[BUDGET-010]** A node MUST track the sum of `limits.execution` **separately per entity**, over that
> entity's own validation frames. The `pre_verify` frame counts toward the entity of the approving frame it
> immediately precedes. The intrinsic cost of validating `tx.signatures` counts toward the `tx.sender`.
> For **unstaked entities**, that sum MUST NOT exceed `MAX_VERIFY_GAS_PER_ENTITY` (100,000).
> For **staked entities**, that sum MUST NOT exceed `MAX_VERIFY_GAS_STAKED_ENTITY` (1,000,000).

`budget-010-src.patch` implements exactly that (two files):

- `utils/validationPrefix.ts`: `validateStructuralRules(tx, prefix, isStakedEntity)` attributes every prefix
  frame to its entity (the sender for `sender`; the frame's resolved target for `payer`, which includes a
  `self_verify` frame, and for `factory`; a `pre_verify` frame shares the approving frame's role and target)
  and adds the signature-verification cost to `tx.sender`. Each entity is capped at 1M if staked, else 100k.
  A leading `expiry_verify` frame belongs to no entity and is still capped at 100k. `MAX_VERIFY_STATE_GAS`
  is unchanged. Without the new argument everything counts as unstaked, which equals the old behaviour for
  every single-entity transaction.
- `validators/frameTx.ts`: resolves stake for the prefix roles, through the same `StakingRegistryService` +
  `ReputationService.checkStake` path the storage rules already use, before the budget check.

`budget-010-full.patch` adds 7 unit tests (`validationPrefix.test.ts`).

## Verification (all run inside the published image, `--network none`, its own `node_modules`)

| suite | unpatched | patched |
|---|---|---|
| `packages/executor` vitest, `test/` | 386 passed; 5 legacy `test/unit/*` files fail to load (missing generated EPv7 types, not in the image) | 393 passed (386 + 7 new); same 5 load failures |
| `tsc -p tsconfig.build.json` (executor) | — | compiles (`build.sh`) |

## Build and run

```sh
infra/sidecar-budget010/build.sh                    # -> pqvault/mempool-sidecar:budget-010
SIDECAR_IMAGE=pqvault/mempool-sidecar:budget-010 infra/sidecar-local.sh <ethrex-port> <staking-registry>
```

## Draft upstream PR (not submitted; for the maintainers of forshtat/mempool-workshop-sidecar)

**Title:** Implement ERC-8421 [BUDGET-010] per-entity validation gas budgets

**Body:**
The validation-prefix budget check currently applies EIP-8141's single combined `MAX_VERIFY_GAS` (100k)
to the whole prefix. ERC-8421 [BUDGET-010] replaces it, for alternative mempools, with a per-entity sum:
100k (`MAX_VERIFY_GAS_PER_ENTITY`) for an unstaked entity and 1M (`MAX_VERIFY_GAS_STAKED_ENTITY`) for a
staked one, so that staked accounts can run heavier validation (for example post-quantum hash-based
signatures, several approvals in a multisig, Merkle proofs) without raising the budget of any unstaked
entity in the same transaction.

This PR:
- adds `MAX_VERIFY_GAS_PER_ENTITY` / `MAX_VERIFY_GAS_STAKED_ENTITY`,
- attributes each prefix frame to its entity (`pre_verify` → the approving frame's entity; signature cost →
  `tx.sender`; a leading `expiry_verify` is unattributed and still capped at 100k),
- resolves stake for the prefix roles before the check (same `StakingRegistryService` / `checkStake` path
  as the storage rules),
- keeps `MAX_VERIFY_STATE_GAS` as is,
- adds 7 unit tests; the existing 28 prefix tests pass unchanged.

Motivating case: a staked 2-of-3 guardian account that verifies two JARDIN (SPHINCS-family) signatures in
`[only_verify, pay]` frames needs ~170k of validation gas (200,200 declared) and is rejected today with
`validation prefix execution gas sum … exceeds MAX_VERIFY_GAS 100000`.
