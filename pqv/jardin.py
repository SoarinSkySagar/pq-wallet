"""Guardian keys for Leafguard, on top of the upstream JARDIN reference signers.

Every hash-based operation (key generation, signing, local verification) is delegated to the
unmodified files in third_party/JARDIN/script:
  - jardin_spx_signer.py         plain SPHINCS+ (stateless; JARDIN "Type 1" / registration path)
  - jardin_fors_plain_signer.py  plain FORS, k=32 a=4, outer Merkle tree of 2^h leaves (stateful compact path)
This module only wires keys together and encodes Leafguard signature entries.

Slot derivation follows the JARDIN write-up (sub-key from HMAC-SHA512 over the master secret and a
fresh per-slot random value r), feeding JARDIN's own `jardin_derive_keys`.
"""
import contextlib
import functools
import io
import os
import threading
from dataclasses import dataclass

from .hashing import enable_jardin_imports, keccak256

enable_jardin_imports()
import jardin_fors_plain_signer as _F  # noqa: E402
import jardin_spx_signer as _S  # noqa: E402
from jardin_primitives import hmac512  # noqa: E402

SIG_STATELESS = 0x01
SIG_COMPACT = 0x02
DOMAIN = keccak256(b"PQ-Vault/v2")
FORS_SIG_BASE = _F.BASE_LEN  # 2593
SPX_SIG_LEN = _S.SIG_LEN  # 6512


def vault_message(sig_hash: bytes) -> bytes:
    """What every approving guardian signs: keccak256(abi.encode(DOMAIN, sigHash)) — PQVault.messageFor.

    Each approval is verified in its own VERIFY frame, so neither needs to commit to the other: the two
    guardians sign independently, in any order."""
    assert len(sig_hash) == 32
    return keccak256(DOMAIN + sig_hash)


def _b32(x: int) -> bytes:
    return x.to_bytes(32, "big")


# The upstream JARDIN hash hot path writes into shared module-level buffers (jardin_primitives._BUF96/_BUF128),
# so two threads computing JARDIN hashes at once corrupt each other's results (seen as a wrong slot root inside a
# signature entry). Every call into the upstream signers is serialised process-wide. Upstream code is unchanged.
_JARDIN = threading.RLock()


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _JARDIN:
            return fn(*args, **kwargs)
    return wrapper


class SpxMaster:
    """A guardian's stateless master key (JARDIN plain SPHINCS+, 6,512-byte signatures)."""

    @_serialized
    def __init__(self, master_sk: bytes):
        assert len(master_sk) == 32
        self.sk_seed, self.sk_prf, self.pk_seed = _S.derive_spx_keys(master_sk)
        self.pk_root = _S.build_pk_root(self.pk_seed, self.sk_seed)

    @property
    def public(self):
        return _b32(self.pk_seed), _b32(self.pk_root)

    @_serialized
    def sign(self, message: bytes, counter: int = 0) -> bytes:
        return _S.spx_sign(self.pk_seed, self.sk_seed, self.sk_prf, self.pk_root,
                           int.from_bytes(message, "big"), counter)

    @_serialized
    def verify(self, message: bytes, sig: bytes) -> bool:
        return _S.spx_verify(self.pk_seed, self.pk_root, int.from_bytes(message, "big"), sig)


class CompactSlot:
    """A JARDIN compact slot: 2^h few-time plain-FORS keys under one Merkle root.

    This object is deliberately *stateless*: `sign(message, q)` signs with whatever leaf it is given.
    Choosing q (and never reusing it for a different message) is the job of a signer state policy
    (pqv.state), which is what the research in research/ attacks and hardens.
    """

    @_serialized
    def __init__(self, master_sk: bytes, r: bytes, h: int = 7):
        assert len(r) == 32 and 2 <= h <= 8
        self.r, self.h = r, h
        entropy = hmac512(master_sk, b"PQ-Vault/slot/v1" + r)[:32]
        self.pk_seed, self.sk_seed = _F.jardin_derive_keys(int.from_bytes(entropy, "big"))
        with contextlib.redirect_stderr(io.StringIO()):  # upstream prints per-leaf progress
            self.levels, self.pk_root = _F.build_balanced_tree(self.pk_seed, self.sk_seed, h)

    @property
    def q_max(self) -> int:
        return 1 << self.h

    @property
    def seed16(self) -> bytes:
        return _b32(self.pk_seed)[:16]

    @property
    def root16(self) -> bytes:
        return _b32(self.pk_root)[:16]

    @property
    def slot_id(self) -> bytes:
        return keccak256(self.seed16 + self.root16)

    @_serialized
    def sign(self, message: bytes, q: int) -> bytes:
        if not 1 <= q <= self.q_max:
            raise ValueError(f"leaf q={q} outside [1, {self.q_max}]: slot exhausted")
        sig, _r, _digest = _F.fors_plain_sign(self.pk_seed, self.sk_seed, self.pk_root, self.levels,
                                              int.from_bytes(message, "big"), q, self.h)
        return sig

    @_serialized
    def verify(self, message: bytes, sig: bytes) -> bool:
        return _F.fors_plain_verify(self.pk_seed, self.pk_root, int.from_bytes(message, "big"), sig)

    @_serialized
    def revealed_secrets(self, message: bytes, q: int):
        """(tree t, leaf index) pairs whose FORS secrets a signature on `message` at leaf q reveals.
        Used by research/ to measure reuse damage; mirrors the upstream index derivation."""
        m = int.from_bytes(message, "big")
        R = _F.derive_R(self.sk_seed, m, q)
        digest = _F.h_msg(self.pk_seed, self.pk_root, R, m)
        return [(t, (digest >> (t * _F.A)) & _F.A_MASK) for t in range(_F.K)]


@dataclass
class GuardianKeys:
    index: int
    master_sk: bytes

    def spx(self) -> SpxMaster:
        return SpxMaster(self.master_sk)

    def new_slot(self, r: bytes = None, h: int = 7) -> CompactSlot:
        return CompactSlot(self.master_sk, r if r is not None else os.urandom(32), h)


def compact_entry(guardian: int, slot: CompactSlot, fors_sig: bytes) -> bytes:
    return bytes([guardian, SIG_COMPACT]) + slot.seed16 + slot.root16 + fors_sig


def stateless_entry(guardian: int, spx_sig: bytes) -> bytes:
    assert len(spx_sig) == SPX_SIG_LEN
    return bytes([guardian, SIG_STATELESS]) + spx_sig


def parse_compact_entry(entry: bytes):
    """-> (guardian, slot_id, q, fors_sig)"""
    assert entry[1] == SIG_COMPACT
    fors = entry[34:]
    return entry[0], keccak256(entry[2:34]), fors[2592], fors
