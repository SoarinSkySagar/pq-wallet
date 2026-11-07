"""Signer-side leaf state for JARDIN compact slots.

A compact slot is safe only if no leaf q ever signs two different messages. This module holds the
guardian device's counter. `DurableLeafStore` is the *baseline* every stateful-HBS deployment
relies on (JARDIN: "burn-before-sign"; NIST SP 800-208 §8.1: update the state in non-volatile
storage before releasing a signature): the next q is written and fsync'd before the signature
exists. On its own it cannot detect that the file was restored from an older backup, copied to a
second device, or rolled back - see research/ for the attacks and for `AnchoredLeafStore`, the
mitigation Leafguard's signer uses: it never signs at or below the chain-anchored high-water mark.
"""
import json
import os
import tempfile
from typing import Callable, Optional


def atomic_write_json(path: str, obj) -> None:
    """write temp -> fsync -> rename -> fsync(dir): the file is either old or new, never torn."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class LeafExhausted(Exception):
    pass


class DurableLeafStore:
    """Baseline: per-slot counter in a JSON file, burned (fsync'd) before signing."""

    def __init__(self, path: str):
        self.path = path
        if not os.path.exists(path):
            atomic_write_json(path, {"slots": {}})

    def _load(self):
        with open(self.path) as f:
            return json.load(f)

    def peek(self, slot_id: bytes) -> int:
        return self._load()["slots"].get(slot_id.hex(), {}).get("next_q", 1)

    def reserve(self, slot_id: bytes, q_max: int, floor: int = 1) -> int:
        st = self._load()
        rec = st["slots"].setdefault(slot_id.hex(), {"next_q": 1})
        q = max(rec["next_q"], floor)
        if q > q_max:
            raise LeafExhausted(f"slot {slot_id.hex()[:10]} exhausted (q_max={q_max}); rotate to a new slot")
        rec["next_q"] = q + 1
        atomic_write_json(self.path, st)  # burn before sign
        return q


class AnchoredLeafStore(DurableLeafStore):
    """Mitigation: local burn-before-sign *and* never below the chain-anchored high-water mark.

    `chain_hwm(slot_id)` returns the LeafLedger value (highest leaf any vault consumed). A device
    restored from an outdated backup, a clone that missed the other copy's signatures, or a
    rolled-back counter therefore resumes above every leaf that reached the chain. Leaves that were
    signed but never landed (in flight, dropped) remain the residual window; research/ quantifies it.
    """

    def __init__(self, path: str, chain_hwm: Callable[[bytes], int]):
        super().__init__(path)
        self.chain_hwm = chain_hwm

    def reserve(self, slot_id: bytes, q_max: int, floor: int = 1) -> int:
        return super().reserve(slot_id, q_max, max(floor, self.chain_hwm(slot_id) + 1))
