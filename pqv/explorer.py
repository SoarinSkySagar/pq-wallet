"""Leafguard block explorer for the local EIP-8141 devnet (stdlib only; binds 127.0.0.1).

    python -m pqv.explorer [--network local] [--port 8300]

Standard explorers do not understand EIP-8141 frame transactions (type 0x06). This one shows every block and
transaction, and for frame transactions each frame (mode, approval scope, target, gas limit and gas used, status,
the decoded call and the logs it emitted) and each signature entry (for Leafguard: which guardian, compact one-time
leaf or stateless SPX, slot and leaf index). Known addresses are named from the local devnet state (contracts,
vaults, wallet accounts). A background thread indexes the chain so address pages can list their transactions.

    GET /api/overview           head, latest blocks and transactions
    GET /api/block/<n|hash>     one block and its transactions
    GET /api/tx/<hash>          one transaction, decoded frame by frame
    GET /api/address/<addr>     balance, nonce, code, name, transactions
    GET /api/search?q=          resolve a block number, tx hash or address
"""
import argparse
import glob
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import devnet as dv
from . import rpc
from .hashing import ROOT, keccak256

APP = os.path.join(ROOT, "app", "explorer")
EIP7708 = "0xfffffffffffffffffffffffffffffffffffffffe"
ENTRY_POINT = "0x00000000000000000000000000000000000000aa"
NET = None
IDX = {"head": -1, "blocks": {}, "txs": [], "addr": {}, "lock": threading.Lock()}

MODES = {0: "DEFAULT", 1: "VERIFY", 2: "SENDER"}
SCOPES = {0: "no approval", 1: "approves payment (pay)", 2: "approves execution (only_verify)",
          3: "approves execution and payment (self_verify)"}


def _sel(sig):
    return "0x" + keccak256(sig.encode())[:4].hex()


def _topic(sig):
    return "0x" + keccak256(sig.encode()).hex()


FUNCS = {_sel(s): s for s in [
    "validate()", "validateAndCommit()", "execute((address,uint256,bytes)[])", "runBatch((address,uint256,bytes)[])",
    "registerSlot(uint8,bytes16,bytes16)", "revokeSlot(bytes32)",
    "replaceGuardian(uint8,(bytes32,bytes32),bytes16,bytes16)", "unlockStake()", "withdrawStake(address)",
    "addStake(uint32)", "consume(bytes32,uint256)",
    "reportEquivocation(bytes16,bytes16,bytes32,bytes,bytes32,bytes)",
    "deploy(bytes32,((bytes32,bytes32)[3],bytes16[3],bytes16[3],uint256,uint32),bool,uint256)",
]}
# (signature, indexed argument names, data argument types and names)
EVENTS = {_topic(s): (s, idx, data) for s, idx, data in [
    ("LeafConsumed(bytes32,uint256,address)", ["slotId", "vault"], [("uint256", "leaf")]),
    ("SlotRevokedForEquivocation(bytes32,uint256,address)", ["slotId"], [("uint256", "leaf"), ("address", "reporter")]),
    ("Executed(bytes32,uint8,uint8,uint256)", ["sigHash"], [("uint8", "guardianA"), ("uint8", "guardianB"), ("uint256", "calls")]),
    ("BatchFailed(bytes32,bytes)", ["sigHash"], [("bytes", "reason")]),
    ("LeafRaced(bytes32,bytes32,uint256)", ["sigHash", "slotId"], [("uint256", "leaf")]),
    ("SlotRegistered(uint8,bytes32,uint256)", ["guardian", "slotId"], [("uint256", "epoch")]),
    ("SlotRevoked(bytes32)", ["slotId"], []),
    ("GuardianReplaced(uint8,uint256)", ["guardian"], [("uint256", "newEpoch")]),
    ("VaultDeployed(address,bytes32,bool)", ["vault"], [("bytes32", "salt"), ("bool", "leafLedger")]),
    ("Transfer(address,address,uint256)", ["from", "to"], [("uint256", "value")]),
]}


def _h(x):
    return int(x, 16) if isinstance(x, str) and x.startswith("0x") else (x or 0)


# ----------------------------------------------------------------------------- names
_LCACHE = {"t": 0, "names": {}, "guardians": {}}


def names():
    """address -> human name, from the local devnet state (refreshed every 10 s)."""
    if time.time() - _LCACHE["t"] < 10:
        return _LCACHE["names"]
    n, gname = {EIP7708: "ETH transfer log (EIP-7708)", ENTRY_POINT: "Frame entry point (0xaa)"}, {}
    d = NET.deployment
    for k, label in (("frameOps", "PQVaultFrameOps"), ("forsVerifier", "JARDIN FORS verifier"),
                     ("spxVerifier", "JARDIN SPX verifier"), ("staking", "Staking registry (ERC-8421)"),
                     ("factory", "PQVaultFactory"), ("ledger", "LeafLedger"), ("vault", "Leafguard (admin, 2-of-3)"),
                     ("baselineVault", "Baseline vault (no leaf ledger)"), ("unstakedVault", "Unstaked vault (control)"),
                     ("smallVault", "Slot-refresh test vault")):
        if d.get(k):
            n[d[k].lower()] = label
    try:
        n[NET.deployer().lower()] = "Devnet deployer / faucet (ECDSA)"
    except Exception:
        pass
    for p in glob.glob(os.path.join(NET.dir, "wallets", "*", "deployment.json")):
        v = json.load(open(p)).get("vault")
        if v:
            n[v.lower()] = f"{os.path.basename(os.path.dirname(p))}'s account"
    for p in glob.glob(os.path.join(NET.dir, "vaults", "*", "vault.json")):
        v = json.load(open(p))
        if v.get("address"):
            n[v["address"].lower()] = f"{v['name']} (multisig)"
            gname[v["address"].lower()] = {m["index"]: m["wallet"] for m in v["members"]}
    _LCACHE.update(t=time.time(), names=n, guardians=gname)
    return n


def name(a):
    return names().get((a or "").lower())


# ----------------------------------------------------------------------------- decoding
def _words(data):
    return [data[i:i + 32] for i in range(0, len(data), 32)]


def decode_execute(data):
    """execute((address,uint256,bytes)[]) calldata -> list of {to, value, data, call}."""
    b = bytes.fromhex(data[2:])[4:]
    try:
        base = int.from_bytes(b[0:32], "big")
        n = int.from_bytes(b[base:base + 32], "big")
        arr = base + 32
        out = []
        for i in range(n):
            off = arr + int.from_bytes(b[arr + 32 * i: arr + 32 * i + 32], "big")
            to = "0x" + b[off + 12: off + 32].hex()
            value = int.from_bytes(b[off + 32: off + 64], "big")
            boff = off + int.from_bytes(b[off + 64: off + 96], "big")
            blen = int.from_bytes(b[boff: boff + 32], "big")
            inner = "0x" + b[boff + 32: boff + 32 + blen].hex()
            out.append({"to": to, "toName": name(to), "valueWei": str(value), "data": inner,
                        "call": FUNCS.get(inner[:10]) if len(inner) >= 10 else ("ETH transfer" if blen == 0 else None)})
        return out
    except Exception:
        return None


def decode_log(l):
    t0 = l["topics"][0] if l.get("topics") else None
    ev = EVENTS.get(t0)
    out = {"address": l["address"], "addressName": name(l["address"]), "topics": l.get("topics", []), "data": l.get("data")}
    if not ev:
        return out
    sig, idx, data = ev
    args = {}
    for k, t in zip(idx, l["topics"][1:]):
        args[k] = "0x" + t[-40:] if k in ("vault", "from", "to") else (str(int(t, 16)) if k == "guardian" else t)
    w = _words(bytes.fromhex(l["data"][2:]))
    for i, (typ, k) in enumerate(data):
        if i >= len(w):
            break
        if typ in ("uint256", "uint8"):
            args[k] = str(int.from_bytes(w[i], "big"))
        elif typ == "address":
            args[k] = "0x" + w[i][-20:].hex()
        elif typ == "bool":
            args[k] = bool(int.from_bytes(w[i], "big"))
        elif typ == "bytes32":
            args[k] = "0x" + w[i].hex()
        else:
            args[k] = "(" + typ + ")"
    for k in ("from", "to", "vault", "reporter"):
        if k in args:
            args[k + "Name"] = name(args[k])
    out.update(event=sig.split("(")[0], signature=sig, args=args)
    return out


def decode_entry(sig, sender):
    """A Leafguard ARBITRARY signature entry: guardian, kind, slot, leaf."""
    raw = bytes.fromhex(sig["signature"][2:]) if sig.get("signature") else b""
    out = {"scheme": {0: "ARBITRARY", 1: "SECP256K1", 2: "P256"}.get(_h(sig["scheme"]), sig["scheme"]),
           "bytes": len(raw), "msg": sig.get("msg")}
    if _h(sig["scheme"]) != 0 or len(raw) < 2:
        return out
    g, kind = raw[0], raw[1]
    names()
    who = _LCACHE["guardians"].get((sender or "").lower(), {}).get(g)
    out.update(guardian=g, guardianName=who)
    if kind == 2 and len(raw) >= 34 + 2593 + 32:
        out.update(kind="compact JARDIN FORS (one-time leaf)", slotId="0x" + keccak256(raw[2:34]).hex(),
                   leaf=raw[34 + 2592], slotLeaves=1 << ((len(raw) - 34 - 2593) // 16))
    elif kind == 1:
        out.update(kind="stateless JARDIN SPX")
    else:
        out.update(kind=f"unknown ({kind})")
    return out


def frame_purpose(f, i, sender):
    mode, scope, data = _h(f["mode"]), _h(f["flags"]) & 3, f.get("data") or "0x"
    fn = FUNCS.get(data[:10]) if len(data) >= 10 else None
    target = f.get("to") or sender
    if mode == 1 and fn == "validate()":
        return f"Verifies signature entry {i} (a guardian's approval) against the vault, then {SCOPES[scope].split(' (')[0]}."
    if mode == 1 and fn == "validateAndCommit()":
        return "Verifies the first guardian's approval and its commitment to the others, then approves execution and payment."
    if mode == 2 and fn and fn.startswith("execute("):
        return "Runs as the vault: marks the approving leaves used in the LeafLedger, then executes the calls."
    if mode == 1:
        return f"Validation frame: {SCOPES.get(scope, scope)}."
    if mode == 2:
        return "Executes as the sender."
    return "Ordinary call from the entry point."


def decode_tx(t, rc):
    out = {"hash": t["hash"], "type": t.get("type"), "block": _h(t.get("blockNumber")), "index": _h(t.get("transactionIndex")),
           "from": t.get("from"), "fromName": name(t.get("from")), "status": rc.get("status") if rc else None,
           "gasUsed": _h(rc.get("gasUsed")) if rc else None, "effectiveGasPrice": _h(rc.get("effectiveGasPrice")) if rc else None,
           "logs": [decode_log(l) for l in (rc or {}).get("logs", [])]}
    if out["gasUsed"] is not None and out["effectiveGasPrice"] is not None:
        out["feeWei"] = str(out["gasUsed"] * out["effectiveGasPrice"])
    if t.get("type") == "0x6":
        sender = t.get("sender")
        out.update(kind="Frame transaction (EIP-8141)", sender=sender, senderName=name(sender),
                   payer=(rc or {}).get("payer"), payerName=name((rc or {}).get("payer")),
                   nonce=_h(t.get("nonceSeq")), nonceKeys=t.get("nonceKeys"),
                   maxFeePerGas=_h(t.get("maxFeePerGas")), maxPriorityFeePerGas=_h(t.get("maxPriorityFeePerGas")))
        frs = (rc or {}).get("frameReceipts") or []
        frames = []
        for i, f in enumerate(t.get("frames", [])):
            r = frs[i] if i < len(frs) else {}
            target = f.get("to") or sender
            data = f.get("data") or "0x"
            fn = FUNCS.get(data[:10]) if len(data) >= 10 else None
            fr = {"index": i, "mode": MODES.get(_h(f["mode"]), f["mode"]), "scope": SCOPES.get(_h(f["flags"]) & 3),
                  "flags": _h(f["flags"]), "atomicBatch": bool(_h(f["flags"]) & 4), "target": target,
                  "targetIsSender": not f.get("to"), "targetName": name(target), "gasLimit": _h(f.get("gasLimit")),
                  "stateLimit": _h(f.get("stateLimit")), "valueWei": str(_h(f.get("value"))), "data": data,
                  "dataBytes": max(0, (len(data) - 2) // 2), "call": fn, "status": r.get("status"),
                  "gasUsed": _h(r.get("gasUsed")) if r else None, "stateGasUsed": _h(r.get("stateGasUsed")) if r else None,
                  "logs": [decode_log(l) for l in r.get("logs", [])], "purpose": frame_purpose(f, i, sender)}
            if fn and fn.startswith("execute("):
                fr["calls"] = decode_execute(data)
            frames.append(fr)
        out["frames"] = frames
        out["signatures"] = [decode_entry(s, sender) | {"index": i} for i, s in enumerate(t.get("signatures", []))]
        out["sigBytes"] = sum(e["bytes"] for e in out["signatures"])
    else:
        to = t.get("to")
        inp = t.get("input") or t.get("data") or "0x"
        out.update(kind={"0x0": "Legacy", "0x2": "EIP-1559", "0x1": "EIP-2930", "0x4": "EIP-7702"}.get(t.get("type"), t.get("type")),
                   to=to, toName=name(to), contractCreated=(rc or {}).get("contractAddress"),
                   contractCreatedName=name((rc or {}).get("contractAddress")), valueWei=str(_h(t.get("value"))),
                   nonce=_h(t.get("nonce")), input=inp[:2 + 2 * 4096], inputBytes=max(0, (len(inp) - 2) // 2),
                   call=FUNCS.get(inp[:10]) if len(inp) >= 10 and to else ("contract creation" if not to else None),
                   gasPrice=_h(t.get("gasPrice")))
    return out


def tx_summary(t, rc=None):
    s = {"hash": t["hash"], "block": _h(t.get("blockNumber")), "type": t.get("type"),
         "from": t.get("sender") or t.get("from"), "status": (rc or {}).get("status")}
    s["fromName"] = name(s["from"])
    if t.get("type") == "0x6":
        s.update(frames=len(t.get("frames", [])), to=None,
                 shape="+".join(MODES.get(_h(f["mode"]), "?") for f in t.get("frames", [])))
        calls = None
        for f in t.get("frames", []):
            d = f.get("data") or "0x"
            if FUNCS.get(d[:10], "").startswith("execute("):
                calls = decode_execute(d)
        if calls:
            s["to"], s["valueWei"] = calls[0]["to"], str(sum(int(c["valueWei"]) for c in calls))
            s["what"] = calls[0]["call"] if len(calls) == 1 else f"{len(calls)} calls"
    else:
        s.update(to=t.get("to"), valueWei=str(_h(t.get("value"))))
        inp = t.get("input") or "0x"
        s["what"] = FUNCS.get(inp[:10]) or ("contract creation" if not t.get("to") else "ETH transfer" if inp == "0x" else "call")
    s["toName"] = name(s.get("to"))
    if s["what"] if "what" in s else None:
        s["what"] = s["what"].split("(")[0]
    return s


# ----------------------------------------------------------------------------- indexer
def _index_block(n):
    b = rpc.eth("eth_getBlockByNumber", [hex(n), True])
    if not b:
        return False
    txs = []
    for t in b["transactions"]:
        rc = rpc.eth("eth_getTransactionReceipt", [t["hash"]])
        s = tx_summary(t, rc)
        txs.append(s)
        touched = {s["from"], s.get("to"), (rc or {}).get("contractAddress"), (rc or {}).get("payer")}
        for f in t.get("frames", []):
            touched.add(f.get("to"))
        for l in (rc or {}).get("logs", []):
            if l["address"].lower() == EIP7708 and len(l["topics"]) == 3:
                touched |= {"0x" + l["topics"][1][-40:], "0x" + l["topics"][2][-40:]}
        with IDX["lock"]:
            for a in touched:
                if a:
                    IDX["addr"].setdefault(a.lower(), []).append(t["hash"])
    with IDX["lock"]:
        IDX["blocks"][n] = {"number": n, "hash": b["hash"], "timestamp": _h(b["timestamp"]), "txs": len(txs),
                            "gasUsed": _h(b["gasUsed"]), "gasLimit": _h(b["gasLimit"]), "baseFee": _h(b.get("baseFeePerGas"))}
        IDX["txs"].extend(txs)
        IDX["head"] = n
    return True


def indexer():
    while True:
        try:
            head = int(rpc.eth("eth_blockNumber"), 16)
            if head < IDX["head"]:  # chain restarted: start over
                with IDX["lock"]:
                    IDX.update(head=-1, blocks={}, txs=[], addr={})
            n = IDX["head"] + 1
            while n <= head:
                _index_block(n)
                n += 1
        except Exception as e:
            print("indexer:", str(e)[:200])
        time.sleep(2)


# ----------------------------------------------------------------------------- API
def overview():
    with IDX["lock"]:
        head = IDX["head"]
        blocks = [IDX["blocks"][n] for n in range(head, max(-1, head - 20), -1) if n in IDX["blocks"]]
        txs = list(reversed(IDX["txs"][-25:]))
        ntx = len(IDX["txs"])
    try:
        chain = int(rpc.eth("eth_chainId"), 16)
        client = rpc.eth("web3_clientVersion")
    except Exception:
        chain, client = None, None
    return {"head": head, "chainId": chain, "client": client, "rpc": NET.cfg["ethrex"], "blocks": blocks, "txs": txs,
            "txCount": ntx, "frameTxCount": sum(1 for t in IDX["txs"] if t.get("type") == "0x6")}


def block(ident):
    b = rpc.eth("eth_getBlockByHash" if len(ident) == 66 else "eth_getBlockByNumber",
                [ident if len(ident) == 66 else hex(int(ident)), True])
    if not b:
        raise LookupError("No such block.")
    txs = [tx_summary(t, rpc.eth("eth_getTransactionReceipt", [t["hash"]])) for t in b["transactions"]]
    keep = ("hash", "parentHash", "miner", "stateRoot", "gasLimit", "gasUsed", "baseFeePerGas", "timestamp", "size",
            "slotNumber", "blockAccessListHash", "withdrawals")
    out = {k: b.get(k) for k in keep}
    out.update(number=_h(b["number"]), timestamp=_h(b["timestamp"]), gasUsed=_h(b["gasUsed"]), gasLimit=_h(b["gasLimit"]),
               baseFeePerGas=_h(b.get("baseFeePerGas")), size=_h(b.get("size")), txs=txs,
               withdrawals=len(b.get("withdrawals") or []))
    return out


def tx(h):
    t = rpc.eth("eth_getTransactionByHash", [h])
    if not t:
        raise LookupError("No such transaction (it may still be pending in the mempool).")
    return decode_tx(t, rpc.eth("eth_getTransactionReceipt", [h]))


def address(a):
    a = a.lower()
    code = rpc.eth("eth_getCode", [a, "latest"])
    with IDX["lock"]:
        hashes = list(IDX["addr"].get(a, []))
        by_hash = {t["hash"]: t for t in IDX["txs"]}
    seen, txs = set(), []
    for h in reversed(hashes):
        if h not in seen and h in by_hash:
            seen.add(h)
            txs.append(by_hash[h])
    return {"address": a, "name": name(a), "balanceWei": str(int(rpc.eth("eth_getBalance", [a, "latest"]), 16)),
            "nonce": int(rpc.eth("eth_getTransactionCount", [a, "latest"]), 16),
            "codeBytes": max(0, (len(code) - 2) // 2), "txs": txs[:100], "txTotal": len(txs)}


def search(q):
    q = q.strip()
    if re.fullmatch(r"\d+", q):
        return {"route": f"block/{int(q)}"}
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", q):
        return {"route": f"address/{q.lower()}"}
    if re.fullmatch(r"0x[0-9a-fA-F]{64}", q):
        if rpc.eth("eth_getTransactionByHash", [q]):
            return {"route": f"tx/{q.lower()}"}
        if rpc.eth("eth_getBlockByHash", [q, False]):
            return {"route": f"block/{q.lower()}"}
        raise LookupError("No transaction or block has that hash.")
    low = q.lower()
    for addr, n in names().items():
        if low and low in n.lower():
            return {"route": f"address/{addr}"}
    raise LookupError("Search by block number, transaction hash, address, or a name such as alice or LeafLedger.")


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path, _, query = self.path.partition("?")
        try:
            if path == "/api/overview":
                return self._send(200, overview())
            m = re.fullmatch(r"/api/block/(\d+|0x[0-9a-fA-F]{64})", path)
            if m:
                return self._send(200, block(m[1]))
            m = re.fullmatch(r"/api/tx/(0x[0-9a-fA-F]{64})", path)
            if m:
                return self._send(200, tx(m[1]))
            m = re.fullmatch(r"/api/address/(0x[0-9a-fA-F]{40})", path)
            if m:
                return self._send(200, address(m[1]))
            if path == "/api/search":
                from urllib.parse import parse_qs
                return self._send(200, search(parse_qs(query).get("q", [""])[0]))
            f = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            p = os.path.realpath(os.path.join(APP, f))
            if not p.startswith(os.path.realpath(APP)) or not os.path.isfile(p):
                return self._send(404, {"error": "not found"})
            return self._send(200, open(p, "rb").read(), "text/html; charset=utf-8")
        except LookupError as e:
            return self._send(404, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)[:300]})


def main():
    global NET
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="local", choices=["local", "public"])
    ap.add_argument("--port", type=int, default=8300)
    a = ap.parse_args()
    NET = dv.Net(a.network)
    threading.Thread(target=indexer, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"Leafguard explorer on http://127.0.0.1:{a.port}  (node: {NET.cfg['ethrex']})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
