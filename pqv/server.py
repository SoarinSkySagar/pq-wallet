"""Leafguard wallet server (stdlib only; binds 127.0.0.1).

    python -m pqv.server <network> [--port 8141]

Serves the wallet web app (app/) and a JSON API. Guardian keys never leave this process: the browser only sees
public data and asks for operations; signing happens here. One guardian operation runs at a time (a lock),
because guardian leaf state must never be used concurrently. Operations run as background jobs whose progress
the page polls.

    GET  /api/wallet            vault, balance, network, mempool, guardian key health
    GET  /api/activity          the vault's transactions (newest first)
    POST /api/plan              {approvers}            what a send would do (no signing)
    POST /api/send              {to, wei, approvers}   -> {job}
    POST /api/rotate            {guardian}             -> {job}
    POST /api/recover           {lost}                 -> {job}
    POST /api/demo/stale-restore {guardian}            simulation only (nothing is broadcast)
    GET  /api/jobs/<id>         {state, events, result | error}
"""
import argparse
import contextlib
import io
import json
import os
import re
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import devnet as dv
from . import rpc
from .hashing import ROOT
from .state import DurableLeafStore

APP = os.path.join(ROOT, "app")
LOCK = threading.Lock()
NET = None
JOBS = {}
_GCACHE = {"mtime": None, "gs": None}
ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
REVOKED = 2**255


def cached_guardians():
    """Guardian objects (JARDIN slot trees) are expensive to build; rebuild only when guardians.json changes."""
    m = os.path.getmtime(NET.path("guardians.json"))
    if _GCACHE["mtime"] != m:
        _GCACHE["gs"], _GCACHE["mtime"] = dv.guardians(NET), m
    return _GCACHE["gs"]


def _mempool_kind():
    """Which sidecar answers on the configured mempool URL (local network only; best effort)."""
    try:
        import subprocess
        img = subprocess.run(["docker", "inspect", "pqvault-sidecar", "--format", "{{.Config.Image}}"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if "budget-010" in img:
            return "ERC-8421 alt mempool with per-entity budgets (BUDGET-010)"
        if img:
            return "ERC-8421 alt mempool, stock rules (combined 100k cap: rejects this vault)"
    except Exception:
        pass
    return "ERC-8421 alt mempool"


def wallet():
    net, dep = NET, NET.deployment
    vault = dep.get("vault")
    out = {"network": net.name, "chainId": int(rpc.eth("eth_chainId"), 16), "block": int(rpc.eth("eth_blockNumber"), 16),
           "mempool": {"url": net.cfg["sidecar"], "kind": _mempool_kind()}, "rotateAt": dv.ROTATE_AT,
           "vault": None, "guardians": []}
    if vault:
        out["vault"] = {"address": vault, "balanceWei": str(int(rpc.eth("eth_getBalance", [vault, "latest"]), 16)),
                        "nonce": int(rpc.eth("eth_getTransactionCount", [vault, "latest"]), 16),
                        "ledger": dep.get("ledger")}
    if net.load("guardians.json"):
        for g in cached_guardians():
            slot = g.slot()
            mark = net.ledger_hwm(slot.slot_id)
            info = net.call(vault, dv.sel("slotGuardian(bytes32)") + slot.slot_id) if vault else b""
            remaining = g.remaining_leaves()
            out["guardians"].append({
                "index": g.i, "keyNumber": len(g.rec["slots"]), "slotId": dv.h(slot.slot_id), "leaves": slot.q_max,
                "remaining": remaining, "used": slot.q_max - remaining, "chainMark": None if mark >= REVOKED else mark,
                "revoked": mark >= REVOKED, "active": bool(info and info[31]), "replaced": g.rec.get("replaced", 0),
                "renewDue": dv.rotation_due(remaining, slot.q_max, dv.ROTATE_AT)})
    return out


def activity():
    vault = (NET.deployment.get("vault") or "").lower()
    rows = []
    for r in reversed(NET.load("receipts.json", [])):
        if (r.get("vault") or "").lower() != vault:
            continue
        frames = [{"status": f.get("status"), "gas": int(f["gasUsed"], 16) if isinstance(f.get("gasUsed"), str) else f.get("gasUsed")}
                  for f in (r.get("frameReceipts") or [])]
        rows.append({"label": r["label"], "txHash": r["txHash"], "block": r["block"], "status": r["status"],
                     "gasUsed": r["gasUsed"], "guardians": r.get("guardians"), "kinds": r.get("kinds"),
                     "frames": frames, "time": r.get("time")})
    return rows


def plan(approvers):
    """What a send with these two guardians would do, without signing anything."""
    gs = cached_guardians()
    approvers = sorted(set(int(x) for x in approvers))
    if len(approvers) != 2 or not all(0 <= a <= 2 for a in approvers):
        raise ValueError("choose exactly two different guardians")
    steps, kinds = [], []
    for a in approvers:
        g = gs[a]
        rem, q = g.remaining_leaves(), g.slot().q_max
        renew = dv.rotation_due(rem, q, dv.ROTATE_AT)
        if renew:
            steps.append({"type": "renew", "guardian": a, "remaining": rem,
                          "approvers": [x for x in range(3) if x != a]})
        kinds.append("compact" if renew or rem > 0 else "stateless")
    frames = [{"name": "VERIFY", "guardian": approvers[0], "kind": kinds[0], "gasLimit": dv.approval_gas(kinds[0]),
               "does": "checks guardian %d's signature and that its leaf is unused, then approves execution" % approvers[0]},
              {"name": "VERIFY", "guardian": approvers[1], "kind": kinds[1], "gasLimit": dv.approval_gas(kinds[1]),
               "does": "checks guardian %d's signature, then approves payment from the vault" % approvers[1]},
              {"name": "SENDER", "guardian": None, "kind": None, "gasLimit": 900_000,
               "does": "marks both leaves used and sends the funds"}]
    return {"approvers": approvers, "renewFirst": steps, "frames": frames,
            "validationGas": frames[0]["gasLimit"] + frames[1]["gasLimit"] + 200}


# ----------------------------------------------------------------------------- jobs
class _Lines(io.TextIOBase):
    def __init__(self, job):
        self.job, self.buf = job, ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.job["events"].append(_event(line))
        return len(s)


def _event(line):
    """Turn a driver log line into a structured progress event."""
    m = re.match(r"^(.*): tx (0x[0-9a-f]{64}) status (0x[0-9a-f]+) block (\d+) gasUsed (\d+)$", line)
    if m:
        return {"type": "included", "label": m[1], "txHash": m[2], "status": m[3], "block": int(m[4]), "gasUsed": int(m[5])}
    if line.startswith("simulate: "):
        try:
            sim = json.loads(line[len("simulate: "):])
            return {"type": "simulated", "ok": sim.get("executionStatus") == "success",
                    "frames": [int(f["gasUsed"], 16) for f in (sim.get("frames") or [])]}
        except ValueError:
            pass
    for prefix, kind in (("signing: ", "signing"), ("submitting: ", "submitting"), ("slot refresh: ", "renew"),
                         ("stateless fallback: ", "fallback")):
        if line.startswith(prefix):
            return {"type": kind, "text": line[len(prefix):]}
    return {"type": "log", "text": line}


def start_job(kind, fn):
    if not LOCK.acquire(blocking=False):
        raise BlockingIOError("another guardian operation is running; wait for it to finish")
    job = {"id": uuid.uuid4().hex[:12], "kind": kind, "state": "running", "events": [], "started": time.time()}
    JOBS[job["id"]] = job

    def run():
        try:
            with contextlib.redirect_stdout(_Lines(job)):
                res = fn()
            job["result"] = {k: v for k, v in (res or {}).items() if k in ("txHash", "kinds", "guardians")}
            if isinstance(res, dict) and res.get("receipt"):
                rc = res["receipt"]
                job["result"].update({"status": rc["status"], "block": int(rc["blockNumber"], 16),
                                      "gasUsed": int(rc["gasUsed"], 16)})
            job["state"] = "done"
        except Exception as e:
            traceback.print_exc()
            job["state"], job["error"] = "error", _friendly(e)
        finally:
            job["finished"] = time.time()
            LOCK.release()

    threading.Thread(target=run, daemon=True).start()
    return job


def _friendly(e):
    s = str(e)
    if "insufficient" in s.lower() or "underflow" in s.lower():
        return "The vault does not hold enough ETH for this amount plus the fee."
    if "simulated execution failed" in s:
        return "The transaction failed in simulation, so it was not sent. " + s[:400]
    if "not mined" in s:
        return "The mempool accepted the transaction but it was not included within 3 minutes. Send it again."
    if "code" in s and "message" in s:
        return "The mempool rejected the transaction: " + s[:400]
    return s[:500]


def demo_stale_restore(g_idx: int):
    """Live rollback attack on our own devnet test wallet: guardian g's device is 'restored from an old
    backup' (leaf counter reset to 1). (a) The Leafguard signer (AnchoredLeafStore) resumes above the
    chain-anchored mark, so no leaf is reused. (b) A baseline JARDIN-style signer (DurableLeafStore,
    no anchoring) re-signs a consumed leaf; ethrex simulation shows the vault's VERIFY frame rejecting it
    (LeafLedger StaleLeaf), so it can never be included. Nothing is broadcast in (b)."""
    net = NET
    gs = dv.guardians(net)
    g = gs[g_idx]
    slot = g.slot()
    hwm = net.ledger_hwm(slot.slot_id)
    if hwm == 0:
        raise RuntimeError("send at least one transaction with this guardian first")
    stale_path = net.path(f"guardian-{g_idx}-leaves.stale-backup.json")
    dv_state = DurableLeafStore(stale_path)  # a freshly 'restored' backup: next_q = 1
    vault = net.deployment["vault"]
    approvers = sorted([g_idx, (g_idx + 1) % 3])
    tx = dv.build_vault_tx(net, vault, [(net.deployer(), 1, "0x")], ["compact", "compact"])
    q_stale = dv_state.reserve(slot.slot_id, slot.q_max)
    dv.sign_entries(tx, [(lambda m: dv.compact_entry(g_idx, slot, slot.sign(m, q_stale))) if a == g_idx
                         else dv.entry_signer(gs[a], "compact") for a in approvers])
    sim = dv.simulate(tx)
    anchored_q = max(1, hwm + 1)
    os.unlink(stale_path)
    return {"guardian": g_idx, "chainHighWaterMark": hwm,
            "baselineSigner": {"leafChosen": q_stale, "reusesConsumedLeaf": q_stale <= hwm,
                               "rejected": sim.get("executionStatus") != "success",
                               "reason": (sim.get("executionError") or "")[:300]},
            "pqVaultSigner": {"leafChosen": anchored_q, "reusesConsumedLeaf": False}}


# ----------------------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        try:
            if self.path == "/api/wallet":
                return self._send(200, wallet())
            if self.path == "/api/activity":
                return self._send(200, activity())
            if self.path.startswith("/api/jobs/"):
                job = JOBS.get(self.path.rsplit("/", 1)[-1])
                return self._send(200, job) if job else self._send(404, {"error": "unknown job"})
            f = "index.html" if self.path in ("/", "/index.html") else self.path.split("?")[0].lstrip("/")
            p = os.path.realpath(os.path.join(APP, f))
            if not p.startswith(os.path.realpath(APP)) or not os.path.isfile(p):
                return self._send(404, {"error": "not found"})
            ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css", "svg": "image/svg+xml"}.get(
                p.rsplit(".", 1)[-1], "text/plain")
            return self._send(200, open(p, "rb").read(), ctype + "; charset=utf-8")
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/plan":
                return self._send(200, plan(body.get("approvers", [])))
            if self.path == "/api/send":
                to, wei = str(body.get("to", "")), int(body.get("wei", 0))
                approvers = tuple(sorted(set(int(x) for x in body.get("approvers", []))))
                if not ADDR.match(to):
                    return self._send(400, {"error": "Enter a 0x address with 40 hex characters."})
                if wei <= 0:
                    return self._send(400, {"error": "Enter an amount above zero."})
                if len(approvers) != 2:
                    return self._send(400, {"error": "Choose exactly two guardians to approve."})
                job = start_job("send", lambda: dv.cmd_transfer(NET, to, wei, approvers, gs=cached_guardians()))
            elif self.path == "/api/rotate":
                g = int(body["guardian"])
                job = start_job("rotate", lambda: dv.cmd_rotate(NET, g, gs=cached_guardians()))
            elif self.path == "/api/recover":
                g = int(body["lost"])
                job = start_job("recover", lambda: dv.cmd_recover(NET, g))
            elif self.path == "/api/demo/stale-restore":
                g = int(body["guardian"])
                if not LOCK.acquire(blocking=False):
                    return self._send(409, {"error": "another guardian operation is running"})
                try:
                    return self._send(200, demo_stale_restore(g))
                finally:
                    LOCK.release()
            else:
                return self._send(404, {"error": "unknown endpoint"})
            return self._send(202, {"job": job["id"]})
        except BlockingIOError as e:
            return self._send(409, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(400, {"error": _friendly(e)})


def main():
    global NET
    ap = argparse.ArgumentParser()
    ap.add_argument("network", choices=["public", "local"])
    ap.add_argument("--port", type=int, default=8141)
    a = ap.parse_args()
    NET = dv.Net(a.network)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"Leafguard wallet on http://127.0.0.1:{a.port}  (network: {a.network})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
