#!/usr/bin/env bash
# Start / stop several Leafguard personal wallets for a demo (one browser tab each).
#
#   infra/wallets.sh start alice bob carol     # -> http://127.0.0.1:8201, :8202, :8203
#   infra/wallets.sh start --lan alice bob     # also serve the mobile app to phones: https://<lan-ip>:9201, :9202
#   infra/wallets.sh pair alice                # phone link, pairing code and a QR code in the terminal
#   infra/wallets.sh status
#   infra/wallets.sh stop
#
# The wallet page is an installable mobile app (PWA). Phones are only screens: keys and signing stay in the wallet
# process on this computer. See pqv/lan.py for the HTTPS certificate and the pairing code.
#
# Needs a running network with Leafguard contracts (infra/local-net.sh up). Each wallet keeps its keys and
# account under .devnet/local/wallets/<name>/; open its page and press "Create wallet" the first time.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
RUN=.devnet/local/wallets
BASE_PORT="${BASE_PORT:-8201}"

pick_python() {
  local c
  for c in "${PQV_PYTHON:-}" python3.12 python3.13 python3 "$HOME"/.local/share/uv/python/cpython-3.1*/bin/python3; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
    "$c" -c 'import hashlib; hashlib.new("KECCAK-256")' >/dev/null 2>&1 && { echo "$c"; return; }
  done
  echo "no Python with KECCAK-256 (OpenSSL >= 3.2); set PQV_PYTHON" >&2; exit 1
}

cmd_start() {
  local lan=()
  if [ "${1:-}" = "--lan" ]; then lan=(--lan); shift; fi
  [ $# -gt 0 ] || { echo "usage: $0 start [--lan] <name> [<name> ...]" >&2; exit 2; }
  local py port=$BASE_PORT name
  py="$(pick_python)"
  mkdir -p "$RUN"
  for name in "$@"; do
    if [ -f "$RUN/$name/server.pid" ] && kill -0 "$(cat "$RUN/$name/server.pid")" 2>/dev/null; then
      echo "$name: already running on http://127.0.0.1:$(cat "$RUN/$name/server.port")"
    else
      mkdir -p "$RUN/$name"
      nohup "$py" -m pqv.wallet "$name" --port "$port" ${lan[@]+"${lan[@]}"} > "$RUN/$name/server.log" 2>&1 &
      echo $! > "$RUN/$name/server.pid"; echo "$port" > "$RUN/$name/server.port"
      until curl -sf "http://127.0.0.1:$port/api/state" >/dev/null 2>&1; do
        kill -0 "$(cat "$RUN/$name/server.pid")" 2>/dev/null || { echo "$name: failed, see $RUN/$name/server.log" >&2; exit 1; }
        sleep 1
      done
      echo "$name: http://127.0.0.1:$port$(lan_url "$name")"
    fi
    port=$((port + 1))
  done
}

lan_url() {  # ", phones: https://…" if the wallet serves phones
  local f="$RUN/$1/lan.json"
  [ -f "$f" ] && sed -n 's/.*"url": *"\([^"]*\)".*/, phones: \1/p' "$f"
  return 0
}

cmd_pair() {
  local name="${1:-}" port
  [ -n "$name" ] && [ -f "$RUN/$name/server.port" ] || { echo "usage: $0 pair <running wallet name>" >&2; exit 2; }
  port="$(cat "$RUN/$name/server.port")"
  [ -f "$RUN/$name/lan.json" ] || { echo "$name does not serve phones; restart it with: $0 start --lan $name" >&2; exit 1; }
  local link
  link="$(curl -sf "http://127.0.0.1:$port/api/pair" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("link") or "")' 2>/dev/null)"
  [ -n "$link" ] || { echo "$name: could not read the pairing link (is it running?)" >&2; exit 1; }
  echo "$name on a phone (same Wi-Fi):"
  echo "  1. once per phone, install the devnet CA: ${link%%/#*}/ca.crt  (file: .devnet/tls/ca.crt)"
  echo "  2. open: $link"
  echo "  pairing code: $(cat "$RUN/$name/pair-code")"
  command -v qrencode >/dev/null && qrencode -t ansiutf8 -m 2 "$link"
  echo "Android over USB instead (no certificate, no code): adb reverse tcp:$port tcp:$port, then open http://localhost:$port"
  echo "The Leafguard app (mobile/) from any network: ./demo.sh tunnel $name"
}

cmd_status() {
  local d name
  for d in "$RUN"/*/; do
    [ -f "$d/server.pid" ] || continue
    name="$(basename "$d")"
    if kill -0 "$(cat "$d/server.pid")" 2>/dev/null; then echo "$name: http://127.0.0.1:$(cat "$d/server.port")$(lan_url "$name")"
    else echo "$name: stopped"; fi
  done
}

cmd_stop() {
  local d
  for d in "$RUN"/*/; do
    [ -f "$d/server.pid" ] || continue
    kill "$(cat "$d/server.pid")" 2>/dev/null || true
    rm -f "$d/server.pid"
    echo "$(basename "$d"): stopped"
  done
}

case "${1:-}" in
  start)  shift; cmd_start "$@" ;;
  pair)   shift; cmd_pair "$@" ;;
  status) cmd_status ;;
  stop)   cmd_stop ;;
  *) echo "usage: $0 start [--lan] <name>... | pair <name> | status | stop" >&2; exit 2 ;;
esac
