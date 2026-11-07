#!/usr/bin/env bash
# Start everything for the Leafguard (Leafguard) demo and print every link. Devnet only, never real funds.
#
#   ./start.sh                      chain + alt mempool + wallets + explorer + ngrok tunnels + phone app server
#   ./start.sh --local              the same without ngrok and the phone app (laptop only)
#   The explorer gets the fixed ngrok domain below (NGROK_DOMAIN=other.ngrok-free.dev ./start.sh to change it)
#
# Stop: ./demo.sh stop  (add --chain to pause the network too). Status: ./demo.sh status.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
LOCAL=.devnet/local
METRO_PORT="${METRO_PORT:-8085}"
NGROK_DOMAIN="${NGROK_DOMAIN-outrageous-syringeal-mathias.ngrok-free.dev}"   # fixed explorer link; NGROK_DOMAIN= for a random one
public=1
[ "${1:-}" = "--local" ] && public=0

say() { printf '\n\033[1;34m### %s\033[0m\n' "$*"; }

say "1/4  network, alt mempool, wallets, explorer"
./demo.sh up

say "2/4  fresh alt-mempool sidecar (a long-running one can stop relaying)"
docker restart pqvault-sidecar >/dev/null
for _ in $(seq 1 30); do
  curl -sf -m 3 -X POST -H 'content-type: application/json' \
    --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' http://127.0.0.1:14337/rpc >/dev/null && break
  sleep 1
done
echo "  sidecar restarted"

if [ $public = 1 ]; then
  say "3/4  ngrok: alice's wallet, the phone app, the explorer"
  ./demo.sh tunnel alice --app --explorer ${NGROK_DOMAIN:+--explorer-url "$NGROK_DOMAIN"}

  say "4/4  phone app dev server (Expo, through ngrok)"
  pkill -f '[e]xpo start --offline' 2>/dev/null || true
  nohup ./demo.sh app --ngrok > "$LOCAL/tunnel/metro.log" 2>&1 < /dev/null &
  for _ in $(seq 1 90); do curl -sf -o /dev/null "http://127.0.0.1:$METRO_PORT/status" && break; sleep 1; done
  curl -sf -o /dev/null "http://127.0.0.1:$METRO_PORT/status" && echo "  Metro running (log: $LOCAL/tunnel/metro.log)" \
    || echo "  Metro did not start: see $LOCAL/tunnel/metro.log"
fi

# ---------------------------------------------------------------------------- links
url_for() {  # url_for <port> <http|https>: public ngrok URL for a local port
  curl -sf -m 2 http://127.0.0.1:4040/api/tunnels 2>/dev/null | python3 -c 'import json,re,sys
p, s = sys.argv[1], sys.argv[2] + "://"
for t in json.load(sys.stdin).get("tunnels", []):
    m = re.search(r":(\d+)/?$", str(t.get("config", {}).get("addr", "")))
    if m and m[1] == p and t.get("public_url", "").startswith(s): print(t["public_url"]); break' "$1" "$2" 2>/dev/null || true
}
code() { cat "$LOCAL/wallets/$1/pair-code" 2>/dev/null || echo -; }

printf '\n\033[1mLeafguard demo is running\033[0m  (chain %s, block %s)\n\n' \
  "$(cast chain-id --rpc-url "$(python3 -c 'import json; print(json.load(open(".devnet/local/network.json"))["ethrex"])')")" \
  "$(cast block-number --rpc-url "$(python3 -c 'import json; print(json.load(open(".devnet/local/network.json"))["ethrex"])')")"
echo "On this laptop (use these for recording):"
echo "  alice     http://127.0.0.1:8201"
echo "  bob       http://127.0.0.1:8202"
echo "  carol     http://127.0.0.1:8203"
echo "  explorer  http://127.0.0.1:8300"
if [ $public = 1 ]; then
  app="$(url_for "$METRO_PORT" http)"
  echo
  echo "Public (ngrok; click 'Visit Site' on ngrok's notice):"
  echo "  explorer  $(url_for 8300 https)"
  echo "  alice     $(url_for 8201 https)   pairing code $(code alice)"
  [ -n "$app" ] && echo "  phone app exp://${app#http://}:80   (open in Expo Go, then Scan QR code / enter the alice URL + code)"
fi
echo
echo "If a payment hangs on 'Sending': docker restart pqvault-sidecar, then Send now / send again."
echo "Stop everything: ./demo.sh stop   (./demo.sh stop --chain also pauses the network)"
