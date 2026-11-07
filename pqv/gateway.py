"""One public site for the demo: the explorer at /, each wallet at /<name>/, behind a single domain.

    python -m pqv.gateway [--port 8400] [--explorer 8300] [--wallets alice:8201,bob:8202,carol:8203]
                          (or --wallet 8201: one wallet, named "alice")

Free ngrok accounts and a single Render service get one domain; this lets the explorer and the wallets share it.
- /                     the explorer, and its API (/api/overview|block|tx|address|search)
- /<name>/...           that wallet: the prefix is stripped and the rest forwarded (/bob/api/state -> /api/state)
- /wallet/... and any other /api/... path    the first wallet (older links, and the phone app pointed at the
                        bare domain)
Requests are forwarded unchanged, headers included, so a wallet still sees the public Host / X-Forwarded-For and
still requires its pairing code (pqv/wallet.py H._remote). /healthz answers 200 at once; while a server is not up
yet, pages get a "starting" page and API calls a 503. The wallets' service worker is not served at the root (its
offline shell would claim "/", the explorer). stdlib only; binds 127.0.0.1 unless --host is given.
"""
import argparse
import http.client
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EXPLORER_API = re.compile(r"^/api/(overview|block|tx|address|search)(/|\?|$)")
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "trailer"}
UP = {}
STARTING = b"""<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="15">
<title>Leafguard is starting</title><body style="font:16px system-ui;max-width:560px;margin:15vh auto;padding:0 20px;color:#172131">
<h1 style="font-size:22px">Leafguard devnet is starting</h1>
<p>A fresh EIP-8141 chain, the alt mempool and the wallets are booting. This takes about 8 minutes after a
deploy or restart. This page refreshes itself.</p></body>"""


def route(path):
    """(upstream port, upstream path, redirect) for a request path; port None = 404."""
    p = path.split("?", 1)[0]
    if p == "/healthz":
        return "health", path, None
    if p in ("/", "/index.html") or EXPLORER_API.match(path):
        return UP["explorer"], path, None
    seg = p.split("/")[1] if p.count("/") >= 1 else ""
    name = UP["first"] if seg == "wallet" else seg if seg in UP["wallets"] else None
    if name:
        prefix = "/" + seg
        if p == prefix:  # /bob -> /bob/, so the page's relative URLs (icons, manifest) resolve under /bob/
            return None, None, prefix + "/" + path[len(p):]
        return UP["wallets"][name], path[len(prefix):] or "/", None
    if p == "/sw.js":
        return None, None, None
    return UP["wallets"][UP["first"]], path, None


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    def _forward(self):
        port, path, redirect = route(self.path)
        if redirect:
            self.send_response(301)
            self.send_header("location", redirect)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        if port is None:
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        if port == "health":
            return self._plain(200, b"ok", "text/plain")
        body = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            c.request(self.command, path, body=body or None, headers=headers)
            resp = c.getresponse()
            data = resp.read()
        except OSError:
            if self.path.startswith("/api/"):
                return self._plain(503, b'{"error": "The Leafguard devnet is still starting; try again in a minute."}',
                                   "application/json")
            return self._plain(503, STARTING, "text/html; charset=utf-8")
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP and k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _plain(self, status, body, ctype):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_HEAD = _forward

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--explorer", type=int, default=8300)
    ap.add_argument("--wallet", type=int, help="one wallet's port (served as alice and at /wallet)")
    ap.add_argument("--wallets", default="", help="name:port,... ; the first is also /wallet")
    a = ap.parse_args()
    pairs = [w.split(":") for w in a.wallets.split(",") if w] or [["alice", str(a.wallet or 8201)]]
    UP.update(explorer=a.explorer, wallets={n: int(p) for n, p in pairs}, first=pairs[0][0])
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"gateway on http://{a.host}:{a.port}: / -> explorer :{a.explorer}; "
          + ", ".join(f"/{n}/ -> :{p}" for n, p in pairs) + f"; /wallet -> {pairs[0][0]}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
