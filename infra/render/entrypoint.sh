#!/usr/bin/env bash
# Leafguard web container: wallets + explorer + gateway (explorer at /, the first wallet at /wallet) on $PORT,
# against a chain and alt mempool running elsewhere. Devnet only, never real funds.
#
# Required environment (print the values on the chain server with infra/render/export-env.sh):
#   ETHREX_RPC        the chain's JSON-RPC, reachable from this container   e.g. http://203.0.113.7:32003
#   SIDECAR_RPC       the ERC-8421 alt-mempool sidecar ([BUDGET-010])       e.g. http://203.0.113.7:14337/rpc
#   DEPLOYMENT_JSON   the chain's Leafguard contract addresses (.devnet/local/deployment.json on the server)
#   DEPLOYER_KEY      the throwaway devnet deployer's private key (pays for account setup and test ETH)
# Optional:
#   ALICE_PAIR_CODE, BOB_PAIR_CODE, CAROL_PAIR_CODE   pairing codes (12 characters, A-Z without I/O and 2-9);
#                     random if unset (printed in the log)
#   WALLETS           default "alice bob carol"; each is served at /<name>/, the first also at /wallet
#   PORT              default 10000 (Render sets it)
# Wallet keys live in /app/.devnet/local/wallets; mount a persistent disk at /app/.devnet to keep accounts across
# restarts (otherwise every start creates new accounts, funded by the deployer).
set -euo pipefail
APP=/app
PY=/usr/local/bin/pqpython
PORT="${PORT:-10000}"
WALLETS="${WALLETS:-alice bob carol}"
LOCAL="$APP/.devnet/local"
LOGS=/tmp/leafguard-logs
LOW_BALANCE_WEI=100000000000000000   # 0.1 ETH

log() { printf '[leafguard %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "FATAL: $*"; exit 1; }
PIDS=()
run() { local name="$1"; shift; "$@" > "$LOGS/$name.log" 2>&1 & PIDS+=("$!"); log "started $name"; }
trap 'kill "${PIDS[@]}" 2>/dev/null || true' EXIT INT TERM

for v in ETHREX_RPC SIDECAR_RPC DEPLOYMENT_JSON DEPLOYER_KEY; do
  [ -n "${!v:-}" ] || die "$v is not set (see infra/render/README.md)"
done
mkdir -p "$LOCAL/wallets" "$LOGS"

# the gateway answers at once (a "starting" page until the servers are up)
first="${WALLETS%% *}"
gw="" port=8201
for w in $WALLETS; do gw="$gw${gw:+,}$w:$port"; port=$((port + 1)); done
run gateway "$PY" -m pqv.gateway --host 0.0.0.0 --port "$PORT" --explorer 8300 --wallets "$gw"

# ----------------------------------------------------------------------------- configuration from the environment
printf '{"ethrex": "%s", "sidecar": "%s"}\n' "$ETHREX_RPC" "$SIDECAR_RPC" > "$LOCAL/network.json"
printf '%s' "$DEPLOYMENT_JSON" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); assert d.get("factory") and d.get("ledger"), "DEPLOYMENT_JSON has no factory/ledger"; json.dump(d, open(sys.argv[1], "w"), indent=1)' \
  "$LOCAL/deployment.json" || die "DEPLOYMENT_JSON is not the server's deployment.json"
addr="$(cast wallet address "$DEPLOYER_KEY")" || die "DEPLOYER_KEY is not a private key"
printf '{"address": "%s", "private_key": "%s"}\n' "$addr" "$DEPLOYER_KEY" > "$APP/.devnet/deployer.json"
chmod 600 "$APP/.devnet/deployer.json" "$LOCAL/deployment.json"
chain="$(cast chain-id --rpc-url "$ETHREX_RPC")" || die "no chain answers at ETHREX_RPC=$ETHREX_RPC"
curl -sf -m 10 -X POST -H 'content-type: application/json' --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' \
  "$SIDECAR_RPC" >/dev/null || log "warning: the alt mempool does not answer at SIDECAR_RPC=$SIDECAR_RPC (payments will fail)"
[ "$(cast codesize "$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["factory"])' "$LOCAL/deployment.json")" --rpc-url "$ETHREX_RPC")" != 0 ] \
  || die "the factory in DEPLOYMENT_JSON is not deployed on chain $chain (fresh chain? copy deployment.json again)"
log "chain $chain at $ETHREX_RPC, deployer $addr ($(cast balance "$addr" --rpc-url "$ETHREX_RPC" --ether) ETH)"

# ----------------------------------------------------------------------------- servers
for w in $WALLETS; do   # optional fixed pairing codes: ALICE_PAIR_CODE, BOB_PAIR_CODE, ...
  v="$(printf '%s' "$w" | tr 'a-z-' 'A-Z_')_PAIR_CODE"
  [ -n "${!v:-}" ] || continue
  code="$(printf '%s' "${!v}" | tr 'a-z' 'A-Z' | tr -cd 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789')"
  [ ${#code} -eq 12 ] || die "$v must be 12 characters from A-Z (no I, O) and 2-9, e.g. K7QM-P3XA-WN9D"
  mkdir -p "$LOCAL/wallets/$w"
  printf '%s-%s-%s\n' "${code:0:4}" "${code:4:4}" "${code:8:4}" > "$LOCAL/wallets/$w/pair-code"
  chmod 600 "$LOCAL/wallets/$w/pair-code"
done
port=8201
for w in $WALLETS; do
  run "wallet-$w" bash -c "cd $APP && exec $PY -m pqv.wallet $w --port $port"
  port=$((port + 1))
done
run explorer bash -c "cd $APP && exec $PY -m pqv.explorer --network local --port 8300"

# ----------------------------------------------------------------------------- accounts
job() {  # job <port> <path>: run a wallet job to the end
  local id st
  id="$(curl -sf -X POST -H 'content-type: application/json' -d '{}' "http://127.0.0.1:$1$2" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["job"])')" || return 1
  for _ in $(seq 1 150); do
    st="$(curl -sf "http://127.0.0.1:$1/api/jobs/$id" | "$PY" -c 'import json,sys; j=json.load(sys.stdin); print(j["state"], j.get("error") or "")')"
    case "$st" in running*) sleep 2 ;; done*) return 0 ;; *) log "$2 on :$1: $st"; return 1 ;; esac
  done
  return 1
}
port=8201
for w in $WALLETS; do
  for _ in $(seq 1 60); do curl -sf -o /dev/null "http://127.0.0.1:$port/api/state" && break; sleep 1; done
  state="$(curl -sf "http://127.0.0.1:$port/api/state")" || die "wallet $w did not start (log: $LOGS/wallet-$w.log)"
  if [ "$(printf '%s' "$state" | "$PY" -c 'import json,sys; print(bool(json.load(sys.stdin).get("account")))')" = False ]; then
    log "$w: creating the account (three post-quantum keys, smart account, 1 test ETH)"
    job "$port" /api/create && log "$w: account created" || log "$w: account creation failed (log: $LOGS/wallet-$w.log)"
  else
    bal="$(curl -sf "http://127.0.0.1:$port/api/state" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["account"]["balanceWei"])')"
    if "$PY" -c 'import sys; sys.exit(int(sys.argv[1]) >= int(sys.argv[2]))' "$bal" "$LOW_BALANCE_WEI"; then
      log "$w: low balance, adding 1 test ETH"; job "$port" /api/faucet || true
    fi
  fi
  port=$((port + 1))
done
log "READY on port $PORT: explorer at /"
for w in $WALLETS; do log "  /$w/   pairing code $(cat "$LOCAL/wallets/$w/pair-code")"; done

# ----------------------------------------------------------------------------- stay up; exit if a server dies
while true; do
  for p in "${PIDS[@]}"; do
    kill -0 "$p" 2>/dev/null || { log "a server exited; last log lines:"; tail -n 5 "$LOGS"/*.log; exit 1; }
  done
  sleep 10
done
