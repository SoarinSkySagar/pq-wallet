#!/usr/bin/env bash
# Everything the Leafguard demo needs, in one command (devnet only, never real funds):
#
#   ./demo.sh                 same as ./demo.sh up
#   ./demo.sh up [--fresh] [--no-lan] [--wallets "alice bob carol"]
#   ./demo.sh status
#   ./demo.sh stop [--chain]  stop the wallets, the explorer and the tunnel (--chain: also pause the network; `up` resumes it)
#   ./demo.sh tunnel [alice ...] [--app] [--explorer] [--url <domain>] [--explorer-url <domain>] [--site <domain>]
#                             --site: one fixed domain for the explorer (/) and the wallet (/wallet), via pqv/gateway.py
#                             expose wallets (and with --app the app's dev server) over ngrok; prints pairing QR codes
#   ./demo.sh app [--ngrok | --usb]
#                             the mobile app's dev server (mobile/, Expo, port 8085, no Expo login); open it in Expo Go:
#                             same Wi-Fi by default, --ngrok through ./demo.sh tunnel --app, --usb for Android via adb
#
# `up` is safe to re-run. It
#   1. brings up the local EIP-8141 network: reuses a running one, resumes stopped containers after a Docker
#      restart (same chain, contracts and wallets), or builds a fresh one with infra/local-net.sh up (~10 min;
#      also with --fresh, which archives the old local state)
#   2. makes sure the ERC-8421 [BUDGET-010] alt-mempool sidecar runs (the stock one rejects Leafguard)
#   3. starts the personal wallets (infra/wallets.sh), by default with phone access (HTTPS + pairing code)
#   4. creates each wallet's account if it has none, and tops up wallets below 0.1 ETH from the devnet faucet
#   5. starts the block explorer (pqv/explorer.py) and prints every URL and pairing code
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
LOCAL=.devnet/local
ENCLAVE="${PQV_ENCLAVE:-pqvault-devnet}"
EXPLORER_PORT="${EXPLORER_PORT:-8300}"
METRO_PORT="${METRO_PORT:-8085}"     # the mobile app's dev server (8081 is the Kurtosis engine's)
BASE_PORT="${BASE_PORT:-8201}"
HEGOTA_BLOCK=66        # frame transactions are enabled from here (infra/local-net.sh)
LOW_BALANCE_WEI=100000000000000000   # 0.1 ETH

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

pick_python() {   # a Python whose hashlib has KECCAK-256 (OpenSSL >= 3.2)
  local c
  for c in "${PQV_PYTHON:-}" python3.12 python3.13 python3 "$HOME"/.local/share/uv/python/cpython-3.1*/bin/python3; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
    "$c" -c 'import hashlib; hashlib.new("KECCAK-256")' >/dev/null 2>&1 && { echo "$c"; return; }
  done
  die "no Python with KECCAK-256 (needs OpenSSL >= 3.2); set PQV_PYTHON"
}

json_get() {  # json_get <file> <key>   (prints nothing if missing)
  python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")
except Exception: pass' "$1" "$2"
}

rpc_up() { [ -n "${1:-}" ] && cast chain-id --rpc-url "$1" >/dev/null 2>&1; }
sidecar_up() {
  curl -sf -m 5 -X POST -H 'content-type: application/json' \
    --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' "$1" >/dev/null 2>&1
}

# ----------------------------------------------------------------------------- 0. preflight
preflight() {
  log "checking tools"
  local t missing=()
  for t in docker cast forge curl openssl; do command -v "$t" >/dev/null || missing+=("$t"); done
  [ ${#missing[@]} -eq 0 ] || die "missing: ${missing[*]} (docker, foundry, curl, openssl)"
  docker info >/dev/null 2>&1 || die "Docker is not running"
  PY="$(pick_python)"
  ok "python: $PY"
  if [ ! -e third_party/JARDIN/.git ] || [ -z "$(ls -A third_party/JARDIN 2>/dev/null)" ]; then
    log "fetching the JARDIN / SPHINCS- submodules"
    git submodule update --init
  fi
  command -v qrencode >/dev/null || warn "qrencode not installed: no QR codes for phones (sudo apt install qrencode)"
}

# ----------------------------------------------------------------------------- 1. network
resume_chain() {   # restart this enclave's stopped node containers (execution, consensus, validator)
  local role ids
  for role in el cl vc; do
    ids="$(docker ps -a --filter "label=kurtosis_enclave_name=$ENCLAVE" --filter status=exited --format '{{.ID}} {{.Names}}' \
           | awk -v r="^$role-[0-9]+-" '$2 ~ r {print $1}')"
    [ -n "$ids" ] || continue
    # shellcheck disable=SC2086
    docker start $ids >/dev/null
    ok "restarted $role container(s)"
    if [ "$role" = el ]; then sleep 3; fi
  done
}

ensure_chain() {
  local rpc fresh="$1"
  rpc="$(json_get "$LOCAL/network.json" ethrex)"
  if [ "$fresh" = 1 ]; then
    log "fresh network requested"
  elif rpc_up "$rpc"; then
    ok "network already running: $rpc"
  elif [ -n "$rpc" ] && docker ps -a --filter "label=kurtosis_enclave_name=$ENCLAVE" --format '{{.Names}}' | grep -q '^el-'; then
    log "network stopped (Docker restart?): resuming the existing chain"
    resume_chain
    local i
    for i in $(seq 1 60); do rpc_up "$rpc" && break; sleep 2; done
    rpc_up "$rpc" || die "ethrex did not come back at $rpc; run ./demo.sh up --fresh for a new chain"
    ok "network resumed: $rpc"
  else
    log "no local network found"
    fresh=1
  fi
  if [ "$fresh" = 1 ]; then
    command -v kurtosis >/dev/null || die "a fresh network needs kurtosis"
    log "building a fresh network with infra/local-net.sh up (about 10 minutes)"
    infra/local-net.sh up budget010
    rpc="$(json_get "$LOCAL/network.json" ethrex)"
  fi
  [ -n "$(json_get "$LOCAL/deployment.json" factory)" ] \
    || die "the network has no Leafguard contracts (.devnet/local/deployment.json); run ./demo.sh up --fresh"

  # blocks must be advancing (a resumed beacon node takes a moment) and past the frame-tx fork
  local b0 b1 i
  b0="$(cast block-number --rpc-url "$rpc")"
  for i in $(seq 1 45); do
    sleep 2; b1="$(cast block-number --rpc-url "$rpc")"
    [ "$b1" -gt "$b0" ] && break
  done
  [ "$b1" -gt "$b0" ] || die "no new blocks after 90 s (block $b0); check: docker ps -a | grep -E '^(el|cl|vc)-'"
  if [ "$b1" -lt "$HEGOTA_BLOCK" ]; then
    log "waiting for frame transactions (block $HEGOTA_BLOCK, now $b1)"
    until [ "$(cast block-number --rpc-url "$rpc")" -ge "$HEGOTA_BLOCK" ]; do sleep 10; done
  fi
  ok "blocks advancing (block $(cast block-number --rpc-url "$rpc"))"
  RPC="$rpc"
}

ensure_sidecar() {
  local url image
  url="$(json_get "$LOCAL/network.json" sidecar)"
  if ! sidecar_up "$url"; then
    if docker ps -a --format '{{.Names}}' | grep -qx pqvault-sidecar; then
      log "starting the stopped alt-mempool sidecar"
      docker start pqvault-sidecar >/dev/null
    else
      log "starting the alt-mempool sidecar ([BUDGET-010])"
      infra/local-net.sh sidecar budget010
    fi
    local i
    for i in $(seq 1 60); do sidecar_up "$url" && break; sleep 2; done
    sidecar_up "$url" || die "the sidecar does not answer at $url (docker logs pqvault-sidecar)"
  fi
  image="$(docker inspect pqvault-sidecar --format '{{.Config.Image}}' 2>/dev/null || echo unknown)"
  case "$image" in
    *budget-010*) ok "alt mempool: $url ($image)" ;;
    *) warn "alt mempool runs $image: the stock image rejects Leafguard (100k validation cap)."
       warn "switch with: infra/local-net.sh sidecar budget010" ;;
  esac
}

# ----------------------------------------------------------------------------- 2. wallets
sync_wallet_network() {   # a wallet that saved its own node URLs follows the demo network after a fresh chain
  local d own shared
  shared="$(cat "$LOCAL/network.json")"
  for d in "$LOCAL"/wallets/*/; do
    [ -f "$d/network.json" ] || continue
    own="$(json_get "$d/network.json" ethrex)"
    if [ "$own" != "$(json_get "$LOCAL/network.json" ethrex)" ] && ! rpc_up "$own"; then
      echo "$shared" > "$d/network.json"
      ok "$(basename "$d"): node URL updated to the demo network"
    fi
  done
}

wallet_port() { cat "$LOCAL/wallets/$1/server.port"; }

api() {   # api <wallet> GET|POST <path>
  local port; port="$(wallet_port "$1")"
  if [ "$2" = POST ]; then curl -sf -m 30 -X POST -H 'content-type: application/json' -d '{}' "http://127.0.0.1:$port$3"
  else curl -sf -m 30 "http://127.0.0.1:$port$3"; fi
}

run_job() {   # run_job <wallet> <path> <label>
  local job state
  job="$(api "$1" POST "$2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["job"])')" \
    || die "$1: could not start $3"
  while :; do
    sleep 2
    state="$(api "$1" GET "/api/jobs/$job" | python3 -c 'import json,sys
d = json.load(sys.stdin); print(d["state"] if d["state"] != "error" else "error: " + str(d.get("error"))[:200])')" || continue
    [ "$state" = running ] || break
  done
  [ "$state" = done ] || die "$1: $3 failed: $state"
}

ensure_wallets() {
  local lan="$1" name; shift
  sync_wallet_network
  log "starting wallets: $*"
  infra/wallets.sh stop >/dev/null 2>&1 || true
  if [ "$lan" = 1 ]; then BASE_PORT="$BASE_PORT" infra/wallets.sh start --lan "$@"
  else BASE_PORT="$BASE_PORT" infra/wallets.sh start "$@"; fi

  for name in "$@"; do
    local st has bal
    st="$(api "$name" GET /api/state)" || die "$name: wallet not answering"
    has="$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(1 if d.get("account") else 0)' <<<"$st")"
    if [ "$has" = 0 ]; then
      log "$name: creating the account (three PQ keys, smart account, 1 test ETH; ~20 s)"
      run_job "$name" /api/create "account creation"
      ok "$name: account created"
    fi
    bal="$(api "$name" GET /api/state | python3 -c 'import json,sys; print(json.load(sys.stdin)["account"]["balanceWei"])')"
    if python3 -c 'import sys; sys.exit(int(sys.argv[1]) >= int(sys.argv[2]))' "$bal" "$LOW_BALANCE_WEI"; then
      log "$name: balance low, adding 1 test ETH"
      run_job "$name" /api/faucet "faucet"
    fi
  done
}

archive_stale_vaults() {   # shared vaults deployed on an earlier chain do not exist on this one: hide them
  local d a n
  for d in "$LOCAL"/vaults/*/; do
    [ -f "$d/vault.json" ] || continue
    a="$(json_get "$d/vault.json" address)"
    [ -n "$a" ] || continue
    if [ "$(cast codesize "$a" --rpc-url "$RPC" 2>/dev/null || echo 1)" = 0 ]; then
      n="$(json_get "$d/vault.json" name)"
      mkdir -p "$LOCAL/vaults-archive"; mv "$d" "$LOCAL/vaults-archive/"
      warn "vault \"$n\" ($a) is from an earlier chain: archived to $LOCAL/vaults-archive/"
    fi
  done
}

# ----------------------------------------------------------------------------- 3. explorer
explorer_running() { [ -f "$LOCAL/explorer.pid" ] && kill -0 "$(cat "$LOCAL/explorer.pid")" 2>/dev/null; }

ensure_explorer() {
  if explorer_running && curl -sf -m 5 -o /dev/null "http://127.0.0.1:$EXPLORER_PORT/"; then
    ok "explorer already running"
    return
  fi
  explorer_running && kill "$(cat "$LOCAL/explorer.pid")" 2>/dev/null || true
  nohup "$PY" -m pqv.explorer --network local --port "$EXPLORER_PORT" > "$LOCAL/explorer.log" 2>&1 &
  echo $! > "$LOCAL/explorer.pid"
  local i
  for i in $(seq 1 30); do curl -sf -m 5 -o /dev/null "http://127.0.0.1:$EXPLORER_PORT/" && break
    kill -0 "$(cat "$LOCAL/explorer.pid")" 2>/dev/null || die "explorer failed: see $LOCAL/explorer.log"; sleep 1; done
  ok "explorer started"
}

# ----------------------------------------------------------------------------- summary
summary() {
  local name port lan bal
  echo
  printf '\033[1mLeafguard demo is ready\033[0m  (chain %s, block %s)\n\n' "$(cast chain-id --rpc-url "$RPC")" "$(cast block-number --rpc-url "$RPC")"
  printf '  %-8s %-24s %-32s %-16s %s\n' wallet computer phone "pairing code" balance
  for name in "$@"; do
    port="$(wallet_port "$name")"
    lan="$(json_get "$LOCAL/wallets/$name/lan.json" url)"
    bal="$(api "$name" GET /api/state | python3 -c 'import json,sys; print("%.4f ETH" % (int(json.load(sys.stdin)["account"]["balanceWei"]) / 1e18))')"
    printf '  %-8s %-24s %-32s %-16s %s\n' "$name" "http://127.0.0.1:$port" "${lan:--}" \
      "$([ -n "$lan" ] && cat "$LOCAL/wallets/$name/pair-code" || echo -)" "$bal"
  done
  echo
  echo "  explorer  http://127.0.0.1:$EXPLORER_PORT"
  if [ -n "$(json_get "$LOCAL/wallets/$1/lan.json" url)" ]; then
    echo
    echo "  Phones (same Wi-Fi): install the devnet CA once from <phone url>/ca.crt, then open the wallet's"
    echo "  Phone screen on the computer and scan its QR code (or: infra/wallets.sh pair <name>)."
    echo "  Android over USB instead: adb reverse tcp:$BASE_PORT tcp:$BASE_PORT, open http://localhost:$BASE_PORT"
  fi
  echo
  echo "  ./demo.sh status | ./demo.sh stop [--chain]"
}

# ----------------------------------------------------------------------------- mobile app: ngrok tunnel + Expo
TUNNEL_DIR="$LOCAL/tunnel"
tunnel_running() { [ -f "$TUNNEL_DIR/ngrok.pid" ] && kill -0 "$(cat "$TUNNEL_DIR/ngrok.pid")" 2>/dev/null; }
stop_tunnel() {
  if tunnel_running; then kill "$(cat "$TUNNEL_DIR/ngrok.pid")"; echo "tunnel: stopped"; fi; rm -f "$TUNNEL_DIR/ngrok.pid"
  if [ -f "$TUNNEL_DIR/gateway.pid" ] && kill "$(cat "$TUNNEL_DIR/gateway.pid")" 2>/dev/null; then echo "gateway: stopped"; fi
  rm -f "$TUNNEL_DIR/gateway.pid"
}
GATEWAY_PORT="${GATEWAY_PORT:-8400}"
start_gateway() {   # start_gateway <wallet port>: explorer at /, that wallet at /wallet, on one port (pqv/gateway.py)
  [ -f "$TUNNEL_DIR/gateway.pid" ] && kill "$(cat "$TUNNEL_DIR/gateway.pid")" 2>/dev/null || true
  nohup "${PY:-$(pick_python)}" -m pqv.gateway --port "$GATEWAY_PORT" --explorer "$EXPLORER_PORT" --wallet "$1" \
    > "$TUNNEL_DIR/gateway.log" 2>&1 &
  echo $! > "$TUNNEL_DIR/gateway.pid"
  local i
  for i in $(seq 1 30); do curl -sf -o /dev/null "http://127.0.0.1:$GATEWAY_PORT/" && return 0; sleep 0.3; done
  die "gateway did not start (see $TUNNEL_DIR/gateway.log)"
}

tunnel_urls() {   # "<port> <public url>" for every tunnel of the running ngrok agent(s), http and https
  local p
  for p in 4040 4041 4042; do
    curl -sf -m 2 "http://127.0.0.1:$p/api/tunnels" 2>/dev/null | python3 -c 'import json,re,sys
for t in json.load(sys.stdin).get("tunnels", []):
    m = re.search(r":(\d+)/?$", str(t.get("config", {}).get("addr", "")))
    if m and t.get("public_url", "").startswith("http"): print(m[1], t["public_url"])' 2>/dev/null || true
  done
}
tunnel_url_for() {   # tunnel_url_for <port> <http|https>
  tunnel_urls | awk -v p="$1" -v s="$2://" '$1 == p && index($2, s) == 1 {print $2; exit}'
}

cmd_tunnel() {
  local names=() explorer=0 app=0 url="" xurl="" site="" n port
  while [ $# -gt 0 ]; do
    case "$1" in
      --explorer) explorer=1 ;;
      --app) app=1 ;;
      --url) shift; url="${1:?--url needs your ngrok domain, e.g. you.ngrok-free.app}" ;;
      --explorer-url) shift; xurl="${1:?--explorer-url needs your ngrok domain}"; explorer=1 ;;
      --site) shift; site="${1:?--site needs your ngrok domain}" ;;
      -*) die "unknown option $1 (tunnel [alice ...] [--app] [--explorer] [--url <domain>] [--explorer-url <domain>] [--site <domain>])" ;;
      *) names+=("$1") ;;
    esac
    shift
  done
  [ ${#names[@]} -gt 0 ] || names=(alice)
  command -v ngrok >/dev/null || die "ngrok is not installed (https://ngrok.com/download), then: ngrok config add-authtoken <token>"
  for n in "${names[@]}"; do
    [ -f "$LOCAL/wallets/$n/server.port" ] && curl -sf -m 5 -o /dev/null "http://127.0.0.1:$(wallet_port "$n")/api/state" \
      || die "wallet $n is not running: ./demo.sh up first"
  done
  [ -z "$url" ] || [ ${#names[@]} -eq 1 ] || die "--url names one domain: use it with a single wallet"
  [ -z "$site" ] || [ ${#names[@]} -eq 1 ] || die "--site serves one wallet (at /wallet): name one"
  stop_tunnel >/dev/null
  mkdir -p "$TUNNEL_DIR"
  if [ -n "$site" ]; then
    explorer_running || die "the explorer is not running: ./demo.sh up first"
    start_gateway "$(wallet_port "${names[0]}")"
    {
      echo 'version: "3"'; echo 'tunnels:'
      echo "  site: {proto: http, addr: $GATEWAY_PORT, domain: $site}"
      [ $app = 0 ] || echo "  app: {proto: http, addr: $METRO_PORT, schemes: [http]}"
    } > "$TUNNEL_DIR/ngrok.yml"
    local cfg; cfg="$(ngrok config check 2>&1 | sed -n 's/.*file at //p')"
    log "starting ngrok: https://$site (explorer at /, ${names[0]} at /wallet)$([ $app = 1 ] && echo ' + the app (Metro)')"
    nohup ngrok start --all ${cfg:+--config "$cfg"} --config "$TUNNEL_DIR/ngrok.yml" --log stdout > "$TUNNEL_DIR/ngrok.log" 2>&1 &
    echo $! > "$TUNNEL_DIR/ngrok.pid"
    local i
    for i in $(seq 1 40); do
      [ -n "$(tunnel_url_for "$GATEWAY_PORT" https)" ] && { [ $app = 0 ] || [ -n "$(tunnel_url_for "$METRO_PORT" http)" ]; } && break
      tunnel_running || { tail -5 "$TUNNEL_DIR/ngrok.log" >&2; die "ngrok stopped (see $TUNNEL_DIR/ngrok.log)"; }
      sleep 0.5
    done
    [ -n "$(tunnel_url_for "$GATEWAY_PORT" https)" ] || { tail -5 "$TUNNEL_DIR/ngrok.log" >&2; die "ngrok gave no URL for $site"; }
    local code link; code="$(cat "$LOCAL/wallets/${names[0]}/pair-code")"; link="https://$site/wallet#pair=$code"
    echo
    printf '\033[1mexplorer\033[0m  https://%s\n' "$site"
    printf '\033[1m%s\033[0m     https://%s/wallet   pairing code \033[1m%s\033[0m\n' "${names[0]}" "$site" "$code"
    if command -v qrencode >/dev/null; then qrencode -t ansiutf8 -m 2 "$link"; else echo "  pairing link: $link"; fi
    [ $app = 0 ] || { echo; echo "app (Metro)  $(tunnel_url_for "$METRO_PORT" http)  -> now run: ./demo.sh app --ngrok"; }
    echo
    echo "  In the Leafguard app: Scan QR code, or wallet URL https://$site + the code. ./demo.sh stop ends the tunnel."
    return 0
  fi
  log "starting ngrok for ${names[*]}$([ $app = 1 ] && echo ' + the app (Metro)')$([ $explorer = 1 ] && echo ' + explorer')"
  if [ ${#names[@]} -eq 1 ] && [ $explorer = 0 ] && [ $app = 0 ]; then
    nohup ngrok http "$(wallet_port "${names[0]}")" ${url:+--url "$url"} --log stdout > "$TUNNEL_DIR/ngrok.log" 2>&1 &
  else
    {   # one agent, several tunnels (ngrok allows one agent session per account)
      echo 'version: "3"'; echo 'tunnels:'
      for n in "${names[@]}"; do
        if [ -n "$url" ]; then echo "  $n: {proto: http, addr: $(wallet_port "$n"), domain: $url}"
        else echo "  $n: {proto: http, addr: $(wallet_port "$n")}"; fi
      done
      # Expo Go loads the app over plain http on port 80 (exp://<host>:80); http only, as free ngrok allows 3 endpoints
      [ $app = 0 ] || echo "  app: {proto: http, addr: $METRO_PORT, schemes: [http]}"
      [ $explorer = 0 ] || echo "  explorer: {proto: http, addr: $EXPLORER_PORT${xurl:+, domain: $xurl}}"
    } > "$TUNNEL_DIR/ngrok.yml"
    local cfg; cfg="$(ngrok config check 2>&1 | sed -n 's/.*file at //p')"
    nohup ngrok start --all ${cfg:+--config "$cfg"} --config "$TUNNEL_DIR/ngrok.yml" --log stdout > "$TUNNEL_DIR/ngrok.log" 2>&1 &
  fi
  echo $! > "$TUNNEL_DIR/ngrok.pid"
  local i want=$(( ${#names[@]} + explorer + app )) urls=""
  for i in $(seq 1 40); do
    urls="$(tunnel_urls)"
    [ "$(printf '%s\n' "$urls" | grep -c ' http')" -ge "$want" ] && break
    tunnel_running || { tail -5 "$TUNNEL_DIR/ngrok.log" >&2; die "ngrok stopped (see $TUNNEL_DIR/ngrok.log)"; }
    sleep 0.5
  done
  [ -n "$urls" ] || { tail -5 "$TUNNEL_DIR/ngrok.log" >&2; die "ngrok gave no public URL (see $TUNNEL_DIR/ngrok.log)"; }
  for n in "${names[@]}"; do
    port="$(wallet_port "$n")"
    local pub code link
    pub="$(tunnel_url_for "$port" https)"
    [ -n "$pub" ] || { warn "$n: no tunnel came up (free ngrok plans limit endpoints; see $TUNNEL_DIR/ngrok.log)"; continue; }
    code="$(cat "$LOCAL/wallets/$n/pair-code")"
    link="$pub/#pair=$code"
    echo
    printf '\033[1m%s\033[0m  %s   pairing code \033[1m%s\033[0m\n' "$n" "$pub" "$code"
    if command -v qrencode >/dev/null; then qrencode -t ansiutf8 -m 2 "$link"; else echo "  pairing link: $link"; fi
  done
  if [ $app = 1 ]; then
    echo; echo "app (Metro)  $(tunnel_url_for "$METRO_PORT" http || true)  -> now run: ./demo.sh app --ngrok"
  fi
  if [ $explorer = 1 ]; then
    echo; echo "explorer  $(tunnel_url_for "$EXPLORER_PORT" https)"
  fi
  echo
  echo "  In the Leafguard app: Scan QR code (or type the URL and the code). ./demo.sh stop also stops the tunnel."
  echo "  The wallets still only need the pairing code: anyone with the URL alone gets 'not paired'."
}

cmd_app() {   # app [--usb | --ngrok] [expo start options]
  local mode=lan
  case "${1:-}" in --usb) mode=usb; shift ;; --ngrok) mode=ngrok; shift ;; --lan) shift ;; esac
  command -v npm >/dev/null || die "Node.js/npm is needed for the mobile app's dev server"
  cd "$ROOT/mobile"
  [ -d node_modules ] || { log "installing the mobile app's packages (npm ci)"; npm ci --no-audit --no-fund; }
  # Metro's default port 8081 is taken by the Kurtosis engine; --offline: no Expo account or login needed
  local args=(--offline --port "$METRO_PORT") link
  case "$mode" in
    usb)
      command -v adb >/dev/null || die "adb is not installed (sudo apt install adb)"
      [ "$(adb devices | awk 'NR > 1 && $2 == "device"' | wc -l)" -ge 1 ] \
        || die "no Android phone over USB: enable USB debugging, plug it in, accept the prompt on the phone"
      adb reverse "tcp:$METRO_PORT" "tcp:$METRO_PORT" >/dev/null
      link="exp://127.0.0.1:$METRO_PORT"
      args+=(--localhost)
      log "USB: the phone reaches this computer's port $METRO_PORT through adb; opening Expo Go in a few seconds"
      ( for _ in $(seq 1 60); do curl -sf -o /dev/null "http://127.0.0.1:$METRO_PORT/status" && break; sleep 1; done
        adb shell am start -a android.intent.action.VIEW -d "$link" >/dev/null 2>&1 ) &
      ;;
    ngrok)
      local pub; pub="$(tunnel_url_for "$METRO_PORT" http)"
      [ -n "$pub" ] || pub="$(tunnel_url_for "$METRO_PORT" https)"
      [ -n "$pub" ] || die "no ngrok tunnel to Metro: run ./demo.sh tunnel alice --app first"
      export EXPO_PACKAGER_PROXY_URL="$pub"
      link="exp://${pub#*://}:$([ "${pub%%://*}" = http ] && echo 80 || echo 443)"
      log "ngrok: the app loads through $pub (any network, no Expo login)"
      ;;
    lan)
      args+=(--lan)
      link="exp://$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1); exit}'):$METRO_PORT"
      log "same Wi-Fi: the phone loads the app from this computer's port $METRO_PORT (no luck? use --ngrok or --usb)"
      ;;
  esac
  echo "  Install Expo Go on the phone. Open this in Expo Go (scan, or Enter URL manually): $link"
  command -v qrencode >/dev/null && qrencode -t ansiutf8 -m 2 "$link"
  exec npx expo start "${args[@]}" "$@"
}

# ----------------------------------------------------------------------------- commands
cmd_up() {
  local fresh=0 lan=1 wallets="${DEMO_WALLETS:-alice bob carol}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --fresh) fresh=1 ;;
      --no-lan) lan=0 ;;
      --wallets) shift; wallets="${1:?--wallets needs a list, e.g. \"alice bob carol\"}" ;;
      *) die "unknown option $1 (up [--fresh] [--no-lan] [--wallets \"a b c\"])" ;;
    esac
    shift
  done
  # shellcheck disable=SC2086
  set -- $wallets
  [ $# -ge 1 ] || die "no wallets given"
  [ $# -ge 3 ] || warn "shared 2-of-3 vaults need three wallets"
  preflight
  ensure_chain "$fresh"
  ensure_sidecar
  archive_stale_vaults
  ensure_wallets "$lan" "$@"
  ensure_explorer
  summary "$@"
}

cmd_status() {
  local rpc; rpc="$(json_get "$LOCAL/network.json" ethrex)"
  if rpc_up "$rpc"; then echo "network:  $rpc  block $(cast block-number --rpc-url "$rpc")"; else echo "network:  down"; fi
  if sidecar_up "$(json_get "$LOCAL/network.json" sidecar)"; then
    echo "mempool:  $(json_get "$LOCAL/network.json" sidecar)  $(docker inspect pqvault-sidecar --format '{{.Config.Image}}' 2>/dev/null)"
  else echo "mempool:  down"; fi
  infra/wallets.sh status | sed 's/^/wallet:   /'
  if explorer_running; then echo "explorer: http://127.0.0.1:$EXPLORER_PORT"; else echo "explorer: stopped"; fi
  if tunnel_running; then tunnel_urls | awk '{print "tunnel:   127.0.0.1:" $1 " -> " $2}'; else echo "tunnel:   off (./demo.sh tunnel)"; fi
}

cmd_stop() {
  stop_tunnel
  if pgrep -f '[e]xpo start --offline' >/dev/null; then pkill -f '[e]xpo start --offline'; echo "app dev server: stopped"; fi
  infra/wallets.sh stop || true
  if explorer_running; then kill "$(cat "$LOCAL/explorer.pid")"; echo "explorer: stopped"; fi
  rm -f "$LOCAL/explorer.pid"
  if [ "${1:-}" = "--chain" ]; then
    docker stop pqvault-sidecar >/dev/null 2>&1 && echo "mempool: stopped" || true
    local ids
    ids="$(docker ps --filter "label=kurtosis_enclave_name=$ENCLAVE" --format '{{.ID}} {{.Names}}' | awk '$2 ~ /^(vc|cl|el)-[0-9]+-/ {print $1}')"
    # shellcheck disable=SC2086
    [ -z "$ids" ] || { docker stop $ids >/dev/null; echo "network: paused (./demo.sh up resumes it)"; }
  fi
}

case "${1:-up}" in
  up)     [ $# -gt 0 ] && shift; cmd_up "$@" ;;
  status) cmd_status ;;
  stop)   shift; cmd_stop "${1:-}" ;;
  tunnel) shift; cmd_tunnel "$@" ;;
  app)    shift; cmd_app "$@" ;;
  -h|--help|help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" ;;
  *) die "usage: ./demo.sh [up [--fresh] [--no-lan] [--wallets \"a b c\"] | status | stop [--chain] | tunnel [names] | app]" ;;
esac
