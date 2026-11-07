"""Move a generated devnet's genesis to "now", so every container boot starts a fresh chain at the current time.

    python3 patch_genesis.py <network-configs dir> [delay seconds, default 20]

The network files (infra/render/network/) were produced by Kurtosis' ethereum-genesis-generator for the local
devnet (ethrex eip8141-tracer + Lighthouse focil): EL genesis.json, CL config.yaml + genesis.ssz, 32 validator
keys. A beacon chain cannot start from an old genesis with a single node (it would be thousands of slots behind,
with nobody to sync from), so at boot we shift genesis forward:

- genesis.ssz: BeaconState.genesis_time is its first field (uint64, little endian, bytes 0..8). Changing it does
  not touch the validators, so genesis_validators_root (and every keystore) stays valid.
- config.yaml: MIN_GENESIS_TIME = the new time.
- genesis.json: execution-layer forks are timestamp based. Every fork time after the old genesis (Amsterdam at
  Gloas epoch 1, Bogota/Hegota at Heze epoch 2) is shifted by the same amount, so the EL and CL forks still
  line up. The EL genesis block itself is unchanged (its timestamp and the forks active at it are the same), so
  its hash still matches the execution payload header inside genesis.ssz.
"""
import json
import os
import re
import struct
import sys
import time


def main():
    d = sys.argv[1]
    delay = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    ssz_p, cfg_p, gen_p = (os.path.join(d, f) for f in ("genesis.ssz", "config.yaml", "genesis.json"))
    ssz = bytearray(open(ssz_p, "rb").read())
    old = struct.unpack("<Q", ssz[:8])[0]
    new = int(time.time()) + delay
    shift = new - old
    ssz[:8] = struct.pack("<Q", new)
    open(ssz_p, "wb").write(ssz)

    cfg = open(cfg_p).read()
    cfg = re.sub(r"(?m)^MIN_GENESIS_TIME:.*$", f"MIN_GENESIS_TIME: {new}", cfg)
    open(cfg_p, "w").write(cfg)

    g = json.load(open(gen_p))
    moved = {}
    for k, v in g["config"].items():
        if k.endswith("Time") and isinstance(v, int) and v > old:
            g["config"][k] = v + shift
            moved[k] = f"+{v - old}s"
    json.dump(g, open(gen_p, "w"), indent=1)
    print(f"genesis moved to {new} ({time.strftime('%H:%M:%S', time.gmtime(new))} UTC); EL forks after genesis: {moved}")


if __name__ == "__main__":
    main()
