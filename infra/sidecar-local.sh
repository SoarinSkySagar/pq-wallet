#!/usr/bin/env bash
# Run the ERC-8421 alt-mempool sidecar (forshtat/mempool-workshop-sidecar, published image) against the
# local Kurtosis ethrex.  Usage: infra/sidecar-local.sh <ethrex-host-port> <staking-registry> [relayer-key-file]
# The image is linux/amd64-only; on Apple Silicon Docker runs it under emulation (bun works there).
set -euo pipefail
PORT="$1"; STAKING="$2"; KEYFILE="${3:-.devnet/local/relayer.key}"
IMAGE="${SIDECAR_IMAGE:-ghcr.io/forshtat/mempool-workshop-sidecar:latest}"
CFG="$(pwd)/.devnet/local/sidecar-config.json"
mkdir -p .devnet/local
[ -f "$KEYFILE" ] || { cast wallet new --json | python3 -c 'import json,sys; d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d; print(d["private_key"])' > "$KEYFILE"; chmod 600 "$KEYFILE"; }
python3 - "$CFG" "$PORT" "$STAKING" "$KEYFILE" <<'PY'
import json, sys
cfg, port, staking, keyfile = sys.argv[1:]
mp = "QmcFZKUX9qoo3StwVycJTAhwdiqbEjcwNvQVK246p3z1rk"
ep = "0x00000000000000000000000000000000000000aa"
json.dump({"entryPoints": [ep], "canonicalEntryPoint": ep, "standardMempoolId": mp,
           "supportedMempools": [{"mempoolId": mp, "entryPoint": ep}],
           "relayers": [open(keyfile).read().strip()],
           "rpcEndpoint": f"http://host.docker.internal:{port}",
           "stakingRegistryAddress": staking, "minStake": 10**16, "minUnstakeDelay": 0,
           "minSignerBalance": 0, "bundleInterval": 1000, "maxSubmitAttempts": 120}, open(cfg, "w"), indent=1)
PY
chmod 600 "$CFG"
docker rm -f pqvault-sidecar >/dev/null 2>&1 || true
docker run -d --name pqvault-sidecar --platform linux/amd64 --add-host host.docker.internal:host-gateway \
  -p 14337:14337 -v "$CFG":/usr/app/config.json:ro "$IMAGE" \
  standalone --redirectRpc --executor.bundlingMode auto --api.port 14337 >/dev/null
docker image inspect "$IMAGE" --format 'sidecar image: {{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}} (locally built){{end}}'
echo "sidecar: http://127.0.0.1:14337/rpc"
