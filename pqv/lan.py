"""Phone access to a personal wallet over the local network (the wallet's installable mobile app, app/wallet/).

The wallet process keeps every key; a phone is only a screen. With `python -m pqv.wallet <name> --lan`, a second
listener serves the same app over HTTPS on the LAN (port + 1000 by default) and every /api/ call on it must carry
the wallet's pairing code (`authorization: Bearer <code>`). The loopback listener is unchanged.

HTTPS is needed because phones only install a web app (service worker, standalone window) from a secure origin.
The certificate comes from a per-machine devnet CA under .devnet/tls/ (made with the openssl CLI; RSA, transport
only, nothing to do with wallet signing). Install .devnet/tls/ca.crt on the phone once (served at /ca.crt).
Android over USB needs none of this: `adb reverse tcp:8201 tcp:8201` and open http://localhost:8201 on the phone.
"""
import contextlib
import fcntl
import hmac
import ipaddress
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time

from .hashing import ROOT

TLS_DIR = os.path.join(ROOT, ".devnet", "tls")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
FAIL_LIMIT, FAIL_WINDOW = 20, 60.0                  # wrong codes per window before the LAN API locks for the window


# ----------------------------------------------------------------------------- addresses
def lan_ips():
    """IPv4 addresses a phone on the same network could use; the default-route one first, docker bridges last."""
    ips = []
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # no packet is sent; this only asks the kernel which interface routes out
        ips.append(s.getsockname()[0])
    with contextlib.suppress(Exception):
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()
        ips += [ip for ip in out if ":" not in ip]
    seen, good = set(), []
    for ip in ips:
        a = ipaddress.ip_address(ip)
        if ip in seen or a.is_loopback or a.is_link_local:
            continue
        seen.add(ip)
        good.append(ip)
    return sorted(good, key=lambda ip: (ip != ips[0], ip.startswith("172.17.")))


# ----------------------------------------------------------------------------- certificates
def _openssl(*args):
    subprocess.run(["openssl", *args], check=True, capture_output=True, timeout=60)


def ensure_cert(ips):
    """(cert, key) for a server certificate covering `ips` + localhost, signed by the devnet CA (both made if absent)."""
    if not shutil.which("openssl"):
        raise SystemExit("--lan needs the openssl command to make the devnet HTTPS certificate")
    os.makedirs(TLS_DIR, mode=0o700, exist_ok=True)
    ca_key, ca_crt = os.path.join(TLS_DIR, "ca.key"), os.path.join(TLS_DIR, "ca.crt")
    key, crt, sans_f = (os.path.join(TLS_DIR, f) for f in ("server.key", "server.crt", "server.sans"))
    sans = ",".join(["DNS:localhost", "IP:127.0.0.1"] + [f"IP:{ip}" for ip in ips])
    with open(os.path.join(TLS_DIR, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # several wallets may start at once; they share one CA and one certificate
        if not os.path.exists(ca_crt):
            _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "825",
                     "-keyout", ca_key, "-out", ca_crt, "-subj", "/O=Leafguard devnet/CN=Leafguard devnet local CA",
                     "-addext", "basicConstraints=critical,CA:TRUE",
                     "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        have = open(sans_f).read() if os.path.exists(sans_f) else ""
        if have != sans or not os.path.exists(crt) or os.path.getmtime(crt) < os.path.getmtime(ca_crt):
            csr, ext = crt + ".csr", crt + ".ext"
            _openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-keyout", key, "-out", csr,
                     "-subj", "/O=Leafguard devnet/CN=Leafguard")
            with open(ext, "w") as f:
                f.write(f"subjectAltName={sans}\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n"
                        "keyUsage=critical,digitalSignature,keyEncipherment\n")
            # 397 days: the longest validity Apple and Chrome accept for a TLS server certificate
            _openssl("x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key, "-CAcreateserial", "-sha256",
                     "-days", "397", "-extfile", ext, "-out", crt)
            os.remove(csr)
            os.remove(ext)
            with open(sans_f, "w") as f:
                f.write(sans)
        for p in (ca_key, key):
            os.chmod(p, 0o600)
    return crt, key


def ca_cert_bytes():
    p = os.path.join(TLS_DIR, "ca.crt")
    return open(p, "rb").read() if os.path.exists(p) else None


# ----------------------------------------------------------------------------- pairing
def pairing_code(wallet_dir):
    """This wallet's pairing code (made once, kept 0600 in the wallet folder), e.g. 'K7QM-P3XA-WN9D'."""
    p = os.path.join(wallet_dir, "pair-code")
    if not os.path.exists(p):
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))  # 60 bits
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("-".join(raw[i:i + 4] for i in range(0, 12, 4)))
    return open(p).read().strip()


def normalize(code):
    return "".join(c for c in str(code).upper() if c in CODE_ALPHABET)


class Gate:
    """Checks the pairing code on LAN requests; locks the LAN API for a minute after too many wrong codes."""

    def __init__(self, code):
        self.code = normalize(code)
        self.fails = []
        self.lock = threading.Lock()

    def check(self, header):
        given = normalize(header[7:] if header and header.lower().startswith("bearer ") else "")
        now = time.monotonic()
        with self.lock:
            self.fails = [t for t in self.fails if now - t < FAIL_WINDOW]
            if len(self.fails) >= FAIL_LIMIT:
                return "locked"
            if given and hmac.compare_digest(given, self.code):
                return "ok"
            if given:
                self.fails.append(now)
            return "missing" if not given else "wrong"


def qr_svg(text):
    """An SVG QR code via the qrencode CLI, or None if it is not installed."""
    if not shutil.which("qrencode"):
        return None
    with contextlib.suppress(Exception):
        return subprocess.run(["qrencode", "-t", "SVG", "-m", "2", "-s", "6", "-o", "-", text],
                              capture_output=True, text=True, timeout=10, check=True).stdout
    return None
