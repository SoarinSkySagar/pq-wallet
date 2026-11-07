"""One public site for the demo: the explorer at / and one wallet at /wallet, behind a single tunnel domain.

    python -m pqv.gateway [--port 8400] [--explorer 8300] [--wallet 8201]

Free ngrok accounts get one fixed domain; this lets the explorer and a wallet share it. The two servers' paths do
not overlap (explorer: /api/overview|block|tx|address|search; wallet: every other /api/ path, its manifest and
icons), so requests are routed by path and forwarded unchanged, headers included. The wallet still sees the
tunnel's Host / X-Forwarded-For and therefore still requires its pairing code (pqv/wallet.py H._remote).
The wallet's service worker is not served here: its offline shell would claim "/", which is the explorer.
stdlib only; binds 127.0.0.1.
"""
import argparse
import http.client
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EXPLORER_API = re.compile(r"^/api/(overview|block|tx|address|search)(/|\?|$)")
HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "trailer"}
UP = {}


def route(path):
    """(upstream port, upstream path) for a request path, or None for 404."""
    p = path.split("?", 1)[0]
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
        body = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            c.request(self.command, path, body=body or None, headers=headers)
            resp = c.getresponse()
            data = resp.read()
        except OSError as e:
            msg = f"upstream on port {port} is not answering: {e}".encode()
            self.send_response(502)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP and k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_POST = do_HEAD = _forward

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--explorer", type=int, default=8300)
    ap.add_argument("--wallet", type=int, default=8201)
    a = ap.parse_args()
    UP.update(explorer=a.explorer, wallet=a.wallet)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"gateway on http://127.0.0.1:{a.port}: / -> explorer :{a.explorer}, /wallet -> wallet :{a.wallet}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
