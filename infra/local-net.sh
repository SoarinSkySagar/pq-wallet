#!/usr/bin/env bash
# One-command local EIP-8141 network for Leafguard (replaces the public hackathon gateway):
#   Kurtosis: ethrex (forshtat eip8141-tracer @ 9aa3645d) + Lighthouse (focil)    -> infra/local-devnet.sh
#   skandha ERC-8421 alt-mempool sidecar, [BUDGET-010] build by default           -> infra/sidecar-local.sh
#
#   infra/local-net.sh up [budget010|stock]   fresh chain, fund the throwaway deployer, staking registry + infra,
#                                             sidecar, wait for Hegota (frame txs). Archives old .devnet/local state.
#   infra/local-net.sh sidecar budget010|stock   (re)start only the sidecar
#   infra/local-net.sh status
#   infra/local-net.sh down
#
# Why not ethpandaops/ethrex:main (e.g. ../frames/chain.sh): the sidecar hands admitted transactions to ethrex via
# ethrex_submitPrivilegedFrameTransaction and validates with ethrex_simulateFrameTransaction, which exist only in
# the forshtat/ethrex eip8141-tracer fork.
#
# The prefunded test account's key is the published throwaway CI key of forshtat/mempool-workshop-sidecar
# (test/e2e/kurtosis/eip8141-single-node.yaml @ 2e22ff0a). It is not stored in this repo: set PQV_PREFUNDED_KEY,
# or the script reads it from that pinned upstream file.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
LOCAL=.devnet/local
SIDECAR_URL="http://127.0.0.1:14337/rpc"
HEGOTA_BLOCK=66   # heze_fork_epoch 2 (infra/kurtosis/eip8141-single-node.yaml) = slot 64, plus margin
UPSTREAM_YAML="https://raw.githubusercontent.com/forshtat/mempool-workshop-sidecar/2e22ff0a2898e69ea4bf5275bce1be8581a8582a/test/e2e/kurtosis/eip8141-single-node.yaml"
PREFUNDED_ADDR="0x5544596212F91984C986A919C8026DF0576816f9"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

pick_python() {   # a Python whose hashlib has KECCAK-256 (OpenSSL >= 3.2)
  local c
  for c in "${PQV_PYTHON:-}" python3.12 python3.13 python3 "$HOME"/.local/share/uv/python/cpython-3.1*/bin/python3; do
    [ -n "$c" ] && command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
    "$c" -c 'import hashlib; hashlib.new("KECCAK-256")' >/dev/null 2>&1 && { echo "$c"; return; }
  done
  die "no Python with KECCAK-256 (need OpenSSL >= 3.2); set PQV_PYTHON"
}

rpc_url() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ethrex"])' "$LOCAL/network.json"; }

start_sidecar() {   # budget010 | stock
  local kind="${1:-budget010}" image port staking
  case "$kind" in
    budget010) image="pqvault/mempool-sidecar:budget-010"
               docker image inspect "$image" >/dev/null 2>&1 || infra/sidecar-budget010/build.sh ;;
    stock)     image="ghcr.io/forshtat/mempool-workshop-sidecar@sha256:c4cdc5c707d94f3968566ba34904d1a7c4ab4dee962d822e1bac1bf6714e022e" ;;
    *) die "sidecar must be budget010 or stock" ;;
  esac
  port="$(rpc_url | sed -E 's#.*:([0-9]+)/?$#\1#')"
  staking="$(python3 -c 'import json; print(json.load(open(".devnet/local/deployment.json"))["staking"])')"
  SIDECAR_IMAGE="$image" infra/sidecar-local.sh "$port" "$staking"
  until curl -sf -X POST -H 'content-type: application/json' \
      --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' "$SIDECAR_URL" >/dev/null; do sleep 2; done
  log "sidecar ($kind) ready at $SIDECAR_URL"
}

prefunded_key() {
  if [ -n "${PQV_PREFUNDED_KEY:-}" ]; then echo "$PQV_PREFUNDED_KEY"; return; fi
  curl -sf "$UPSTREAM_YAML" | grep -oE 'private key: 0x[0-9a-fA-F]{64}' | head -1 | awk '{print $3}'
}

cmd_up() {
  local kind="${1:-budget010}" py rpc dep key stamp
  py="$(pick_python)"
  command -v kurtosis >/dev/null && command -v docker >/dev/null && command -v cast >/dev/null \
    || die "needs kurtosis, docker and foundry (cast)"
  (cd contracts && forge build -q)
  if [ -d "$LOCAL" ] && ls "$LOCAL"/*.json >/dev/null 2>&1; then   # a fresh chain invalidates the old state
    stamp="$LOCAL/archive-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$stamp"
    find "$LOCAL" -maxdepth 1 -type f \( -name '*.json' ! -name 'sidecar-config.json' \) -exec mv {} "$stamp"/ \;
    log "archived previous local state to $stamp"
  fi
  mkdir -p "$LOCAL"
  log "starting Kurtosis devnet (ethrex eip8141-tracer + Lighthouse)"
  infra/local-devnet.sh up >/dev/null
  rpc="$(infra/local-devnet.sh rpc)"
  until cast chain-id --rpc-url "$rpc" >/dev/null 2>&1; do sleep 2; done
  printf '{"ethrex": "%s", "sidecar": "%s"}\n' "$rpc" "$SIDECAR_URL" > "$LOCAL/network.json"
  log "ethrex: $rpc ($(cast rpc --rpc-url "$rpc" web3_clientVersion))"

  [ -f .devnet/deployer.json ] || { cast wallet new --json > .devnet/deployer.json; chmod 600 .devnet/deployer.json; }
  dep="$(python3 -c 'import json; d=json.load(open(".devnet/deployer.json")); d=d[0] if isinstance(d,list) else d; print(d["address"])')"
  key="$(prefunded_key)"; [ -n "$key" ] || die "could not get the prefunded test key (set PQV_PREFUNDED_KEY)"
  [ "$(cast wallet address "$key")" = "$PREFUNDED_ADDR" ] || die "prefunded key does not match $PREFUNDED_ADDR"
  cast send --rpc-url "$rpc" --private-key "$key" --legacy --gas-price 3000000000 "$dep" --value 100ether >/dev/null
  log "funded deployer $dep with 100 ETH (devnet)"

  "$py" -m pqv.devnet local deploy-infra
  start_sidecar "$kind"
  log "waiting for Hegota (block $HEGOTA_BLOCK; ~7 min after genesis)"
  until [ "$(cast block-number --rpc-url "$rpc")" -ge "$HEGOTA_BLOCK" ]; do sleep 10; done
  log "ready. next: $py -m pqv.devnet local new-guardians && $py -m pqv.devnet local deploy-vault --fund 2 --stake 0.02"
}

cmd_status() {
  local rpc; rpc="$(rpc_url)"
  echo "ethrex:  $rpc  block $(cast block-number --rpc-url "$rpc")  $(cast rpc --rpc-url "$rpc" web3_clientVersion)"
  echo "sidecar: $SIDECAR_URL  image $(docker inspect pqvault-sidecar --format '{{.Config.Image}}' 2>/dev/null || echo 'not running')"
  python3 -c 'import json; d=json.load(open(".devnet/local/deployment.json")); [print(f"  {k}: {v}") for k,v in d.items() if k not in ("txs",)]' 2>/dev/null || true
}

case "${1:-}" in
  up)      shift; cmd_up "$@" ;;
  sidecar) shift; start_sidecar "${1:-budget010}" ;;
  status)  cmd_status ;;
  down)    docker rm -f pqvault-sidecar >/dev/null 2>&1 || true; infra/local-devnet.sh down ;;
  *) echo "usage: $0 up [budget010|stock] | sidecar budget010|stock | status | down" >&2; exit 2 ;;
esac
