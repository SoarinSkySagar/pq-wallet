"""Adversarial simulation of stateful JARDIN compact-slot signers under crashes, stale backups,
cloning, interrupted signing and state rollback.

Everything cryptographic is real: signatures come from the upstream JARDIN plain-FORS signer
(pqv.jardin.CompactSlot -> third_party/JARDIN), acceptance uses the upstream reference verifier,
and the attacker's view is the exact set of FORS secrets the released signatures reveal.
What is modelled: device storage (committed vs. not-yet-fsync'd), the chain (in-order processing;
`ledger=True` enforces q > high-water mark like Leafguard's LeafLedger, `ledger=False` is the JARDIN
baseline that keeps no on-chain leaf state), and transaction landing (delays / drops).

Metrics (per run):
  max_gamma           most distinct messages signed under one leaf (gamma=1 is the safe case)
  reused_leaves       leaves with gamma >= 2
  min_bits            lowest empirical forgery cost (bits) over all leaves
  exploitable_bits    lowest forgery cost over leaves the chain would still accept (q > hwm with a
                      ledger; every leaf without one) - the security that actually protects funds
  double_accepted     leaves on which the chain accepted two different messages (on-chain replay)
  exposure_steps      blocks during which a reused leaf was still acceptable on chain (forgeable)
  exposure_min_bits   lowest forgery cost seen during those blocks
  refused / retires / stateless / reslots   liveness cost: waits/refusals, leaf skips after a crash (no tx),
                      SPX fallbacks, new-slot registrations
"""
import math
import os
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from pqv.jardin import CompactSlot  # noqa: E402
from pqv.hashing import keccak256  # noqa: E402
from research.policies import POLICIES, Decision, Refused, Storage  # noqa: E402

A = 4  # JARDIN plain FORS: k=32 trees of height a=4
K = 32


def forgery_bits(index_sets: List[List[tuple]]) -> float:
    """Expected work (bits) to forge at this leaf: the forger grinds R until all K digest indices
    land on revealed leaves; success probability per try = prod_t (revealed_t / 2^A)."""
    if not index_sets:
        return float(K * A)
    revealed = defaultdict(set)
    for idx in index_sets:
        for t, j in idx:
            revealed[t].add(j)
    return -sum(math.log2(len(revealed[t]) / (1 << A)) for t in range(K))


def expected_bits(gamma: int) -> float:
    return -K * math.log2(1 - (1 - 2 ** -A) ** gamma)


@dataclass
class Device:
    name: str
    policy: object
    st: Storage
    slot_gen: int = 0  # which slot (reslot policy rotates)
    restored: bool = False  # the device *knows* it was restored (restore wizard); silent restores keep False
    host_lies: bool = False  # host reports chain hwm = 0 to the device


@dataclass
class World:
    slots: Dict[int, CompactSlot]
    ledger: bool
    rng: random.Random
    revoke: bool = False  # LeafLedger.reportEquivocation: a watcher reports reuse one block after it is broadcast
    hwm: Dict[int, int] = field(default_factory=lambda: defaultdict(int))
    leaked: Dict[tuple, Dict[bytes, list]] = field(default_factory=lambda: defaultdict(dict))  # (gen,q)->{msg: idx}
    pending: List[tuple] = field(default_factory=list)  # (land_at_step, gen, q, msg, sig)
    accepted: Dict[tuple, set] = field(default_factory=dict)
    step: int = 0
    revoked: set = field(default_factory=set)
    reports_due: Dict[int, int] = field(default_factory=dict)  # gen -> block at which the report lands
    exposure_steps: int = 0
    exposure_min_bits: float = 128.0
    stats: Dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def slot(self, gen):
        return self.slots[gen]

    def submit(self, gen, q, msg, sig, delay=0, drop=False):
        if not drop:
            self.pending.append((self.step + delay, gen, q, msg, sig))

    def mine(self):
        keep = []
        for item in sorted(self.pending, key=lambda x: x[0]):
            land, gen, q, msg, sig = item
            if land > self.step:
                keep.append(item)
                continue
            if self.ledger and q <= self.hwm[gen]:
                self.stats["rejected_stale"] += 1
                continue
            assert self.slot(gen).verify(msg, sig)
            acc = self.accepted.setdefault((gen, q), set())
            if acc and msg not in acc:
                self.stats["double_accepted"] += 1  # the chain accepted a second message on one leaf
            acc.add(msg)
            self.hwm[gen] = max(self.hwm[gen], q)
            self.stats["landed"] += 1
        self.pending = keep
        if self.revoke:
            for gen, due in list(self.reports_due.items()):
                if due <= self.step:
                    self.revoked.add(gen)
                    self.hwm[gen] = 1 << 62  # REVOKED: every leaf of the slot is now refused
                    del self.reports_due[gen]
                    self.stats["revocations"] += 1
            for (gen, q), msgs in self.leaked.items():
                # A watcher only needs to report reuse that is still exploitable (leaf above the mark);
                # reuse of an already-retired leaf cannot be used against any vault.
                if len(msgs) >= 2 and gen not in self.revoked and gen not in self.reports_due and q > self.hwm[gen]:
                    self.reports_due[gen] = self.step + 1
        self.step += 1
        self._exposure()

    def _exposure(self):
        """Is there, right now, a reused leaf the chain would still accept? (forgery opportunity)"""
        worst = None
        for (gen, q), msgs in self.leaked.items():
            if len(msgs) >= 2 and ((not self.ledger) or q > self.hwm[gen]):
                b = forgery_bits(list(msgs.values()))
                worst = b if worst is None else min(worst, b)
        if worst is not None:
            self.exposure_steps += 1
            self.exposure_min_bits = min(self.exposure_min_bits, worst)


def sign(world: World, dev: Device, crash_at: Optional[str] = None, delay=0, drop=False):
    """One signing request on `dev`. crash_at in {None, 'after_burn', 'after_release'}."""
    gen = dev.slot_gen
    if gen in world.revoked:
        # Slot revoked for equivocation: this device draws its *own* fresh r (JARDIN per-device slot
        # derivation) and registers a new slot through the 2-of-3 path. Two clones draw different r,
        # so after this they can no longer collide.
        dev.slot_gen = gen = max(world.slots) + 1
        world.slots[gen] = CompactSlot(keccak256(b"sim-master"), world.rng.randbytes(32), world.slots[0].h)
        dev.st = Storage(committed={"next_q": 1})
        world.stats["reslots"] += 1
    view = 0 if dev.host_lies else world.hwm[gen]
    try:
        d: Decision = dev.policy.decide(dev.st, view, dev.restored)
    except Refused as e:
        in_flight = any(p[1] == gen for p in world.pending)
        if "window" in str(e) and not dev.host_lies and not in_flight:
            # Nothing of ours is pending, yet q > hwm + W: a leaf was burned but never landed (crash,
            # drop). Moving forward never reuses a leaf, so retire it on-chain with a stateless (SPX)
            # or 2-of-3 authorised ledger advance, then continue. Costs one extra transaction.
            world.hwm[gen] = max(world.hwm[gen], dev.st.read("next_q", 1) - 1)
            world.stats["retires"] += 1
            d = dev.policy.decide(dev.st, world.hwm[gen], dev.restored)
        else:
            world.stats["refused"] += 1  # waits for confirmation (or, with a lying host, blocks: fail-safe)
            return
    if d.stateless:
        world.stats["stateless"] += 1  # SPX path: stateless, no leaf consumed
        return
    if d.new_slot:
        dev.slot_gen = max(world.slots) + 1
        world.slots[dev.slot_gen] = CompactSlot(keccak256(b"sim-master"), keccak256(b"slot%d" % dev.slot_gen), world.slots[0].h)
        gen = dev.slot_gen
        world.stats["reslots"] += 1
        dev.restored = False
    q = d.q
    pol = dev.policy
    if pol.burn_before_sign:
        dev.st.write("next_q", q + 1, fsync=pol.fsync)
        if crash_at == "after_burn":
            dev.st.crash()
            world.stats["crashes"] += 1
            return
    msg = world.rng.randbytes(32)
    slot = world.slot(gen)
    sig = slot.sign(msg, q)
    world.leaked[(gen, q)][msg] = slot.revealed_secrets(msg, q)  # released => observable
    world.submit(gen, q, msg, sig, delay, drop)
    if not pol.burn_before_sign:
        if crash_at == "after_release":
            dev.st.crash()
            world.stats["crashes"] += 1
            return
        dev.st.write("next_q", q + 1, fsync=pol.fsync)
    elif crash_at == "after_release":
        dev.st.crash()
        world.stats["crashes"] += 1


def metrics(world: World) -> dict:
    gam = {k: len(v) for k, v in world.leaked.items()}
    bits = {k: forgery_bits(list(v.values())) for k, v in world.leaked.items()}
    acceptable = [b for (gen, q), b in bits.items() if (not world.ledger) or q > world.hwm[gen]]
    return {
        "max_gamma": max(gam.values(), default=0),
        "reused_leaves": sum(1 for g in gam.values() if g >= 2),
        "min_bits": round(min(bits.values(), default=128.0), 1),
        "exploitable_bits": round(min(acceptable, default=128.0), 1),
        "double_accepted": world.stats["double_accepted"],
        "exposure_steps": world.exposure_steps,
        "exposure_min_bits": round(world.exposure_min_bits, 1),
        "retires": world.stats["retires"],
        "revocations": world.stats["revocations"],
        "refused": world.stats["refused"],
        "stateless": world.stats["stateless"],
        "reslots": world.stats["reslots"],
        "signatures": sum(gam.values()),
        "landed": world.stats["landed"],
    }


# ----------------------------------------------------------------------------- scenarios
def _world(slot, ledger, seed):
    """ledger: False (JARDIN baseline), True (LeafLedger high-water mark), or "revoke" (+ equivocation reports)."""
    return World({0: slot}, bool(ledger), random.Random(seed), revoke=(ledger == "revoke"))


def s_crash(slot, P, ledger, seed, n=40, p=0.25):
    """S1/S4: crashes (power loss) at random points while signing; every landed tx confirms."""
    w = _world(slot, ledger, seed)
    d = Device("A", P(slot.q_max), Storage())
    for _ in range(n):
        point = w.rng.choice([None, None, "after_burn", "after_release"]) if w.rng.random() < p * 2 else None
        sign(w, d, point)
        w.mine()
    return metrics(w)


def s_stale_backup(slot, P, ledger, seed, n=30, backup_at=10, restore_at=20, silent=True):
    """S2: restore a state backup taken `restore_at - backup_at` signatures ago."""
    w = _world(slot, ledger, seed)
    d = Device("A", P(slot.q_max), Storage())
    backup = None
    for i in range(n):
        if i == backup_at:
            backup = d.st.copy()
        if i == restore_at:
            d.st = backup.copy()
            d.restored = not silent
        sign(w, d)
        w.mine()
    return metrics(w)


def s_lost_state(slot, P, ledger, seed, n=30, lose_at=15):
    """State wiped (device reset / new phone with only the mnemonic)."""
    w = _world(slot, ledger, seed)
    d = Device("A", P(slot.q_max), Storage())
    for i in range(n):
        if i == lose_at:
            d.st = Storage(present=False)
        sign(w, d)
        w.mine()
    return metrics(w)


def s_clone(slot, P, ledger, seed, n=30, clone_at=10, delay=1):
    """S3: the device state is cloned (VM snapshot / disk image / backup restored on a second
    device); both copies keep signing concurrently and their transactions race (landing delay)."""
    w = _world(slot, ledger, seed)
    a = Device("A", P(slot.q_max), Storage())
    b = None
    for i in range(n):
        if i == clone_at:
            b = Device("B", P(slot.q_max), a.st.copy())
        sign(w, a, delay=delay)
        if b:
            sign(w, b, delay=delay)
        w.mine()
    return metrics(w)


def s_rollback(slot, P, ledger, seed, n=40, every=5, depth=3):
    """S5: fault injection rewinds the counter by `depth` every `every` signatures."""
    w = _world(slot, ledger, seed)
    d = Device("A", P(slot.q_max), Storage())
    for i in range(n):
        if i and i % every == 0:
            cur = d.st.read("next_q", 1)
            d.st.committed["next_q"] = max(1, cur - depth)
            d.st.volatile.clear()
        sign(w, d)
        w.mine()
    return metrics(w)


def s_rollback_inflight(slot, P, ledger, seed, n=40, every=5, depth=3, delay=3, drop_p=0.3):
    """S5 with transactions that land late or get dropped: the residual window of chain anchoring."""
    w = _world(slot, ledger, seed)
    d = Device("A", P(slot.q_max), Storage())
    for i in range(n):
        if i and i % every == 0:
            cur = d.st.read("next_q", 1)
            d.st.committed["next_q"] = max(1, cur - depth)
            d.st.volatile.clear()
        sign(w, d, delay=delay, drop=w.rng.random() < drop_p)
        w.mine()
    for _ in range(delay + 1):
        w.mine()
    return metrics(w)


def s_lying_host(slot, P, ledger, seed, n=30, backup_at=5, restore_at=20):
    """Anchoring's trust assumption broken: stale restore *and* the host feeds the device hwm=0."""
    w = _world(slot, ledger, seed)
    d = Device("A", P(slot.q_max), Storage(), host_lies=True)
    backup = None
    for i in range(n):
        if i == backup_at:
            backup = d.st.copy()
        if i == restore_at:
            d.st = backup.copy()
        sign(w, d)
        w.mine()
    return metrics(w)


SCENARIOS = {
    "crash": s_crash,
    "stale_backup_silent": lambda s, P, l, seed: s_stale_backup(s, P, l, seed, silent=True),
    "stale_backup_known": lambda s, P, l, seed: s_stale_backup(s, P, l, seed, silent=False),
    "lost_state": s_lost_state,
    "clone": s_clone,
    "rollback": s_rollback,
    "rollback_inflight": s_rollback_inflight,
    "lying_host": s_lying_host,
}
