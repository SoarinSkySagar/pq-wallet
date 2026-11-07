#!/usr/bin/env python3
"""Record how mempools treat a 2-of-3 Leafguard three-frame transaction (evidence for REPORT §8.1).

    python -m research.mempool_admission <network> <tag> [--vault vault] [--guardians 0,1] [--stateless ""]

Builds and signs one transfer (1 wei to the deployer) with the given approvers, simulates it on ethrex
(per-frame gas), submits it to (a) the configured ERC-8421 sidecar and (b) ethrex's canonical EIP-8141
mempool, and appends the node responses to .devnet/<network>/mempool-admission.json under <tag>. If the
sidecar admits it, waits for the receipt and records it like any other vault transaction.
Signing burns real leaves (burn-before-sign), exactly as a normal transfer does.
"""
import argparse
import json
import time

from pqv import devnet as dv
from pqv import rpc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("network", choices=["public", "local"])
    ap.add_argument("tag")
    ap.add_argument("--vault", default="vault")
    ap.add_argument("--guardians", default="0,1")
    ap.add_argument("--stateless", default="")
    a = ap.parse_args(argv)
    net = dv.Net(a.network)
    gs = dv.guardians(net)
    approvers = tuple(int(x) for x in a.guardians.split(","))
    stateless = tuple(int(x) for x in a.stateless.split(",") if x)
    signers, kinds, idx = dv.approver_plan(gs, approvers, stateless)
    vault = net.deployment[a.vault]
    tx = dv.build_vault_tx(net, vault, [(net.deployer(), 1, "0x")], kinds)
    sh = dv.sign_vault_tx(tx, signers, kinds)
    sim = dv.simulate(tx)
    rec = {"tag": a.tag, "time": int(time.time()), "vault": vault, "vaultLabel": a.vault,
           "guardians": idx, "kinds": kinds, "sigHash": dv.h(sh),
           "declaredVerifyLimits": [f.execution for f in tx.frames if f.mode == 1], "sidecar": net.cfg["sidecar"],
           "simulation": {k: sim.get(k) for k in ("valid", "prefixShape", "canonicalMempoolViolation",
                                                  "executionStatus", "gasUsed", "frames", "executionError")}}
    try:
        h = rpc.eth("eth_sendRawTransaction", [dv.h(tx.raw())])
        rec["canonicalMempool"] = {"admitted": True, "result": h}
    except rpc.RpcError as e:
        rec["canonicalMempool"] = {"admitted": False, "error": e.error}
    try:
        h = rpc.sidecar("eth_sendFrameTransaction", [tx.to_wire()])
        rec["altMempool"] = {"admitted": True, "txHash": h}
    except rpc.RpcError as e:
        rec["altMempool"] = {"admitted": False, "error": e.error}
    if rec["altMempool"]["admitted"]:
        end = time.time() + 180
        while time.time() < end:
            rc = rpc.eth("eth_getTransactionReceipt", [rec["altMempool"]["txHash"]])
            if rc:
                rec["receipt"] = {"status": rc["status"], "block": int(rc["blockNumber"], 16),
                                  "gasUsed": int(rc["gasUsed"], 16),
                                  "frames": [(f.get("status"), int(f.get("gasUsed", "0x0"), 16)) for f in rc.get("frameReceipts", [])]}
                dv.record_receipt(net, f"{a.tag}: transfer {dv._plus(idx, stateless)}",
                                  {"txHash": rec["altMempool"]["txHash"], "receipt": rc, "submittedVia": net.cfg["sidecar"]},
                                  {"sigHash": rec["sigHash"], "guardians": idx, "kinds": kinds, "vault": vault})
                break
            time.sleep(2)
    out = net.load("mempool-admission.json", [])
    out.append(rec)
    net.save("mempool-admission.json", out)
    print(json.dumps(rec, indent=1, default=str))


if __name__ == "__main__":
    main()
