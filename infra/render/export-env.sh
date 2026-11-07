#!/usr/bin/env bash
# Run on the chain server (where ./start.sh or ./demo.sh up runs) to print the environment for the Leafguard web
# container (infra/render/Dockerfile): paste the lines into Render's environment, or save them as an --env-file.
#
#   infra/render/export-env.sh <public host or IP of this server> [> leafguard.env]
#
# The node RPC and the alt-mempool sidecar must be reachable from the web container at that host
# (open the two ports printed below in the server's firewall). Devnet only: DEPLOYER_KEY is the throwaway
# devnet deployer, never a real key.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
HOST="${1:?usage: $0 <public host or IP of this server>}"
L=.devnet/local
[ -f "$L/network.json" ] && [ -f "$L/deployment.json" ] && [ -f .devnet/deployer.json ] \
  || { echo "no running Leafguard devnet here (.devnet/local/*.json): ./start.sh first" >&2; exit 1; }
python3 - "$HOST" <<'PY'
import json, sys
from urllib.parse import urlparse
host = sys.argv[1]
net = json.load(open(".devnet/local/network.json"))
dep = json.load(open(".devnet/local/deployment.json"))
key = json.load(open(".devnet/deployer.json")); key = key[0] if isinstance(key, list) else key
infra = {k: dep[k] for k in ("staking", "frameOps", "forsVerifier", "spxVerifier", "factory", "ledger") if k in dep}
def public(url):
    u = urlparse(url)
    return f"{u.scheme}://{host}:{u.port}{u.path}"
print(f"ETHREX_RPC={public(net['ethrex'])}")
print(f"SIDECAR_RPC={public(net['sidecar'])}")
print("DEPLOYMENT_JSON=" + json.dumps(infra, separators=(",", ":")))
print(f"DEPLOYER_KEY={key['private_key']}")
print(f"# open these ports on this server: {urlparse(net['ethrex']).port} (node RPC), {urlparse(net['sidecar']).port} (alt mempool)",
      file=sys.stderr)
PY
