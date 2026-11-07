"""Leafguard devnet driver: deploy infrastructure, create guardians and a vault, send 2-of-3 frame txs.

    python -m pqv.devnet <network> status
    python -m pqv.devnet <network> deploy-infra
    python -m pqv.devnet <network> new-guardians
    python -m pqv.devnet <network> deploy-vault [--fund ETH] [--stake ETH] [--vault label]
    python -m pqv.devnet <network> transfer <to> <wei> [--guardians 0,1] [--stateless 1] [--rotate-at 8]
    python -m pqv.devnet <network> rotate <g> [--guardians a,b] [--stateless a]

Slot refresh: a compact slot has 2^h leaves (128). Before signing, a guardian whose slot is nearly used up (or
revoked) is rotated to a fresh slot first, approved by the other two (`--rotate-at`, 0 = off); an approver whose
slot is completely used up signs with its stateless SPX key instead, so neither transfers nor rotations get stuck.

Every vault transaction has three frames (the ERC-8421 `[only_verify, pay]` prefix + one body frame):
    frame 0 VERIFY (scope EXECUTION) validate(): guardian A's approval (entry 0)
    frame 1 VERIFY (scope PAYMENT)   validate(): guardian B's approval (entry 1, higher guardian index)
    frame 2 SENDER execute(calls): consume both leaves, run the batch
The prefix (~170k gas) exceeds EIP-8141's combined 100k MAX_VERIFY_GAS; an ERC-8421 mempool with [BUDGET-010]
(1M for a staked entity; infra/local-net.sh runs one) admits it.

<network> is `local` (infra/local-net.sh: Kurtosis ethrex + Lighthouse + skandha) or `public` (historical).
ECDSA appears in exactly one place: the throwaway deployer EOA (.devnet/deployer.json) that publishes
contracts with ordinary transactions. The vault itself has no ECDSA code path.
State and keys live in .devnet/ (gitignored). Devnet only - never point this at a real network.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

from . import frametx as ft
from . import rpc
from .hashing import ROOT, keccak256
from .jardin import CompactSlot, GuardianKeys, compact_entry, stateless_entry, vault_message
from .state import AnchoredLeafStore, DurableLeafStore

DEVNET_DIR = os.path.join(ROOT, ".devnet")
OUT = os.path.join(ROOT, "contracts", "out")
SOLC_CANDIDATES = [os.environ.get("PQV_SOLC", ""), os.path.expanduser("~/Library/Application Support/svm/0.8.28/solc-0.8.28"),
                   os.path.expanduser("~/.svm/0.8.28/solc-0.8.28"),
                   os.path.expanduser("~/.local/share/svm/0.8.28/solc-0.8.28"), "solc"]
PUBLIC = {"ethrex": "https://altmempool.forshtat.com", "sidecar": "https://altmempoolgateway.forshtat.com/rpc",
          "staking": "0xd51c04d7a0b01124e84de152f4e56d9eecb22d2f", "explorer": "https://explorer.altmempool.wtf"}
# Declared VERIFY-frame execution limits. The sum over both frames + 100 per ARBITRARY entry is what an
# ERC-8421 node counts against the vault's validation budget ([BUDGET-010]). Measured (Foundry, gas-faithful
# FrameOps stand-in): 87,756 (frame 0) / 82,110 (frame 1) with a compact approval, 296,854 with a stateless (SPX)
# one. Limits below carry ~10-15% headroom.
VERIFY_BASE_GAS = 30_000
VERIFY_COMPACT_GAS = 70_000
VERIFY_STATELESS_GAS = 300_000
MAX_TX_COST_WEI = 5 * 10**16
SLOT_H = 7  # compact slot height: 2^7 = 128 leaves
# Automatic slot refresh: before a guardian approves with its compact key, rotate it when this many leaves (or
# fewer) are left, capped at a quarter of the slot so tiny test slots still get used. 0 disables it.
ROTATE_AT = 8


# ----------------------------------------------------------------------------- small utils
def sel(sig: str) -> bytes:
    return keccak256(sig.encode())[:4]


def h(b: bytes) -> str:
    return "0x" + b.hex()


def unhex(s: str) -> bytes:
    return bytes.fromhex(s[2:] if s.startswith("0x") else s)


def cast(*args) -> str:
    r = subprocess.run(["cast", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cast {args[0]} failed: {re.sub(r'0x[0-9a-fA-F]{64}', '<hex64>', r.stderr.strip())[:1500]}")
    return r.stdout.strip()


class Net:
    def __init__(self, name: str):
        self.name = name
        self.dir = os.path.join(DEVNET_DIR, name)
        os.makedirs(self.dir, exist_ok=True)
        cfg_path = os.path.join(self.dir, "network.json")
        if os.path.exists(cfg_path):
            self.cfg = json.load(open(cfg_path))
        elif name == "public":
            self.cfg = dict(PUBLIC)
        else:
            raise SystemExit(f"write {cfg_path} with ethrex/sidecar URLs first (see infra/local-devnet.sh)")
        rpc.ETHREX_URL, rpc.SIDECAR_URL = self.cfg["ethrex"], self.cfg["sidecar"]

    # files
    def path(self, f):
        return os.path.join(self.dir, f)

    def load(self, f, default=None):
        p = self.path(f)
        return json.load(open(p)) if os.path.exists(p) else default

    def save(self, f, obj):
        p = self.path(f)
        with open(p, "w") as fh:
            json.dump(obj, fh, indent=1)
        os.chmod(p, 0o600)

    @property
    def deployment(self):
        return self.load("deployment.json", {})

    # chain helpers
    def deployer_key(self) -> str:
        d = json.load(open(os.path.join(DEVNET_DIR, "deployer.json")))
        d = d[0] if isinstance(d, list) else d
        return d["private_key"]

    def deployer(self) -> str:
        d = json.load(open(os.path.join(DEVNET_DIR, "deployer.json")))
        return (d[0] if isinstance(d, list) else d)["address"]

    def gas_price(self) -> int:
        base = int(rpc.eth("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
        return max(2 * base, 10**9)

    def send_legacy(self, to, data_hex, value_wei=0, gas=None) -> dict:
        args = ["send", "--private-key", self.deployer_key(), "--rpc-url", self.cfg["ethrex"], "--legacy",
                "--gas-price", str(self.gas_price()), "--json", "--value", str(value_wei)]
        if gas:
            args += ["--gas-limit", str(gas)]
        args += ["--create", data_hex] if to is None else [to, data_hex]
        r = json.loads(cast(*args))
        if r.get("status") not in ("0x1", 1, "1"):
            raise RuntimeError(f"tx {r.get('transactionHash')} reverted")
        return r

    def call(self, to: str, data: bytes) -> bytes:
        return unhex(rpc.eth("eth_call", [{"to": to, "data": h(data)}, "latest"]))

    def ledger_hwm(self, slot_id: bytes) -> int:
        led = self.deployment.get("ledger")
        if not led:
            return 0
        return int.from_bytes(self.call(led, sel("hwm(bytes32)") + slot_id), "big")


def artifact(name: str, sol: str = None) -> str:
    p = os.path.join(OUT, f"{sol or name}.sol", f"{name}.json")
    return json.load(open(p))["bytecode"]["object"]


def _solc() -> str:
    import shutil
    for c in SOLC_CANDIDATES:
        if c and (os.path.isfile(c) or shutil.which(c)):
            return c
    raise SystemExit("solc 0.8.28 not found (set PQV_SOLC)")


def frameops_initcode() -> str:
    """Compiled without the optimizer, like @forshtat/hegota-support's helpers: the bytecode maps 1:1
    onto the verbatim opcode sequence that was reviewed against ethrex's stack order."""
    src = os.path.join(ROOT, "contracts", "src", "PQVaultFrameOps.yul")
    out = subprocess.run([_solc(), "--strict-assembly", "--evm-version", "cancun", "--bin", src],
                         capture_output=True, text=True, check=True).stdout
    return "0x" + out.split("Binary representation:")[1].split()[0]


# ----------------------------------------------------------------------------- commands
def deploy_infra(net: Net):
    dep = net.deployment
    def create(label, initcode, ctor_sig=None, *ctor_args):
        if dep.get(label):
            print(f"  {label}: {dep[label]} (exists)")
            return dep[label]
        code = initcode
        if ctor_sig:
            code += cast("abi-encode", ctor_sig, *ctor_args)[2:]
        r = net.send_legacy(None, code)
        dep[label] = r["contractAddress"]
        dep.setdefault("txs", {})[label] = r["transactionHash"]
        net.save("deployment.json", dep)
        print(f"  {label}: {dep[label]}  tx {r['transactionHash']}  gas {int(r['gasUsed'], 16)}")
        return dep[label]

    staking = net.cfg.get("staking") or create("staking", artifact("NaiveStakingRegistry"))
    dep["staking"] = staking
    fo = create("frameOps", frameops_initcode())
    fors = create("forsVerifier", artifact("JardinForsPlainVerifier"))
    spx = create("spxVerifier", artifact("JardinSpxVerifier"))
    fac = create("factory", artifact("PQVaultFactory"), "constructor(address,address,address,address)", fo, fors, spx, staking)
    dep["ledger"] = "0x" + net.call(fac, sel("ledger()"))[-20:].hex()
    net.save("deployment.json", dep)
    print(f"  ledger: {dep['ledger']}")


def new_guardians(net: Net, slot_h: int = SLOT_H, file: str = "guardians.json"):
    if net.load(file):
        raise SystemExit(f"{file} exists; refusing to overwrite keys")
    if not 2 <= slot_h <= 7:
        raise SystemExit("slot height must be in [2, 7] (2^h leaves; the leaf index is one byte)")
    gs = [{"index": i, "master_sk": h(os.urandom(32)), "slots": [{"r": h(os.urandom(32)), "h": slot_h}]}
          for i in range(3)]
    net.save(file, gs)
    print(f"created 3 guardians with {1 << slot_h}-leaf slots (secret keys in {os.path.relpath(net.path(file), ROOT)})")


class Guardian:
    def __init__(self, net: Net, rec: dict, prefix: str = "guardian", anchored: bool = True,
                 file: str = "guardians.json"):
        self.net, self.rec, self.i, self.file, self.anchored = net, rec, rec["index"], file, anchored
        self.keys = GuardianKeys(self.i, unhex(rec["master_sk"]))
        self._spx = None
        self._slots = {}
        path = net.path(f"{prefix}-{self.i}-leaves.json")
        # Leafguard guardians use the anchored store; the baseline (attack demo) uses JARDIN-style burn-before-sign only.
        self.leaves = AnchoredLeafStore(path, net.ledger_hwm) if anchored else DurableLeafStore(path)

    @property
    def spx(self):
        if self._spx is None:
            self._spx = self.keys.spx()
        return self._spx

    def slot(self, k=-1) -> CompactSlot:
        s = self.rec["slots"][k]
        if s["r"] not in self._slots:
            self._slots[s["r"]] = CompactSlot(self.keys.master_sk, unhex(s["r"]), s["h"])
        return self._slots[s["r"]]

    def remaining_leaves(self) -> int:
        """Leaves this guardian can still sign with on its current slot: above both its local counter and the
        on-chain LeafLedger mark. A revoked slot (mark = max) has 0."""
        slot = self.slot()
        nxt = self.leaves.peek(slot.slot_id)
        if self.anchored:
            nxt = max(nxt, self.net.ledger_hwm(slot.slot_id) + 1)
        return max(0, slot.q_max - nxt + 1)

    def sign_compact(self, msg: bytes) -> bytes:
        slot = self.slot()
        q = self.leaves.reserve(slot.slot_id, slot.q_max)  # burned (fsync) before the signature exists
        return compact_entry(self.i, slot, slot.sign(msg, q))

    def sign_stateless(self, msg: bytes) -> bytes:
        return stateless_entry(self.i, self.spx.sign(msg))


def guardians(net: Net, file: str = "guardians.json", prefix: str = "guardian", anchored: bool = True):
    gs = net.load(file)
    if not gs:
        raise SystemExit(f"run new-guardians first ({file})")
    return [Guardian(net, g, prefix, anchored, file) for g in gs]


def save_guardians(gs):
    gs[0].net.save(gs[0].file, [g.rec for g in gs])


def rotation_due(remaining: int, q_max: int, rotate_at: int = ROTATE_AT) -> bool:
    """Rotate before signing when `remaining` <= min(rotate_at, q_max // 4) (at least 1). rotate_at <= 0: never."""
    if rotate_at <= 0:
        return False
    return remaining <= min(rotate_at, max(1, q_max // 4))


def effective_kinds(kinds, remaining):
    """An approver asked to sign compact whose slot has no leaf left signs stateless (SPX) instead."""
    return ["stateless" if k == "compact" and r <= 0 else k for k, r in zip(kinds, remaining)]


def deploy_vault(net: Net, fund_eth: float, stake_eth: float, use_ledger: bool = True, label: str = "vault",
                 gs=None):
    dep = net.deployment
    if dep.get(label):
        print(f"{label} exists: {dep[label]}")
        return
    gs = gs or guardians(net)
    g_tuple = ",".join(f"({h(g.spx.public[0])},{h(g.spx.public[1])})" for g in gs)
    seeds = ",".join(h(g.slot().seed16) for g in gs)
    roots = ",".join(h(g.slot().root16) for g in gs)
    init = f"([{g_tuple}],[{seeds}],[{roots}],{MAX_TX_COST_WEI},0)"
    salt = h(os.urandom(32))
    stake = int(stake_eth * 10**18)
    data = cast("calldata", "deploy(bytes32,((bytes32,bytes32)[3],bytes16[3],bytes16[3],uint256,uint32),bool,uint256)",
                salt, init, "true" if use_ledger else "false", str(stake))
    value = int(fund_eth * 10**18) + stake
    est = int(rpc.eth("eth_estimateGas", [{"from": net.deployer(), "to": dep["factory"], "data": data,
                                           "value": hex(value)}]), 16)
    r = net.send_legacy(dep["factory"], data, value, gas=int(est * 1.3))
    topic = "0x" + keccak256(b"VaultDeployed(address,bytes32,bool)").hex()
    addr = next("0x" + l["topics"][1][-40:] for l in r["logs"] if l["topics"][0] == topic)
    dep[label] = addr
    dep.setdefault("txs", {})[label] = r["transactionHash"]
    net.save("deployment.json", dep)
    print(f"{label}: {addr}  tx {r['transactionHash']}  2-of-3 ledger={use_ledger} stake={stake_eth} ETH")


def execute_calldata(calls) -> bytes:
    arr = "[" + ",".join(f"({to},{val},{data})" for to, val, data in calls) + "]"
    return unhex(cast("calldata", "execute((address,uint256,bytes)[])", arr))


def approval_gas(kind: str) -> int:
    """Declared execution limit of a VERIFY frame checking one approval of this kind."""
    return VERIFY_BASE_GAS + (VERIFY_STATELESS_GAS if kind == "stateless" else VERIFY_COMPACT_GAS)


def build_vault_tx(net: Net, vault: str, calls, kinds, exec_gas=900_000, state_gas=400_000) -> ft.FrameTx:
    """Unsigned three-frame vault transaction; kinds = ("compact"|"stateless") of guardian A, guardian B."""
    if len(kinds) != 2:
        raise ValueError("a vault transaction carries exactly 2 approvals")
    nonce = int(rpc.eth("eth_getTransactionCount", [vault, "latest"]), 16)
    chain_id = int(rpc.eth("eth_chainId"), 16)
    base = int(rpc.eth("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
    max_fee = max(4 * base, 2 * 10**9)
    v = unhex(vault)
    # frame 0 target None = tx.sender (only_verify must target the sender); frame 1 names the payer (the vault)
    fs = [ft.Frame(ft.MODE_VERIFY, ft.SCOPE_EXECUTION, None, approval_gas(kinds[0]), 0, 0, sel("validate()")),
          ft.Frame(ft.MODE_VERIFY, ft.SCOPE_PAYMENT, v, approval_gas(kinds[1]), 0, 0, sel("validate()")),
          ft.Frame(ft.MODE_SENDER, 0, v, exec_gas, state_gas, 0, execute_calldata(calls))]
    return ft.FrameTx(sender=v, nonce_seq=nonce, frames=fs,
                      signatures=[ft.Signature(ft.SCHEME_ARBITRARY), ft.Signature(ft.SCHEME_ARBITRARY)],
                      max_priority_fee=10**9, max_fee=max_fee, chain_id=chain_id)


def entry_signer(g, kind):
    return (lambda m: g.sign_stateless(m)) if kind == "stateless" else (lambda m: g.sign_compact(m))


def sign_entries(tx: ft.FrameTx, entry_fns):
    """Fill both signature entries: entry_fns[i](vault_message(sigHash)) -> entry bytes. Each guardian signs the
    same message, independently and in any order."""
    sh = tx.sig_hash()  # empty-msg ARBITRARY bytes are elided, so adding them does not change the hash
    m = vault_message(sh)
    for i, fn in enumerate(entry_fns):
        tx.signatures[i].signature = fn(m)
    assert tx.sig_hash() == sh
    return sh


def sign_vault_tx(tx: ft.FrameTx, approvers, kinds):
    return sign_entries(tx, [entry_signer(g, k) for g, k in zip(approvers, kinds)])


def simulate(tx: ft.FrameTx, trace=False) -> dict:
    return rpc.eth("ethrex_simulateFrameTransaction", [h(tx.raw()), "latest", {"trace": trace}])


def submit(net: Net, tx: ft.FrameTx, wait=True) -> dict:
    tx_hash = rpc.sidecar("eth_sendFrameTransaction", [tx.to_wire()])
    out = {"txHash": tx_hash, "submittedVia": net.cfg["sidecar"]}
    if not wait:
        return out
    end = time.time() + 180
    while time.time() < end:
        try:
            st = rpc.sidecar("skandha_frameTransactionStatus", [tx_hash])
            out["sidecarStatus"] = st.get("status")
        except rpc.RpcError:
            pass
        rc = rpc.eth("eth_getTransactionReceipt", [tx_hash])
        if rc:
            out["receipt"] = rc
            return out
        time.sleep(2)
    raise TimeoutError(f"{tx_hash} not mined: {out}")


def record_receipt(net: Net, label: str, out: dict, extra=None):
    recs = net.load("receipts.json", [])
    rc = out.get("receipt", {})
    recs.append({"label": label, "txHash": out["txHash"], "block": int(rc.get("blockNumber", "0x0"), 16),
                 "status": rc.get("status"), "gasUsed": int(rc.get("gasUsed", "0x0"), 16),
                 "frameReceipts": rc.get("frameReceipts"), "submittedVia": out.get("submittedVia"),
                 "sidecarStatus": out.get("sidecarStatus"), "network": net.name, "time": int(time.time()),
                 **(extra or {})})
    net.save("receipts.json", recs)


def approver_plan(gs, approvers, stateless=()):
    """The two approving guardians in entry order (increasing index) and their signature kinds."""
    idx = sorted(set(approvers))
    if len(approvers) != 2 or len(idx) != 2:
        raise ValueError("give exactly two different approving guardians")
    return [gs[i] for i in idx], ["stateless" if i in stateless else "compact" for i in idx], idx


def vault_call(net: Net, calls, approvers=(0, 1), stateless=(), label="vault call", gs=None, vault_label="vault",
               rotate_at: int = ROTATE_AT):
    """Build, 2-of-3 sign, simulate, submit and record one vault frame transaction.
    Compact approvers whose slot is nearly used up are rotated first (`rotate_at`); approvers with no leaf left
    sign stateless."""
    gs = gs or guardians(net)
    vault = net.deployment[vault_label]
    for i in sorted(set(approvers)):
        if i in stateless:
            continue
        g = gs[i]
        rem, q_max = g.remaining_leaves(), g.slot().q_max
        if rotation_due(rem, q_max, rotate_at):
            print(f"slot refresh: guardian {i} has {rem}/{q_max} leaves left; rotating to a fresh slot first")
            cmd_rotate(net, i, gs=gs, vault_label=vault_label)
    signers, kinds, idx = approver_plan(gs, approvers, stateless)
    eff = effective_kinds(kinds, [g.remaining_leaves() for g in signers])
    for i, k, e in zip(idx, kinds, eff):
        if k != e:
            print(f"stateless fallback: guardian {i} has no compact leaf left; it signs with its SPX key")
    kinds = eff
    tx = build_vault_tx(net, vault, calls, kinds)
    print("signing: " + ", ".join(f"guardian {i} ({k})" for i, k in zip(idx, kinds)))
    sh = sign_vault_tx(tx, signers, kinds)
    sim = simulate(tx)
    # `valid`/`violation` are ethrex's *canonical* (EIP-8141 public mempool) verdict. Leafguard's VERIFY frames
    # read the shared LeafLedger and exceed the combined 100k budget, so they are expected to be canonically
    # invalid and admitted by an ERC-8421 alt mempool (staked sender); what must hold is that execution succeeds.
    brief = {k: sim.get(k) for k in ("valid", "prefixShape", "violation", "canonicalMempoolViolation", "executionStatus",
                                     "gasUsed", "frames", "executionError", "failedFrameIndex", "haltReason", "revertData")}
    print("simulate:", json.dumps(brief))
    if sim.get("executionStatus") != "success":
        raise RuntimeError(f"simulated execution failed: {brief}")
    print(f"submitting: to {net.cfg['sidecar']}")
    out = submit(net, tx)
    rc = out["receipt"]
    print(f"{label}: tx {out['txHash']} status {rc['status']} block {int(rc['blockNumber'], 16)} "
          f"gasUsed {int(rc['gasUsed'], 16)}")
    record_receipt(net, label, out, {"sigHash": h(sh), "guardians": idx, "kinds": kinds,
                                     "simulation": brief, "vault": vault})
    out["kinds"], out["guardians"] = kinds, idx
    return out


def _others(exclude: int):
    return tuple(x for x in range(3) if x != exclude)


def _plus(idx, stateless):
    return "+".join(f"g{i}" + ("(spx)" if i in stateless else "") for i in sorted(idx))


def cmd_transfer(net: Net, to: str, wei: int, approvers=(0, 1), stateless=(), label=None, vault_label="vault",
                 rotate_at: int = ROTATE_AT, gs=None):
    return vault_call(net, [(to, wei, "0x")], approvers, stateless, label or f"transfer {_plus(approvers, stateless)}",
                      gs=gs, vault_label=vault_label, rotate_at=rotate_at)


def cmd_rotate(net: Net, g: int, pair=None, stateless=(), gs=None, vault_label="vault"):
    """Slot refresh / key rotation: guardian g gets a fresh compact slot (new random r, same height); the old slot
    is revoked in the same 2-of-3 transaction, approved by the other two guardians. An approver with no compact
    leaf left signs stateless automatically. Rotations never trigger further rotations."""
    gs = gs or guardians(net)
    pair = pair or _others(g)
    old = gs[g].slot()
    new_r = h(os.urandom(32))
    new_slot = CompactSlot(gs[g].keys.master_sk, unhex(new_r), old.h)
    vault = net.deployment[vault_label]
    calls = [(vault, 0, cast("calldata", "registerSlot(uint8,bytes16,bytes16)", str(g), h(new_slot.seed16), h(new_slot.root16))),
             (vault, 0, cast("calldata", "revokeSlot(bytes32)", h(old.slot_id)))]
    out = vault_call(net, calls, pair, stateless, label=f"rotate guardian {g} slot (approved by {_plus(pair, stateless)})",
                     gs=gs, vault_label=vault_label, rotate_at=0)
    gs[g].rec["slots"].append({"r": new_r, "h": old.h})
    save_guardians(gs)
    return out


def cmd_recover(net: Net, lost: int):
    """Key-loss recovery: the two other guardians install new master + slot keys for `lost`."""
    gs = guardians(net)
    pair = _others(lost)
    new_master = os.urandom(32)
    nk = GuardianKeys(lost, new_master)
    spx = nk.spx()
    new_r = os.urandom(32)
    slot = CompactSlot(new_master, new_r, SLOT_H)
    vault = net.deployment["vault"]
    data = cast("calldata", "replaceGuardian(uint8,(bytes32,bytes32),bytes16,bytes16)", str(lost),
                f"({h(spx.public[0])},{h(spx.public[1])})", h(slot.seed16), h(slot.root16))
    out = vault_call(net, [(vault, 0, data)], pair, label=f"recover lost guardian {lost} (approved by {_plus(pair, ())})", gs=gs)
    rec = net.load("guardians.json")
    rec[lost] = {"index": lost, "master_sk": h(new_master), "slots": [{"r": h(new_r), "h": SLOT_H}],
                 "replaced": rec[lost].get("replaced", 0) + 1}
    net.save("guardians.json", rec)
    for f in (f"guardian-{lost}-leaves.json",):
        if os.path.exists(net.path(f)):
            os.replace(net.path(f), net.path(f + ".lost-device"))
    return out


# ----------------------------------------------------------------------------- research: live attack on controlled test wallets
def cmd_attack_stale_restore(net: Net):
    """Outdated-backup restoration against our own devnet test wallets.

    Baseline: a vault without leaf tracking (JARDIN behaviour) and guardians using burn-before-sign only.
    Guardian 0 signs tx1 at leaf q; its device is then restored from a backup taken before that signature;
    it signs tx2 at the *same* leaf q. We show (1) the baseline vault includes both, (2) a watcher turns the
    two public signatures into an equivocation proof that revokes the slot in the LeafLedger, and (3) the
    same restored signer cannot get a reused leaf past Leafguard's VERIFY frame.
    """
    import shutil
    report = {}
    if not net.load("baseline-guardians.json"):
        net.save("baseline-guardians.json", [{"index": i, "master_sk": h(os.urandom(32)),
                                               "slots": [{"r": h(os.urandom(32)), "h": SLOT_H}]} for i in range(3)])
    bgs = guardians(net, "baseline-guardians.json", "baseline-guardian", anchored=False)
    deploy_vault(net, 0.05, 0.02, use_ledger=False, label="baselineVault", gs=bgs)
    g0 = bgs[0]
    leaf_file = g0.leaves.path
    backup = leaf_file + ".backup"
    shutil.copyfile(leaf_file, backup)  # the backup the device will later be restored from
    to = net.deployer()
    out1 = vault_call(net, [(to, 1, "0x")], (0, 1), label="baseline vault: tx1 (guardian 0, leaf q)", gs=bgs,
                      vault_label="baselineVault")
    q1 = out1["receipt"] and parse_q(net, out1["txHash"])
    shutil.copyfile(backup, leaf_file)  # silent restore of the outdated backup
    out2 = vault_call(net, [(to, 2, "0x")], (0, 2), label="baseline vault: tx2 after stale restore (guardian 0, SAME leaf)",
                      gs=bgs, vault_label="baselineVault")
    q2 = parse_q(net, out2["txHash"])
    report["baseline"] = {"tx1": out1["txHash"], "tx2": out2["txHash"], "leaf_tx1": q1, "leaf_tx2": q2,
                          "both_included": out1["receipt"]["status"] == "0x1" and out2["receipt"]["status"] == "0x1"}
    # (2) permissionless equivocation report from the two public transactions
    e_a, m_a = entry0_and_message(net, out1["txHash"])
    e_b, m_b = entry0_and_message(net, out2["txHash"])
    slot = g0.slot()
    data = cast("calldata", "reportEquivocation(bytes16,bytes16,bytes32,bytes,bytes32,bytes)", h(slot.seed16), h(slot.root16),
                h(m_a), h(e_a[34:]), h(m_b), h(e_b[34:]))
    r = net.send_legacy(net.deployment["ledger"], data, gas=1_000_000)
    report["equivocationReport"] = {"tx": r["transactionHash"], "status": r["status"],
                                    "slotRevoked": net.ledger_hwm(slot.slot_id) >= 2**255}
    recs = net.load("receipts.json", [])
    recs.append({"label": "watcher: equivocation proof revokes the reused slot (LeafLedger.reportEquivocation)",
                 "txHash": r["transactionHash"], "block": int(r["blockNumber"], 16), "status": r["status"],
                 "gasUsed": int(r["gasUsed"], 16), "network": net.name, "time": int(time.time()), "legacyTx": True})
    net.save("receipts.json", recs)
    # (3) the same stale signer against Leafguard
    gs = guardians(net)
    pg = gs[0]
    pslot = pg.slot()
    hwm = net.ledger_hwm(pslot.slot_id)
    if hwm >= 1:
        tx = build_vault_tx(net, net.deployment["vault"], [(to, 3, "0x")], ["compact", "compact"])
        # guardian 0 (restored signer, reused leaf) is entry 0 (lowest index), checked in VERIFY frame 0
        sign_entries(tx, [lambda m: compact_entry(0, pslot, pslot.sign(m, hwm)), entry_signer(gs[1], "compact")])
        sim = simulate(tx)
        try:
            rpc.sidecar("eth_sendFrameTransaction", [tx.to_wire()])
            admitted = "ADMITTED (unexpected)"
        except rpc.RpcError as e:
            admitted = f"rejected: {json.dumps(e.error)[:300]}"
        report["pqVault"] = {"reusedLeaf": hwm, "simulation": {k: sim.get(k) for k in ("valid", "violation", "executionError")},
                             "altMempool": admitted}
    net.save("attack-stale-restore.json", report)
    print(json.dumps(report, indent=1))
    return report


def raw_tx(net: Net, tx_hash: str) -> ft.FrameTx:
    raw = rpc.eth("debug_getRawTransaction", [tx_hash])
    return ft.decode_raw(unhex(raw))


def entry0_and_message(net: Net, tx_hash: str):
    """From an included vault transaction (public data): the first approval's entry and the message it signed."""
    tx = raw_tx(net, tx_hash)
    return tx.signatures[0].signature, vault_message(tx.sig_hash())


def parse_q(net: Net, tx_hash: str) -> int:
    e0, _ = entry0_and_message(net, tx_hash)
    return e0[34 + 2592]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("network", choices=["public", "local"])
    ap.add_argument("cmd")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--fund", type=float, default=0.3)
    ap.add_argument("--stake", type=float, default=0.02)
    ap.add_argument("--guardians", default="0,1", help="the two approving guardian indices")
    ap.add_argument("--stateless", default="", help="approvers that sign with their stateless SPX key")
    ap.add_argument("--vault", default="vault", help="deployment label of the vault to use / deploy")
    ap.add_argument("--rotate-at", type=int, default=ROTATE_AT,
                    help="rotate an approver's slot first when this many leaves (or fewer) are left; 0 = off")
    ap.add_argument("--slot-h", type=int, default=SLOT_H, help="new-guardians: compact slot height (2^h leaves)")
    a = ap.parse_args(argv)
    net = Net(a.network)
    if a.cmd == "status":
        print(json.dumps({"head": int(rpc.eth("eth_blockNumber"), 16), "deployment": net.deployment}, indent=1))
    elif a.cmd == "deploy-infra":
        deploy_infra(net)
    elif a.cmd == "new-guardians":
        new_guardians(net, a.slot_h)
    elif a.cmd == "deploy-vault":
        deploy_vault(net, a.fund, a.stake, label=a.vault)
    elif a.cmd == "transfer":
        cmd_transfer(net, a.args[0], int(a.args[1]), tuple(int(x) for x in a.guardians.split(",")),
                     tuple(int(x) for x in a.stateless.split(",") if x), vault_label=a.vault, rotate_at=a.rotate_at)
    elif a.cmd == "rotate":
        g = int(a.args[0])
        pair = tuple(int(x) for x in a.guardians.split(",")) if g not in (0, 1) or a.guardians != "0,1" else None
        cmd_rotate(net, g, pair, tuple(int(x) for x in a.stateless.split(",") if x), vault_label=a.vault)
    elif a.cmd == "recover":
        cmd_recover(net, int(a.args[0]))
    elif a.cmd == "attack-stale-restore":
        cmd_attack_stale_restore(net)
    else:
        sys.exit(f"unknown command {a.cmd}")


if __name__ == "__main__":
    main()
