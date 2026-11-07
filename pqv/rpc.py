"""JSON-RPC client (stdlib only) for the chain-1337 devnet: ethrex root and ERC-8421 sidecar."""
import json
import time
import urllib.error
import urllib.request

ETHREX_URL = "https://altmempool.forshtat.com"
SIDECAR_URL = "https://altmempoolgateway.forshtat.com/rpc"  # workshop gateway: alt mempool, faucet, explorer
EXPLORER_URL = "https://explorer.altmempool.wtf"


class RpcError(Exception):
    def __init__(self, method, error):
        self.method, self.error = method, error
        super().__init__(f"{method}: {json.dumps(error)[:2000]}")


_id = 0


def call(url: str, method: str, params=None, timeout: float = 60):
    global _id
    _id += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json",
                                                          "user-agent": "leafguard/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        resp = json.loads(e.read() or b"{}")
    if "error" in resp:
        raise RpcError(method, resp["error"])
    return resp.get("result")


def eth(method, params=None, **kw):
    return call(ETHREX_URL, method, params, **kw)


def sidecar(method, params=None, **kw):
    return call(SIDECAR_URL, method, params, **kw)


def wait_receipt(tx_hash: str, timeout: float = 120, poll: float = 2.0):
    end = time.time() + timeout
    while time.time() < end:
        r = eth("eth_getTransactionReceipt", [tx_hash])
        if r:
            return r
        time.sleep(poll)
    raise TimeoutError(f"no receipt for {tx_hash} after {timeout}s")
