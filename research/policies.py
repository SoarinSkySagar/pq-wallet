"""Signer state policies for a JARDIN compact slot (what decides the leaf q of the next signature).

Each policy is a model of an approach from the literature or from existing code, implemented over a
storage object that the simulator can snapshot, restore, clone, roll back and crash:

  naive        persist-after-sign, no fsync (a common implementation bug; included as a lower bound)
  durable      burn-before-sign with atomic+fsync'd state (JARDIN SE app; NIST SP 800-208 sec. 8.1)
  fallback     durable, plus SHRINCS-style "static backup": if the state is *missing* the device
               uses the stateless path (SPX) instead of guessing q (a stale-but-present state cannot
               be told apart from a fresh one, so it is still used)
  reslot       durable, plus JARDIN's restore rule: a device restored from the mnemonic draws a fresh
               r and registers a new slot; applied whenever the device knows it was restored
  anchored     durable, plus q > chain high-water mark (Leafguard LeafLedger floor)
  anchored_w1  anchored, plus an in-flight window W=1: never sign q > hwm + 1, so at most one
               signed-but-unconfirmed leaf exists at any time (Leafguard default)
"""
from dataclasses import dataclass, field
from typing import Dict, Optional


class Refused(Exception):
    """The policy declined to produce a compact signature (liveness cost, not a safety failure)."""


@dataclass
class Storage:
    """Durable device state. `committed` survives crashes; `volatile` models a written-but-not-fsynced
    value that a crash (power loss) can drop."""
    committed: Dict[str, int] = field(default_factory=dict)
    volatile: Dict[str, int] = field(default_factory=dict)
    present: bool = True  # False = state lost/wiped

    def read(self, key, default):
        if key in self.volatile:
            return self.volatile[key]
        return self.committed.get(key, default)

    def write(self, key, value, fsync: bool):
        if fsync:
            self.committed[key] = value
            self.volatile.pop(key, None)
        else:
            self.volatile[key] = value

    def crash(self):
        self.volatile.clear()

    def copy(self):
        return Storage(dict(self.committed), dict(self.volatile), self.present)


@dataclass
class Decision:
    q: Optional[int]  # leaf to use for a compact signature, or None
    stateless: bool = False  # use the SPX path instead (no leaf consumed)
    new_slot: bool = False  # device rotated to a fresh slot (needs a 2-of-3 registerSlot)


class Policy:
    name = "base"
    burn_before_sign = True
    fsync = True

    def __init__(self, q_max: int, window: Optional[int] = None):
        self.q_max, self.window = q_max, window

    def floor(self, chain_hwm: int) -> int:
        return 1

    def decide(self, st: Storage, chain_hwm: int, restored: bool) -> Decision:
        if not st.present:
            return self.on_missing(st, chain_hwm)
        q = max(st.read("next_q", 1), self.floor(chain_hwm))
        if self.window is not None and q > chain_hwm + self.window:
            raise Refused(f"window: q={q} exceeds hwm+{self.window}")
        if q > self.q_max:
            raise Refused("slot exhausted")
        return Decision(q)

    def on_missing(self, st: Storage, chain_hwm: int) -> Decision:
        st.present = True  # a device with no state starts over at q=1 (what a naive restore does)
        st.committed = {}
        return self.decide(st, chain_hwm, False)


class Naive(Policy):
    name, burn_before_sign, fsync = "naive", False, False


class Durable(Policy):
    name = "durable"


class Fallback(Durable):
    name = "fallback"

    def on_missing(self, st, chain_hwm):
        return Decision(None, stateless=True)


class Reslot(Durable):
    name = "reslot"

    def decide(self, st, chain_hwm, restored):
        if restored or not st.present:
            st.present = True
            st.committed = {"next_q": 1}
            st.volatile = {}
            return Decision(1, new_slot=True)
        return super().decide(st, chain_hwm, restored)


class Anchored(Durable):
    name = "anchored"

    def floor(self, chain_hwm):
        return chain_hwm + 1


class AnchoredW1(Anchored):
    name = "anchored_w1"

    def __init__(self, q_max):
        super().__init__(q_max, window=1)


POLICIES = {p.name: p for p in (Naive, Durable, Fallback, Reslot, Anchored, AnchoredW1)}
