"""One public site for the demo: the explorer at / and one wallet at /wallet, behind a single tunnel domain.

    python -m pqv.gateway [--port 8400] [--explorer 8300] [--wallet 8201]

Free ngrok accounts get one fixed domain; this lets the explorer and a wallet share it. The two servers' paths do
not overlap (explorer: /api/overview|block|tx|address|search; wallet: every other /api/ path, its manifest and
icons), so requests are routed by path and forwarded unchanged, headers included. The wallet still sees the
tunnel's Host / X-Forwarded-For and therefore still requires its pairing code (pqv/wallet.py H._remote).
The wallet's service worker is not served here: its offline shell would claim "/", which is the explorer.
stdlib only; binds 127.0.0.1 unless --host is given (the Docker image uses 0.0.0.0). /healthz answers 200 at once.
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
    """(upstream port, upstream path) for a request path, or None for 404."""
    p = path.split("?", 1)[0]
    if p == "/healthz":
        return "health", path
    if p in ("/", "/index.html") or EXPLORER_API.match(path):
        return UP["explorer"], path
    if p in ("/wallet", "/wallet/"):
        return UP["wallet"], "/" + path[len(p):]
    if p == "/sw.js":
        return None
    return UP["wallet"], path


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    def _forward(self):
        r = route(self.path)
        if r is None:
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        port, path = r
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
    ap.add_argument("--wallet", type=int, default=8201)
    a = ap.parse_args()
    UP.update(explorer=a.explorer, wallet=a.wallet)
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"gateway on http://{a.host}:{a.port}: / -> explorer :{a.explorer}, /wallet -> wallet :{a.wallet}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
