#!/usr/bin/env bash
# Leafguard demo in one container (Render, or any Docker host). Devnet only, never real funds.
#
# Boots a fresh single-node EIP-8141 chain (ethrex eip8141-tracer + Lighthouse beacon node + validator client),
# deploys the Leafguard contracts, starts the ERC-8421 alt-mempool sidecar with [BUDGET-010], creates the
# alice / bob / carol wallets, and serves the explorer at / and alice's wallet at /wallet on $PORT.
# Every start is a new chain (state lives in /data, wiped on boot), ready in about 8 minutes.
#
# Environment:
#   PORT                 public port (Render sets it; default 10000)
#   ALICE_PAIR_CODE      fixed pairing code for alice (12 letters/digits, e.g. K7QM-P3XA-WN9D); random if unset
#   PQV_PREFUNDED_KEY    key of the genesis-prefunded test account (default: read from the upstream workshop yaml)
#   WALLETS              wallets to create (default "alice bob carol"); the first one is served at /wallet
set -euo pipefail
APP=/app
DATA=/data
PORT="${PORT:-10000}"
WALLETS="${WALLETS:-alice bob carol}"
PY=/usr/local/bin/pqpython
RPC=http://127.0.0.1:8545
SIDECAR=http://127.0.0.1:14337/rpc
FEE_RECIPIENT=0x8943545177806ED17B9F23F0a21ee5948eCaa776
PREFUNDED_ADDR=0x8943545177806ED17B9F23F0a21ee5948eCaa776
UPSTREAM_YAML="https://raw.githubusercontent.com/forshtat/mempool-workshop-sidecar/2e22ff0a2898e69ea4bf5275bce1be8581a8582a/test/e2e/kurtosis/eip8141-single-node.yaml"
HEGOTA_BLOCK=66
LOGS="$DATA/logs"

log() { printf '[leafguard %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "FATAL: $*"; exit 1; }
PIDS=()
run() {  # run <name> <cmd...>: background service, log to $LOGS/<name>.log (tail it on failure)
  local name="$1"; shift
  "$@" > "$LOGS/$name.log" 2>&1 &
  PIDS+=("$!")
  log "started $name (pid $!)"
}
cleanup() { log "stopping"; kill "${PIDS[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM
wait_for() {  # wait_for <description> <seconds> <command...>
  local what="$1" secs="$2"; shift 2
  local i
  for ((i = 0; i < secs; i++)); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  die "$what did not come up in ${secs}s"
}

# ----------------------------------------------------------------------------- 0. fresh state, public port
rm -rf "$DATA" "$APP/.devnet"
mkdir -p "$LOGS" "$DATA/net" "$APP/.devnet/local"
# the gateway answers right away (a "starting" page until the explorer and wallet are up), so the platform
# sees the port open while the chain boots
run gateway "$PY" -m pqv.gateway --host 0.0.0.0 --port "$PORT" --explorer 8300 --wallet 8201

# ----------------------------------------------------------------------------- 1. chain
cp -r "$APP/infra/render/network/network-configs/." "$DATA/net/"
cp "$APP/infra/render/network/jwt/jwtsecret" "$DATA/net/jwtsecret"
cp -r "$APP/infra/render/network/validator-keys" "$DATA/vkeys"
: > "$DATA/net/bootstrap_nodes.txt"   # single node: no peers to find
"$PY" "$APP/infra/render/patch_genesis.py" "$DATA/net" 25

run ethrex ethrex --datadir="$DATA/ethrex" --network="$DATA/net/genesis.json" --syncmode=full --log.level=info \
  --http.addr=127.0.0.1 --http.port=8545 --http.api=eth,net,web3,debug,admin,txpool --http.api=ethrex \
  --authrpc.addr=127.0.0.1 --authrpc.port=8551 --authrpc.jwtsecret="$DATA/net/jwtsecret" \
  --p2p.port=30303 --discovery.port=30303 --builder.gas-limit=200000000
wait_for "ethrex RPC" 120 cast chain-id --rpc-url "$RPC"

run beacon lighthouse beacon_node --debug-level=info --datadir="$DATA/lighthouse" --testnet-dir="$DATA/net" \
  --listen-address=127.0.0.1 --port=9000 --quic-port=9001 --http --http-address=127.0.0.1 --http-port=4000 \
  --disable-packet-filter --disable-enr-auto-update --enr-address=127.0.0.1 --enr-tcp-port=9000 \
  --enr-udp-port=9000 --enr-quic-port=9001 --execution-endpoints=http://127.0.0.1:8551 \
  --jwt-secrets="$DATA/net/jwtsecret" --suggested-fee-recipient="$FEE_RECIPIENT" --supernode \
  --allow-insecure-genesis-sync --target-peers=0
wait_for "beacon node API" 180 curl -sf http://127.0.0.1:4000/eth/v1/node/version

run validator lighthouse vc --debug-level=info --testnet-dir="$DATA/net" --validators-dir="$DATA/vkeys/keys" \
  --secrets-dir="$DATA/vkeys/secrets" --init-slashing-protection --beacon-nodes=http://127.0.0.1:4000 \
  --suggested-fee-recipient="$FEE_RECIPIENT" --gas-limit=200000000 --builder-proposals

log "waiting for the first blocks"
wait_for "block production" 240 bash -c "[ \$(cast block-number --rpc-url $RPC) -ge 2 ]"
log "chain $(cast chain-id --rpc-url "$RPC") producing blocks"

# ----------------------------------------------------------------------------- 2. contracts
printf '{"ethrex": "%s", "sidecar": "%s"}\n' "$RPC" "$SIDECAR" > "$APP/.devnet/local/network.json"
cast wallet new --json > "$APP/.devnet/deployer.json"
chmod 600 "$APP/.devnet/deployer.json"
dep="$("$PY" -c 'import json; d=json.load(open("/app/.devnet/deployer.json")); d=d[0] if isinstance(d,list) else d; print(d["address"])')"
key="${PQV_PREFUNDED_KEY:-$(curl -sf "$UPSTREAM_YAML" | grep -oE 'private key: 0x[0-9a-fA-F]{64}' | head -1 | awk '{print $3}')}"
[ -n "$key" ] || die "no prefunded test key (set PQV_PREFUNDED_KEY)"
[ "$(cast wallet address "$key")" = "$PREFUNDED_ADDR" ] || die "the prefunded key does not match $PREFUNDED_ADDR"
cast send --rpc-url "$RPC" --private-key "$key" --legacy --gas-price 3000000000 "$dep" --value 1000ether >/dev/null
log "funded the throwaway deployer $dep"
(cd "$APP" && "$PY" -m pqv.devnet local deploy-infra) > "$LOGS/deploy-infra.log" 2>&1 || { tail -20 "$LOGS/deploy-infra.log"; die "deploy-infra failed"; }
staking="$("$PY" -c 'import json; print(json.load(open("/app/.devnet/local/deployment.json"))["staking"])')"
log "contracts deployed (staking registry $staking)"

# ----------------------------------------------------------------------------- 3. alt mempool ([BUDGET-010])
relayer="$(cast wallet new --json | "$PY" -c 'import json,sys; d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d; print(d["private_key"])')"
"$PY" - "$staking" "$relayer" <<'EOF'
import json, sys
staking, relayer = sys.argv[1:]
mp, ep = "QmcFZKUX9qoo3StwVycJTAhwdiqbEjcwNvQVK246p3z1rk", "0x00000000000000000000000000000000000000aa"
json.dump({"entryPoints": [ep], "canonicalEntryPoint": ep, "standardMempoolId": mp,
           "supportedMempools": [{"mempoolId": mp, "entryPoint": ep}], "relayers": [relayer],
           "rpcEndpoint": "http://127.0.0.1:8545", "stakingRegistryAddress": staking, "minStake": 10**16,
           "minUnstakeDelay": 0, "minSignerBalance": 0, "bundleInterval": 1000, "maxSubmitAttempts": 120},
          open("/opt/sidecar/config.json", "w"), indent=1)
EOF
rm -rf /opt/sidecar/db /root/.skandha 2>/dev/null || true
run sidecar bash -c 'cd /opt/sidecar && exec bun --bun ./packages/cli/bin/skandha.js standalone --redirectRpc --executor.bundlingMode auto --api.port 14337'
wait_for "alt-mempool sidecar" 120 curl -sf -X POST -H 'content-type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' "$SIDECAR"

# ----------------------------------------------------------------------------- 4. wallets + explorer
port=8201
first=""
for w in $WALLETS; do
  mkdir -p "$APP/.devnet/local/wallets/$w"
  if [ "$w" = alice ] && [ -n "${ALICE_PAIR_CODE:-}" ]; then
    code="$(printf '%s' "$ALICE_PAIR_CODE" | tr 'a-z' 'A-Z' | tr -cd 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789')"
    [ ${#code} -eq 12 ] || die "ALICE_PAIR_CODE must be 12 characters from A-Z (no I, O) and 2-9, e.g. K7QM-P3XA-WN9D"
    printf '%s-%s-%s\n' "${code:0:4}" "${code:4:4}" "${code:8:4}" > "$APP/.devnet/local/wallets/$w/pair-code"
    chmod 600 "$APP/.devnet/local/wallets/$w/pair-code"
  fi
  run "wallet-$w" bash -c "cd $APP && exec $PY -m pqv.wallet $w --port $port"
  eval "PORT_$w=$port"
  [ -n "$first" ] || first="$w"
  port=$((port + 1))
done
run explorer bash -c "cd $APP && exec $PY -m pqv.explorer --network local --port 8300"
for w in $WALLETS; do
  p="PORT_$w"; wait_for "wallet $w" 60 curl -sf "http://127.0.0.1:${!p}/api/state"
done

job() {  # job <port> <path>: start a wallet job and wait for it
  local id st
  id="$(curl -sf -X POST -H 'content-type: application/json' -d '{}' "http://127.0.0.1:$1$2" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["job"])')"
  for _ in $(seq 1 120); do
    st="$(curl -sf "http://127.0.0.1:$1/api/jobs/$id" | "$PY" -c 'import json,sys; j=json.load(sys.stdin); print(j["state"], j.get("error") or "")')"
    case "$st" in running*) sleep 2 ;; done*) return 0 ;; *) log "job $2 on :$1: $st"; return 1 ;; esac
  done
  return 1
}
for w in $WALLETS; do
  p="PORT_$w"; job "${!p}" /api/create && log "$w: account created" || log "$w: account creation failed (see $LOGS/wallet-$w.log)"
done

log "waiting for Hegota (block $HEGOTA_BLOCK) so payments can be sent"
wait_for "Hegota" 900 bash -c "[ \$(cast block-number --rpc-url $RPC) -ge $HEGOTA_BLOCK ]"
log "READY: explorer at / and $first's wallet at /wallet on port $PORT; pairing code $(cat "$APP/.devnet/local/wallets/$first/pair-code")"

# ----------------------------------------------------------------------------- 5. stay up; exit if a service dies
while true; do
  for i in "${!PIDS[@]}"; do
    if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
      log "a service exited (pid ${PIDS[$i]}); last log lines:"
      for f in "$LOGS"/*.log; do echo "== $f"; tail -5 "$f"; done
      exit 1
    fi
  done
  sleep 10
done
