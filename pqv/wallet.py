"""A personal Leafguard wallet (MetaMask-style), one per process, for demos with several users.

    python -m pqv.wallet <name> [--port 8201] [--network local] [--lan [--lan-port 9201]]
    infra/wallets.sh start [--lan] alice bob carol      # several wallets, one port each

The page (app/wallet/) is an installable mobile web app. With --lan a second listener serves it over HTTPS to phones
on the same network; its /api/ calls need the wallet's pairing code (pqv/lan.py). The phone app (mobile/) reaches the
loopback listener through a tunnel (ngrok, ./demo.sh tunnel); such forwarded requests need the code too (H._remote).
Keys never leave this process.

Each wallet is its own account: a 2-of-3 Leafguard smart account whose three JARDIN keys live on this computer
(.devnet/<network>/wallets/<name>/). It shares the network's contracts (factory, LeafLedger, verifiers) with every
other wallet. The browser page only sees public data; signing happens in this process. Test ETH comes from the
throwaway devnet deployer (the only ECDSA key in the project), which also pays for deploying the account.

    GET  /api/state               account, balance, network connection, keys, contacts
    GET  /api/activity            received and sent ETH (EIP-7708 transfer logs), newest first
    GET  /api/quote               fee estimate (send plan: renewals, key kinds, gas) and the most that can be sent
    POST /api/create              create this wallet's account (keys + smart account + test ETH)  -> {job}
    POST /api/faucet              1 test ETH from the devnet faucet                                 -> {job}
    POST /api/send {to, wei}      pay, signed by the two keys with the most leaves left             -> {job}
    POST /api/network {ethrex, sidecar}   connect to another node / mempool
    GET  /api/jobs/<id>
    GET  /api/pair                (loopback only) pairing code; LAN URL + QR if --lan; ngrok tunnel URL + QR if one runs

Shared 2-of-3 multisig vaults with other wallets (pqv/multisig.py):
    POST /api/vaults {name, guardians: [two other wallet names]}       create; invites the other two
    GET  /api/vaults/<id>                                             detail, proposals, activity
    POST /api/vaults/<id>/join                                        accept an invitation (makes this wallet's key)
    POST /api/vaults/<id>/deploy                                      once all three joined          -> {job}
    POST /api/vaults/<id>/propose {to, wei}                           propose + approve a payment
    POST /api/vaults/<id>/proposals/<pid>/approve                     second approval submits it     -> {job}
"""
import argparse
import contextlib
import fcntl
import glob
import json
import os
import re
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import devnet as dv
from . import lan
from . import multisig as ms
from . import rpc
from . import server as srv
from .hashing import ROOT, keccak256

APP = os.path.join(ROOT, "app", "wallet")
ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
EIP7708 = "0xfffffffffffffffffffffffffffffffffffffffe"  # ethrex emits a Transfer log here for every ETH move
TRANSFER = "0x" + keccak256(b"Transfer(address,address,uint256)").hex()
INFRA = ("staking", "frameOps", "forsVerifier", "spxVerifier", "factory", "ledger")
FAUCET_WEI = 10**18
CREATE_FUND_ETH, CREATE_STAKE_ETH = 1.0, 0.02
# Billed gas of a wallet payment, fitted to every frame tx on the local chain (26 receipts: transfers, rotations,
# compact and SPX approvals, slot heights 2 and 7; all reproduced exactly). These txs are billed at the calldata floor,
# which grows linearly with the signature bytes, plus state gas for each storage slot they create.
GAS_TWO_SPX = 862_713          # transfer approved by two stateless (SPX) signatures
GAS_COMPACT_SAVES_H2 = 246_720  # each compact approval in place of an SPX one, slot height 2 ...
GAS_COMPACT_PER_LEVEL = 1_024   # ... and 1,024 less per extra tree level (16 more bytes of auth path)
GAS_ROTATION_EXTRA = 22_528    # a rotation's two calls (registerSlot + revokeSlot) over a transfer's one
GAS_NEW_STORAGE = 97_920       # state gas per storage slot created: a slot's first leaf in the LeafLedger, a new slot
MAX_COST_RESERVE = 5 * 10**15  # the account must hold value + the tx's maximum cost when payment is approved
W = None
LAN = None                     # {"url", "code", "port"} when phones may connect (--lan)
GATE = None                    # pairing-code check for every request from a phone (LAN listener or a tunnel)
PORT = None                    # this wallet's loopback port (a tunnel such as ngrok forwards to it)
NGROK_APIS = [f"http://127.0.0.1:{p}/api/tunnels" for p in (4040, 4041, 4042)]  # ngrok agents' local API
ACCENTS = ["#2F6F5E", "#3B5BA9", "#7C4A9E", "#2C7DA0", "#6A7B2E", "#A04E68"]  # same as app/wallet/index.html
CTYPES = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8",
          "svg": "image/svg+xml", "png": "image/png", "webmanifest": "application/manifest+json"}


class WalletNet(dv.Net):
    """dv.Net whose files live in wallets/<name>/, on top of the network's shared contracts."""

    def __init__(self, network: str, name: str):
        super().__init__(network)
        self.shared_dir, self.wallet = self.dir, name
        self.dir = os.path.join(self.shared_dir, "wallets", name)
        os.makedirs(self.dir, exist_ok=True)
        own = self.load("network.json")
        if own:
            self.cfg.update(own)
        rpc.ETHREX_URL, rpc.SIDECAR_URL = self.cfg["ethrex"], self.cfg["sidecar"]

    @property
    def deployment(self):
        p = os.path.join(self.shared_dir, "deployment.json")
        shared = json.load(open(p)) if os.path.exists(p) else {}
        return {**self.load("deployment.json", {}), **{k: shared[k] for k in INFRA if k in shared}}


@contextlib.contextmanager
def deployer_lock():
    """Several wallet processes share the devnet deployer key: serialise its transactions (nonces)."""
    with open(os.path.join(dv.DEVNET_DIR, "deployer.lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ----------------------------------------------------------------------------- state
def account():
    """The account address if it exists on the connected chain, else None."""
    a = W.deployment.get("vault")
    if not a:
        return None
    try:
        return a if rpc.eth("eth_getCode", [a, "latest"]) not in ("0x", "0x0", None) else None
    except Exception:
        return None


def _guardians():
    return dv.guardians(W) if W.load("guardians.json") else []


def contacts():
    out = [{"name": v["name"], "address": v["address"], "vault": True} for v in ms.all_vaults(W) if v.get("address")]
    for p in sorted(glob.glob(os.path.join(W.shared_dir, "wallets", "*", "deployment.json"))):
        name = os.path.basename(os.path.dirname(p))
        if name == W.wallet:
            continue
        v = json.load(open(p)).get("vault")
        if v:
            out.append({"name": name, "address": v})
    return out


def vault_summary(v):
    me = next((m for m in v["members"] if m["wallet"] == W.wallet), None)
    out = {"id": v["id"], "name": v["name"], "status": v["status"], "address": v.get("address"), "creator": v["creator"],
           "members": [{"wallet": m["wallet"], "index": m["index"], "joined": bool(m["pub"]), "me": m["wallet"] == W.wallet}
                       for m in v["members"]],
           "invited": bool(me and not me["pub"]), "balanceWei": None, "pending": 0, "awaitingMe": 0}
    if v.get("address"):
        out["balanceWei"] = str(int(rpc.eth("eth_getBalance", [v["address"], "latest"]), 16))
        ms.expire_stale(W, v)
        for p in ms.proposals(W, v["id"]):
            if p["status"] in ("pending", "ready"):
                out["pending"] += 1
                if me and str(me["index"]) not in p["approvals"]:
                    out["awaitingMe"] += 1
    return out


def my_vaults():
    return [vault_summary(v) for v in ms.all_vaults(W) if any(m["wallet"] == W.wallet for m in v["members"])]


def vault_detail(vid):
    v = ms.load_vault(W, vid)
    out = vault_summary(v)
    me = next((m for m in v["members"] if m["wallet"] == W.wallet), None)
    out["myIndex"] = me["index"] if me else None
    out["proposals"] = [{k: p.get(k) for k in ("id", "proposer", "to", "wei", "status", "txHash", "block", "error",
                                                 "created", "submitter", "kinds")}
                        | {"approvals": [{"index": int(i), "wallet": a["wallet"], "kind": a["kind"]}
                                         for i, a in sorted(p["approvals"].items())]}
                        for p in ms.proposals(W, vid)]
    out["activity"] = activity(v["address"], v.get("createdBlock", 0)) if v.get("address") else []
    g = ms.my_guardian(W, v) if v.get("address") else None
    out["myKey"] = {"remaining": g.remaining_leaves(), "leaves": g.slot().q_max} if g else None
    return out


def state():
    out = {"name": W.wallet, "network": {"rpc": W.cfg["ethrex"], "mempool": W.cfg["sidecar"], "connected": False},
           "account": None, "keys": [], "contacts": contacts(), "rotateAt": dv.ROTATE_AT}
    try:
        out["network"].update({"chainId": int(rpc.eth("eth_chainId"), 16), "block": int(rpc.eth("eth_blockNumber"), 16),
                               "connected": True, "infra": bool(W.deployment.get("factory"))})
    except Exception as e:
        out["network"]["error"] = str(e)[:200]
        return out
    out["vaults"] = my_vaults()
    out["wallets"] = sorted(os.path.basename(d) for d in glob.glob(os.path.join(W.shared_dir, "wallets", "*"))
                            if os.path.isdir(d) and os.path.basename(d) != W.wallet)
    a = account()
    if a:
        out["account"] = {"address": a, "balanceWei": str(int(rpc.eth("eth_getBalance", [a, "latest"]), 16))}
        for g in _guardians():
            s = g.slot()
            out["keys"].append({"index": g.i, "leaves": s.q_max, "remaining": g.remaining_leaves(),
                                "renewals": len(g.rec["slots"]) - 1})
    elif W.deployment.get("vault"):
        out["staleAccount"] = W.deployment["vault"]  # created on a network that is gone (fresh chain)
    return out


def _names():
    n = {c["address"].lower(): c["name"] for c in contacts()}
    try:
        n[W.deployer().lower()] = "Test ETH faucet"
    except Exception:
        pass
    d = W.deployment
    for k, label in (("factory", "Account setup"), ("staking", "Mempool stake")):
        if d.get(k):
            n[d[k].lower()] = label
    if account():
        n[account().lower()] = W.wallet
    return n


def activity(a=None, created=None):
    a = a or account()
    if not a:
        return []
    pad = "0x" + "0" * 24 + a[2:].lower()
    start = hex(W.deployment.get("createdBlock", 0) if created is None else created)
    names, rows = _names(), []
    for direction, topics in (("in", [TRANSFER, None, pad]), ("out", [TRANSFER, pad])):
        for l in rpc.eth("eth_getLogs", [{"fromBlock": start, "toBlock": "latest", "address": EIP7708, "topics": topics}]):
            other = "0x" + l["topics"][2 if direction == "out" else 1][-40:]
            rows.append({"direction": direction, "counterparty": other, "name": names.get(other.lower()),
                         "wei": str(int(l["data"], 16)), "block": int(l["blockNumber"], 16), "txHash": l["transactionHash"]})
    rows.sort(key=lambda r: (-r["block"], r["direction"]))
    return rows


def frame_tx_gas(signers, new_storage=0, rotation=False):
    """Billed gas of one vault frame tx. signers: [("compact", slot_height) | ("stateless", None)] x 2."""
    g = GAS_TWO_SPX + (GAS_ROTATION_EXTRA if rotation else 0) + GAS_NEW_STORAGE * new_storage
    for kind, h in signers:
        if kind == "compact":
            g -= GAS_COMPACT_SAVES_H2 - GAS_COMPACT_PER_LEVEL * (h - 2)
    return g


def send_plan(gs=None):
    """The transactions a payment will send, mirroring op_send and dv.vault_call: the two keys with the most leaves
    left approve; a nearly used-up key is first renewed (a rotation approved by the other two); a key with no
    compact leaf left signs stateless. Returns [{"what", "gas", "guardians", "kinds"}]."""
    gs = gs or _guardians()
    rem = {g.i: g.remaining_leaves() for g in gs}
    height = {g.i: g.slot().h for g in gs}
    q_max = {g.i: g.slot().q_max for g in gs}
    unused = {g.i: W.ledger_hwm(g.slot().slot_id) == 0 for g in gs}  # first leaf creates the ledger entry
    pick = sorted(sorted(gs, key=lambda g: (-rem[g.i], g.i))[:2], key=lambda g: g.i)
    plan = []

    def tx(what, idx, rotation=False, extra_storage=0):
        kinds = ["stateless" if rem[i] <= 0 else "compact" for i in idx]
        new = extra_storage + sum(1 for i, k in zip(idx, kinds) if k == "compact" and unused[i])
        plan.append({"what": what, "guardians": list(idx), "kinds": kinds, "newStorage": new,
                     "gas": frame_tx_gas([(k, height[i]) for i, k in zip(idx, kinds)], new, rotation)})
        for i, k in zip(idx, kinds):
            if k == "compact":
                rem[i] -= 1
                unused[i] = False

    for g in pick:
        if dv.rotation_due(rem[g.i], q_max[g.i]):
            # registerSlot stores the new slot; revoking a never-used old slot creates its ledger entry too
            tx("renew", dv._others(g.i), rotation=True, extra_storage=1 + (1 if unused[g.i] else 0))
            rem[g.i], unused[g.i] = q_max[g.i], True
    tx("payment", [g.i for g in pick])
    return plan


def quote():
    a = account()
    base = int(rpc.eth("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
    bal = int(rpc.eth("eth_getBalance", [a, "latest"]), 16) if a else 0
    plan = send_plan() if a else [{"what": "payment", "gas": frame_tx_gas([("compact", dv.SLOT_H)] * 2), "kinds": []}]
    gas = sum(t["gas"] for t in plan)
    return {"feeWei": str(gas * (base + 10**9)), "gas": gas, "plan": plan,
            "maxSendWei": str(max(0, bal - MAX_COST_RESERVE))}


# ----------------------------------------------------------------------------- operations (background jobs)
def op_create():
    if account():
        raise RuntimeError("this wallet already has an account")
    if not W.deployment.get("factory"):
        raise RuntimeError("the network has no Leafguard contracts yet: run infra/local-net.sh up first")
    if W.load("guardians.json"):  # keys of an account on a network that no longer exists
        old = os.path.join(W.dir, f"archive-{int(time.time())}")
        os.makedirs(old)
        for f in os.listdir(W.dir):
            if f.endswith(".json") and f != "network.json":
                shutil.move(os.path.join(W.dir, f), old)
    print("keys: generating three post-quantum keys on this computer")
    dv.new_guardians(W)
    print("account: deploying your smart account (2 of 3 keys sign)")
    with deployer_lock():
        dv.deploy_vault(W, CREATE_FUND_ETH, CREATE_STAKE_ETH)
    dep = W.load("deployment.json")
    rc = rpc.eth("eth_getTransactionReceipt", [dep["txs"]["vault"]])
    dep["createdBlock"] = int(rc["blockNumber"], 16)
    W.save("deployment.json", dep)
    print(f"done: account {dep['vault']} with {CREATE_FUND_ETH} test ETH")
    return {"txHash": dep["txs"]["vault"], "receipt": rc}


def op_faucet():
    a = account()
    if not a:
        raise RuntimeError("create the account first")
    print("faucet: requesting 1 test ETH")
    with deployer_lock():
        r = json.loads(dv.cast("send", "--private-key", W.deployer_key(), "--rpc-url", W.cfg["ethrex"], "--legacy",
                               "--gas-price", str(W.gas_price()), "--json", "--value", str(FAUCET_WEI), a))
    if r.get("status") not in ("0x1", 1, "1"):
        raise RuntimeError(f"faucet transaction {r.get('transactionHash')} failed")
    print(f"faucet: tx {r['transactionHash']} status 0x1 block {int(r['blockNumber'], 16)} gasUsed {int(r['gasUsed'], 16)}")
    return {"txHash": r["transactionHash"], "receipt": r}


def op_send(to, wei):
    gs = _guardians()
    # the two keys with the most leaves left sign (ties: lower index); renewal/fallback are handled by the driver
    pick = sorted(sorted(gs, key=lambda g: (-g.remaining_leaves(), g.i))[:2], key=lambda g: g.i)
    return dv.cmd_transfer(W, to, wei, tuple(g.i for g in pick), gs=gs, label=f"send to {to}")


# ----------------------------------------------------------------------------- phones
def tunnel_url():
    """Public HTTPS URL of an ngrok tunnel to this wallet's port, if one runs (ngrok's local agent API)."""
    import urllib.request
    for api in NGROK_APIS:
        try:
            with urllib.request.urlopen(api, timeout=0.5) as r:
                tunnels = json.load(r).get("tunnels", [])
        except Exception:
            continue
        for t in tunnels:
            addr = str(t.get("config", {}).get("addr", ""))
            if t.get("public_url", "").startswith("https://") and re.search(rf"(^|:){PORT}/?$", addr):
                return t["public_url"].rstrip("/")
    return None


def pair_info():
    """How a phone reaches this wallet: over a tunnel (any network, used by the mobile app) and/or the LAN listener.
    Each link is <url>/#pair=<code>: the mobile app scans it, a phone browser opens it as the web app."""
    code = lan.pairing_code(W.dir)
    out = {"enabled": bool(LAN), "code": code, "tunnel": None}
    t = tunnel_url()
    if t:
        out["tunnel"] = {"url": t, "link": f"{t}/#pair={code}", "qr": lan.qr_svg(f"{t}/#pair={code}")}
    if LAN:
        link = f"{LAN['url']}/#pair={code}"
        out.update({"url": LAN["url"], "link": link, "qr": lan.qr_svg(link), "caUrl": LAN["url"] + "/ca.crt"})
    return out


# ----------------------------------------------------------------------------- HTTP
def manifest():
    a = account()
    name = W.wallet[:1].upper() + W.wallet[1:]
    icons = [{"src": f"/icons/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png", "purpose": p}
             for p in ("any", "maskable") for n in (192, 512)]
    return {"id": "/", "name": f"{name}'s Leafguard", "short_name": name, "start_url": "/", "scope": "/",
            "display": "standalone", "orientation": "portrait", "background_color": "#FFFFFF",
            "theme_color": ACCENTS[int(a[2:4], 16) % len(ACCENTS)] if a else ACCENTS[1],
            "description": "A 2-of-3 post-quantum smart-account wallet on a devnet. Keys stay on the wallet computer.",
            "icons": icons}


LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
FORWARD_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip")


class H(srv.H):
    @property
    def _remote(self):
        """True for a phone: the LAN listener, or a request a tunnel (ngrok, a reverse proxy) forwarded to loopback.
        A tunnel's requests come from 127.0.0.1 but carry forwarding headers or the tunnel's public Host name."""
        if getattr(self.server, "lan", False):
            return True
        if any(self.headers.get(h) for h in FORWARD_HEADERS):
            return True
        host = (self.headers.get("host") or "").strip().lower()
        host = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        return host not in LOCAL_HOSTS

    def _denied(self):
        """None if the request may proceed; from a phone every /api/ call needs the pairing code."""
        if not self._remote or not self.path.startswith("/api/"):
            return None
        r = GATE.check(self.headers.get("authorization", ""))
        if r == "ok":
            return None
        if r == "locked":
            return self._send(429, {"error": "Too many wrong pairing codes; try again in a minute.", "pair": True}) or True
        return self._send(401, {"error": "Wrong pairing code." if r == "wrong" else "This phone is not paired.",
                                "pair": True}) or True

    def do_GET(self):
        if self._denied():
            return
        path = self.path.split("?")[0].split("#")[0]
        try:
            if path == "/api/state":
                return self._send(200, dict(state(), remote=self._remote))
            if path == "/api/pair":
                if self._remote:
                    return self._send(404, {"error": "not found"})
                return self._send(200, pair_info())
            if path == "/manifest.webmanifest":
                return self._send(200, json.dumps(manifest()).encode(), CTYPES["webmanifest"])
            if path == "/ca.crt":
                b = lan.ca_cert_bytes()
                return self._send(200, b, "application/x-x509-ca-cert") if b else self._send(404, {"error": "no CA"})
            if self.path == "/api/activity":
                return self._send(200, activity())
            if self.path == "/api/quote":
                return self._send(200, quote())
            m = re.match(r"^/api/vaults/([0-9a-f]{10})$", self.path)
            if m:
                return self._send(200, vault_detail(m[1]))
            if self.path.startswith("/api/jobs/"):
                job = srv.JOBS.get(self.path.rsplit("/", 1)[-1])
                return self._send(200, job) if job else self._send(404, {"error": "unknown job"})
            f = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            p = os.path.realpath(os.path.join(APP, f))
            if not p.startswith(os.path.realpath(APP) + os.sep) or not os.path.isfile(p):
                return self._send(404, {"error": "not found"})
            ctype = CTYPES.get(p.rsplit(".", 1)[-1], "text/plain; charset=utf-8")
            return self._send(200, open(p, "rb").read(), ctype)
        except Exception as e:
            return self._send(500, {"error": srv._friendly(e)})

    def do_POST(self):
        if self._denied():
            return
        n = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/create":
                job = srv.start_job("create", op_create)
            elif self.path == "/api/faucet":
                job = srv.start_job("faucet", op_faucet)
            elif self.path == "/api/send":
                to, wei = str(body.get("to", "")).strip(), int(body.get("wei", 0))
                if not ADDR.match(to):
                    return self._send(400, {"error": "Enter a 0x address with 40 hex characters."})
                if to.lower() == (account() or "").lower():
                    return self._send(400, {"error": "That is this wallet's own address."})
                if wei <= 0:
                    return self._send(400, {"error": "Enter an amount above zero."})
                if wei > int(quote()["maxSendWei"]):
                    return self._send(400, {"error": "Not enough ETH for this amount plus the network fee."})
                job = srv.start_job("send", lambda: op_send(to, wei))
            elif self.path == "/api/vaults":
                return self._send(200, ms.create(W, body.get("name", ""), body.get("guardians", [])))
            elif re.match(r"^/api/vaults/[0-9a-f]{10}/", self.path):
                parts = self.path.split("/")
                vid, action = parts[3], parts[4]
                if action == "join":
                    return self._send(200, ms.join(W, vid))
                if action == "deploy":
                    job = srv.start_job("vault-deploy", lambda: ms.deploy(W, vid, deployer_lock))
                elif action == "propose":
                    to, wei = str(body.get("to", "")).strip(), int(body.get("wei", 0))
                    v = ms.load_vault(W, vid)
                    if not ADDR.match(to):
                        return self._send(400, {"error": "Enter a 0x address with 40 hex characters."})
                    if to.lower() == (v.get("address") or "").lower():
                        return self._send(400, {"error": "That is the vault's own address."})
                    bal = int(rpc.eth("eth_getBalance", [v["address"], "latest"]), 16) if v.get("address") else 0
                    if wei <= 0 or wei > bal - MAX_COST_RESERVE:
                        return self._send(400, {"error": "Enter an amount the vault can cover, leaving 0.005 ETH for the fee."})
                    p = ms.propose(W, vid, to, wei)
                    return self._send(200, {"proposal": p["id"]})
                elif action == "proposals" and len(parts) == 7 and parts[6] == "approve":
                    pid = parts[5]
                    if not pid.isalnum():
                        return self._send(400, {"error": "bad proposal id"})
                    p, ready = ms.approve(W, vid, pid)
                    if not ready:
                        return self._send(200, {"approvals": len(p["approvals"])})
                    job = srv.start_job("vault-payment", lambda: ms.submit(W, vid, pid))
                elif action == "proposals" and len(parts) == 7 and parts[6] == "submit":
                    pid = parts[5]
                    if not pid.isalnum():
                        return self._send(400, {"error": "bad proposal id"})
                    job = srv.start_job("vault-payment", lambda: ms.submit(W, vid, pid))
                else:
                    return self._send(404, {"error": "unknown endpoint"})
            elif self.path == "/api/network":
                ethrex, sidecar = str(body.get("ethrex", "")).strip(), str(body.get("sidecar", "")).strip()
                if not ethrex.startswith("http") or not sidecar.startswith("http"):
                    return self._send(400, {"error": "Enter both URLs, starting with http:// or https://."})
                old = (rpc.ETHREX_URL, rpc.SIDECAR_URL)
                rpc.ETHREX_URL, rpc.SIDECAR_URL = ethrex, sidecar
                try:
                    chain = int(rpc.eth("eth_chainId"), 16)
                except Exception as e:
                    rpc.ETHREX_URL, rpc.SIDECAR_URL = old
                    return self._send(400, {"error": f"No node answered at {ethrex}: {str(e)[:120]}"})
                W.cfg.update({"ethrex": ethrex, "sidecar": sidecar})
                W.save("network.json", {"ethrex": ethrex, "sidecar": sidecar})
                return self._send(200, {"chainId": chain})
            else:
                return self._send(404, {"error": "unknown endpoint"})
            return self._send(202, {"job": job["id"]})
        except BlockingIOError:
            return self._send(409, {"error": "The wallet is busy with another action; wait for it to finish."})
        except Exception as e:
            return self._send(400, {"error": srv._friendly(e)})


def main():
    global W, GATE, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="wallet name (lowercase letters, digits, dashes)")
    ap.add_argument("--port", type=int, default=8201)
    ap.add_argument("--network", default="local", choices=["local", "public"])
    ap.add_argument("--lan", action="store_true", help="also serve the app to phones on this network (HTTPS + pairing code)")
    ap.add_argument("--lan-port", type=int, help="HTTPS port for phones (default: --port + 1000)")
    a = ap.parse_args()
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,23}$", a.name):
        raise SystemExit("wallet name: lowercase letters, digits and dashes, up to 24 characters")
    W = WalletNet(a.network, a.name)
    GATE, PORT = lan.Gate(lan.pairing_code(W.dir)), a.port
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"{a.name}'s Leafguard wallet on http://127.0.0.1:{a.port}", flush=True)
    lan_file = os.path.join(W.dir, "lan.json")
    with contextlib.suppress(FileNotFoundError):
        os.remove(lan_file)
    if a.lan:
        start_lan(a.lan_port or a.port + 1000, lan_file)
    httpd.serve_forever()


def start_lan(port, lan_file):
    global LAN
    import ssl
    import threading
    ips = lan.lan_ips()
    if not ips:
        raise SystemExit("--lan: this computer has no LAN address")
    crt, key = lan.ensure_cert(ips)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(crt, key)
    https = ThreadingHTTPServer(("0.0.0.0", port), type("HLan", (H,), {"timeout": 30}))
    # handshake lazily in each connection's own thread, so one slow phone cannot stall accept()
    https.socket = ctx.wrap_socket(https.socket, server_side=True, do_handshake_on_connect=False)
    https.lan = True
    LAN = {"url": f"https://{ips[0]}:{port}", "code": lan.pairing_code(W.dir), "port": port, "ips": ips}
    with open(lan_file, "w") as f:
        json.dump({"url": LAN["url"], "port": port, "ips": ips}, f)
    threading.Thread(target=https.serve_forever, daemon=True).start()
    print(f"phones: {LAN['url']}  (pairing code in {os.path.join(W.dir, 'pair-code')}; CA: .devnet/tls/ca.crt)", flush=True)


if __name__ == "__main__":
    main()
